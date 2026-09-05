import copy
from collections import Counter
from dataclasses import replace

import pytest
from downforce_core.domain import (
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
    TrackStatus,
    WeatherRecord,
)
from downforce_core.ml.artifacts import (
    ArtifactStore,
    ArtifactUnavailableError,
    ridge_from_dict,
    ridge_to_dict,
)
from downforce_core.ml.contracts import DatasetSplit
from downforce_core.ml.dataset import _pit_rows
from downforce_core.ml.features import CanonicalFeatureBuilder, feature_schema_payload
from downforce_core.ml.inference import MLInferenceEngine
from downforce_core.ml.model import RidgeModel
from downforce_core.storage import DownforceRepository
from test_replay_engine import _provenance, _session


def _eligible_session():  # type: ignore[no-untyped-def]
    session = _session()
    driver_id = session.drivers[0].driver_id
    session_id = session.metadata.session_id
    additional_laps = tuple(
        LapRecord(
            session_id=session_id,
            driver_id=driver_id,
            lap_number=number,
            lap_start_time_ms=(number - 1) * 90_000,
            lap_end_time_ms=number * 90_000,
            lap_time_ms=90_000 + number * 100,
            stint_number=2,
            compound=session.laps[1].compound,
            tyre_life_laps=float(number - 1),
            is_accurate=True,
            is_generated=False,
            is_deleted=False,
            raw_track_status="1",
            provenance=_provenance("fixture.ml-laps"),
        )
        for number in range(3, 7)
    )
    positions = session.race_positions + tuple(
        RacePositionRecord(
            session_id=session_id,
            driver_id=driver_id,
            session_time_ms=number * 90_000,
            position=1,
            lap_number=number,
            provenance=_provenance("fixture.ml-positions"),
        )
        for number in range(3, 7)
    )
    weather = (
        WeatherRecord(
            session_id=session_id,
            session_time_ms=0,
            air_temperature_c=20,
            track_temperature_c=30,
            humidity_percent=50,
            rainfall=False,
            provenance=_provenance("fixture.ml-weather"),
        ),
    )
    return replace(
        session,
        laps=session.laps + additional_laps,
        race_positions=positions,
        weather=weather,
    )


def test_feature_builder_is_immutable_to_every_future_canonical_observation() -> None:
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    before = CanonicalFeatureBuilder(session).feature_for_lap(driver_id, 4)
    assert before.feature is not None
    future_lap = replace(
        session.laps[-1],
        lap_time_ms=240_000,
        tyre_life_laps=99,
        raw_track_status="5",
    )
    future_weather = WeatherRecord(
        session_id=session.metadata.session_id,
        session_time_ms=900_000,
        air_temperature_c=99,
        track_temperature_c=99,
        humidity_percent=99,
        rainfall=True,
        provenance=_provenance("fixture.future-weather"),
    )
    future_position = replace(session.race_positions[-1], session_time_ms=900_000, position=20)
    mutated = replace(
        session,
        laps=session.laps[:-1] + (future_lap,),
        weather=session.weather + (future_weather,),
        race_positions=session.race_positions + (future_position,),
    )
    after = CanonicalFeatureBuilder(mutated).feature_for_lap(driver_id, 4)
    assert after == before


def test_feature_cursor_snaps_to_completed_boundary_and_rejects_neutralized_lap() -> None:
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    builder = CanonicalFeatureBuilder(session)
    at_boundary = builder.feature_at(driver_id, 4 * 90_000)
    between_boundaries = builder.feature_at(driver_id, 5 * 90_000 - 1)
    assert at_boundary == between_boundaries
    neutralized = replace(session.laps[-1], raw_track_status="14")
    changed = replace(session, laps=session.laps[:-1] + (neutralized,))
    rejected = CanonicalFeatureBuilder(changed).feature_for_lap(driver_id, 6)
    assert rejected.feature is None
    assert rejected.eligibility.reason == "neutralized_or_unknown_track"


def test_json_artifact_registry_verifies_checksum_and_versions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = ArtifactStore(tmp_path)
    bundle_id, digest = store.publish({"dataset_digest": "fixture"})
    assert store.load()["dataset_digest"] == "fixture"
    path = tmp_path / "artifacts" / "ml" / "bundles" / f"{bundle_id}.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ArtifactUnavailableError, match="checksum"):
        store.load()
    assert len(digest) == 64


def test_feature_builder_rejects_partial_weather_missing_state_and_ood() -> None:
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    assert CanonicalFeatureBuilder(session).feature_for_lap(driver_id, 4).feature is not None

    missing_age_laps = tuple(
        replace(lap, tyre_life_laps=None)
        if str(lap.driver_id) == driver_id and lap.lap_number == 4
        else lap
        for lap in session.laps
    )
    missing_age = CanonicalFeatureBuilder(replace(session, laps=missing_age_laps)).feature_for_lap(
        driver_id, 4
    )
    assert missing_age.eligibility.reason == "missing_tyre_age"

    missing_weather = CanonicalFeatureBuilder(
        replace(session, weather=(replace(session.weather[0], track_temperature_c=None),))
    ).feature_for_lap(driver_id, 4)
    assert missing_weather.eligibility.reason == "missing_weather_fields"

    missing_position = CanonicalFeatureBuilder(
        replace(
            session,
            race_positions=tuple(
                row for row in session.race_positions if str(row.driver_id) != driver_id
            ),
        )
    ).feature_for_lap(driver_id, 4)
    assert missing_position.eligibility.reason == "missing_race_position"

    extreme_age_laps = tuple(
        replace(lap, tyre_life_laps=101)
        if str(lap.driver_id) == driver_id and lap.lap_number == 4
        else lap
        for lap in session.laps
    )
    extreme_age = CanonicalFeatureBuilder(replace(session, laps=extreme_age_laps)).feature_for_lap(
        driver_id, 4
    )
    assert extreme_age.eligibility.reason == "out_of_distribution:tyre_age_laps"


def test_feature_builder_rejects_partially_wet_restart_and_lapped_boundaries() -> None:
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    wet_then_dry = replace(
        session,
        weather=session.weather
        + (
            replace(session.weather[0], session_time_ms=315_000, rainfall=True),
            replace(session.weather[0], session_time_ms=359_000, rainfall=False),
        ),
    )
    wet = CanonicalFeatureBuilder(wet_then_dry).feature_for_lap(driver_id, 4)
    assert wet.eligibility.reason == "wet_or_unknown_weather"

    restart = replace(
        session,
        race_control=(
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=170_000,
                message="RED FLAG",
                track_status=TrackStatus.RED_FLAG,
                provenance=_provenance("fixture.ml-red-flag"),
            ),
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=180_000,
                message="Started",
                provenance=_provenance("fixture.ml-restart"),
            ),
        ),
    )
    builder = CanonicalFeatureBuilder(restart)
    assert builder.feature_for_lap(driver_id, 3).eligibility.reason == "restart_lap"
    assert builder.feature_for_lap(driver_id, 4).eligibility.reason == "insufficient_clean_history"

    other = session.drivers[1]
    leader_lap = replace(
        session.laps[-1],
        driver_id=other.driver_id,
        lap_number=5,
        lap_start_time_ms=250_000,
        lap_end_time_ms=350_000,
        lap_time_ms=100_000,
        provenance=_provenance("fixture.ml-lapped"),
    )
    lapped = CanonicalFeatureBuilder(
        replace(session, laps=session.laps + (leader_lap,))
    ).feature_for_lap(driver_id, 4)
    assert lapped.eligibility.reason == "lapped_driver_unsupported"


def test_session_finished_state_is_causal() -> None:
    session = _eligible_session()
    finished_at = 350_000
    finished = replace(
        session,
        race_control=(
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=finished_at,
                message="CHEQUERED FLAG",
                raw_status="CHEQUERED",
                category="Flag",
                source_kind="race_control_message",
                provenance=_provenance("fixture.ml-chequered"),
            ),
        ),
    )
    builder = CanonicalFeatureBuilder(finished)
    assert builder.session_is_finished_at(finished_at - 1) is False
    assert builder.session_is_finished_at(finished_at) is True


def test_inference_returns_unavailable_at_exact_session_finish(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    finished_at = 350_000
    finished = replace(
        session,
        race_control=(
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=finished_at,
                message="Finished",
                raw_status="Finished",
                source_kind="session_status",
                provenance=_provenance("fixture.ml-finished-status"),
            ),
        ),
    )
    engine = MLInferenceEngine(DownforceRepository(tmp_path), tmp_path)
    monkeypatch.setattr(engine, "_load_bundle", _valid_bundle)
    monkeypatch.setattr(engine, "_builder", lambda _session_id: CanonicalFeatureBuilder(finished))

    before = engine.predict(str(session.metadata.session_id), driver_id, finished_at - 1)
    at_finish = engine.predict(str(session.metadata.session_id), driver_id, finished_at)

    assert before["reason"] != "session_finished"
    assert at_finish["availability"] == "unavailable"
    assert at_finish["reason"] == "session_finished"
    assert at_finish["as_of"] == {"time_ms": finished_at, "lap": None}


def test_pit_targets_require_dry_green_in_and_out_laps() -> None:
    session = _eligible_session()
    driver_id = str(session.drivers[0].driver_id)
    adjusted_laps = tuple(
        replace(lap, lap_time_ms=100_000)
        if str(lap.driver_id) == driver_id and lap.lap_number == 5
        else replace(lap, lap_time_ms=101_000)
        if str(lap.driver_id) == driver_id and lap.lap_number == 6
        else lap
        for lap in session.laps
    )
    pit = PitStopRecord(
        session_id=session.metadata.session_id,
        driver_id=session.drivers[0].driver_id,
        stop_number=2,
        lap_number=5,
        pit_in_time_ms=440_000,
        pit_out_time_ms=460_000,
        pit_lane_duration_ms=20_000,
        provenance=_provenance("fixture.ml-pit"),
    )
    clean = replace(session, laps=adjusted_laps, pit_stops=session.pit_stops + (pit,))
    clean_rows = _pit_rows(
        CanonicalFeatureBuilder(clean), "dataset-fixture", DatasetSplit.TRAIN, Counter()
    )
    assert len(clean_rows) == 1
    assert clean_rows[0].race_control_regime == "green"
    assert clean_rows[0].weather_regime == "dry"

    neutralized_laps = tuple(
        replace(lap, raw_track_status="4")
        if str(lap.driver_id) == driver_id and lap.lap_number == 5
        else lap
        for lap in adjusted_laps
    )
    rejections: Counter[str] = Counter()
    rows = _pit_rows(
        CanonicalFeatureBuilder(replace(clean, laps=neutralized_laps)),
        "dataset-fixture",
        DatasetSplit.TRAIN,
        rejections,
    )
    assert rows == []
    assert rejections["pit:neutralized_or_unknown_track"] >= 1

    restart = replace(
        clean,
        race_control=(
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=350_000,
                message="RED FLAG",
                track_status=TrackStatus.RED_FLAG,
                provenance=_provenance("fixture.ml-pit-red-flag"),
            ),
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=360_000,
                message="Started",
                provenance=_provenance("fixture.ml-pit-restart"),
            ),
        ),
    )
    restart_rejections: Counter[str] = Counter()
    restart_rows = _pit_rows(
        CanonicalFeatureBuilder(restart),
        "dataset-fixture",
        DatasetSplit.TRAIN,
        restart_rejections,
    )
    assert restart_rows == []
    assert restart_rejections["pit:red_flag_restart_cycle"] == 1


def _valid_bundle() -> dict[str, object]:
    return {
        "dataset_digest": "0" * 64,
        "source_datasets": [],
        "feature_schema": feature_schema_payload(),
        "row_rejections": {},
        "split_sessions": {
            "train": ["session-train"],
            "validation": ["session-validation"],
            "calibration": ["session-calibration"],
            "test": ["session-test"],
        },
        "pace": {
            "selected": "rolling-median-baseline",
            "nonlinear": False,
            "model": None,
            "interval_half_width_ms": {"80": 800, "90": 1_200},
        },
        "tyre_degradation": {
            "selected": "zero-residual-baseline",
            "nonlinear": False,
            "model": None,
            "interval_half_width_ms": {"80": 800, "90": 1_200},
        },
        "pit_loss": {
            "selected": "circuit-median-with-global-fallback",
            "global_median_ms": 20_000,
            "circuit_medians_ms": {"Fixture Circuit": 20_000},
            "supported_circuits": ["Fixture Circuit"],
            "interval_half_width_ms": {"80": 2_000, "90": 3_000},
        },
    }


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("missing-model", "tyre degradation"),
        ("schema-mismatch", "feature schema"),
        ("missing-interval", "intervals"),
        ("negative-pit", "pit estimator"),
        ("overlapping-split", "overlaps"),
        ("huge-interval", "intervals"),
        ("unknown-estimator", "pace estimator"),
    ],
)
def test_semantically_invalid_artifacts_fail_status_cleanly(
    tmp_path,
    mutation: str,
    expected: str,  # type: ignore[no-untyped-def]
) -> None:
    payload = copy.deepcopy(_valid_bundle())
    if mutation == "missing-model":
        payload.pop("tyre_degradation")
    elif mutation == "schema-mismatch":
        schema = payload["feature_schema"]
        assert isinstance(schema, dict)
        features = schema["features"]
        assert isinstance(features, list)
        features.reverse()
    elif mutation == "missing-interval":
        pace = payload["pace"]
        assert isinstance(pace, dict)
        intervals = pace["interval_half_width_ms"]
        assert isinstance(intervals, dict)
        intervals.pop("80")
    elif mutation == "negative-pit":
        pit = payload["pit_loss"]
        assert isinstance(pit, dict)
        pit["global_median_ms"] = -1
    elif mutation == "overlapping-split":
        splits = payload["split_sessions"]
        assert isinstance(splits, dict)
        splits["test"] = ["session-train"]
    elif mutation == "huge-interval":
        pit = payload["pit_loss"]
        assert isinstance(pit, dict)
        pit["interval_half_width_ms"] = {"80": 1e308, "90": 1e308}
    else:
        pace = payload["pace"]
        assert isinstance(pace, dict)
        pace["model"] = ridge_to_dict(
            RidgeModel(
                means=(0.0,) * 15,
                scales=(1.0,) * 15,
                coefficients=(0.0,) * 15,
                intercept=90_000,
                regularization=1.0,
            )
        )
        pace["selected"] = "garbage"
    ArtifactStore(tmp_path).publish(payload)
    status = MLInferenceEngine(DownforceRepository(tmp_path), tmp_path).status()
    assert status["availability"] == "unavailable"
    assert expected in str(status["reason"])


def test_ridge_serialization_reload_is_prediction_equivalent() -> None:
    model = RidgeModel(
        means=(1.0, 2.0),
        scales=(2.0, 4.0),
        coefficients=(3.0, -2.0),
        intercept=10.0,
        regularization=25.0,
    )
    reloaded = ridge_from_dict(ridge_to_dict(model))
    assert reloaded == model
    assert reloaded.predict((5.0, 10.0)) == model.predict((5.0, 10.0))
