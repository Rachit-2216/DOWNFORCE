from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from downforce_core.domain import (
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
)
from downforce_core.domain.enums import TrackStatus, TyreCompound
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError
from downforce_core.ml.features import CanonicalFeatureBuilder
from downforce_core.replay import ReplayEngine, build_timeline
from downforce_core.strategy.composition import CompositionPrediction, ModelComposition
from downforce_core.strategy.contracts import (
    DriverSimulationState,
    PitAction,
    ScenarioAssumptions,
    SimulationState,
    Strategy,
    validate_strategy,
)
from downforce_core.strategy.engine import (
    StrategyEngine,
    StrategyUnavailableError,
    _PathOutcome,
    _quantile_error,
)
from test_ml import _eligible_session
from test_replay_engine import _provenance


class _ConstantComposition:
    model_version = "fixture"
    dataset_digest = "fixture"
    pit_width80 = 0.0
    pit_width90 = 0.0

    def lap_prediction(self, **_kwargs: object) -> CompositionPrediction:
        return CompositionPrediction(80_000.0, 0.0, 0.0, True)

    def pit_estimate(self, _circuit: str) -> tuple[float, float, float]:
        return 20_000.0, 20_000.0, 20_000.0


class _PitSensitiveComposition(_ConstantComposition):
    pit_width80 = 15_000.0
    pit_width90 = 20_000.0

    def lap_prediction(self, **kwargs: object) -> CompositionPrediction:
        compound = kwargs["compound"]
        mean = 70_000.0 if compound is TyreCompound.MEDIUM else 80_000.0
        return CompositionPrediction(mean, 0.0, 0.0, False)

    def pit_estimate(self, _circuit: str) -> tuple[float, float, float]:
        return 25_000.0, 5_000.0, 45_000.0


class _FastStopHorizonComposition(_ConstantComposition):
    def lap_prediction(self, **kwargs: object) -> CompositionPrediction:
        mean = 60_000.0 if kwargs["compound"] is TyreCompound.MEDIUM else 80_000.0
        return CompositionPrediction(mean, 0.0, 0.0, True)

    def pit_estimate(self, _circuit: str) -> tuple[float, float, float]:
        return 0.0, 0.0, 0.0


class _AnchorComposition(_ConstantComposition):
    def lap_prediction(self, **kwargs: object) -> CompositionPrediction:
        return CompositionPrediction(float(kwargs["anchor_ms"]), 0.0, 0.0, False)


def _driver(identity: str, *, laps: int, pace: float, position: int) -> DriverSimulationState:
    return DriverSimulationState(
        driver_id=identity,
        abbreviation=identity,
        status="active",
        laps_completed=laps,
        next_completion_time_ms=100_000.0,
        current_lap_elapsed_ms=0.0,
        compound=TyreCompound.HARD,
        tyre_age_laps=5.0,
        stint_number=1,
        stops_completed=0,
        source_position=position,
        next_actionable_lap=laps + 2,
        pace_anchor_ms=pace,
        feature_values=(
            1.0,
            5.0,
            1.0,
            0.0,
            float(position),
            pace,
            pace,
            0.0,
            0.0,
            30.0,
            20.0,
            50.0,
            0.0,
            0.0,
            1.0,
        ),
        feature_compound=TyreCompound.HARD,
        used_compounds=(TyreCompound.HARD,),
        model_supported=True,
    )


def _state(*, target_laps: int = 1, rival_laps: int = 1) -> SimulationState:
    return SimulationState(
        session_id="fixture",
        source_cursor_ms=100_000,
        reference_lap=1,
        scheduled_total_laps=4,
        scheduled_distance_source="explicit_override",
        circuit="fixture",
        drivers=(
            _driver("AAA", laps=target_laps, pace=80_000.0, position=1),
            _driver("BBB", laps=rival_laps, pace=82_000.0, position=2),
        ),
        seed=2216,
        model_version="fixture",
        dataset_digest="fixture",
        assumptions=(),
    )


def _engine() -> StrategyEngine:
    engine = object.__new__(StrategyEngine)
    engine.composition = _ConstantComposition()  # type: ignore[assignment]
    return engine


def test_pace_and_tyre_composition_uses_one_residual_distribution() -> None:
    root = Path(__file__).resolve().parents[4]
    composition = ModelComposition(ArtifactStore(root))
    values = (
        30.0,
        13.0,
        2.0,
        1.0,
        1.0,
        90_000.0,
        90_100.0,
        200.0,
        0.0,
        30.0,
        20.0,
        50.0,
        0.0,
        0.0,
        1.0,
    )
    prediction = composition.lap_prediction(
        anchor_ms=90_100.0,
        source_values=values,
        lap=31,
        tyre_age=14.0,
        stint=2,
        pit_count=1,
        compound=TyreCompound.HARD,
        local_anchor_age=13.0,
    )
    assert prediction.error_width_80_ms == pytest.approx(composition.tyre_width80)
    assert prediction.error_width_90_ms == pytest.approx(composition.tyre_width90)
    assert prediction.mean_lap_ms != 90_100.0
    assert 45_000 < prediction.mean_lap_ms < 330_000


def test_composition_rejects_a_frozen_feature_dimension_mismatch() -> None:
    root = Path(__file__).resolve().parents[4]
    composition = ModelComposition(ArtifactStore(root))
    model = composition.tyre["model"]
    assert isinstance(model, dict)
    composition.tyre = {
        **composition.tyre,
        "model": {
            **model,
            "coefficients": model["coefficients"][:-1],
            "means": model["means"][:-1],
            "scales": model["scales"][:-1],
        },
    }

    with pytest.raises(ArtifactUnavailableError, match="frozen feature schema"):
        composition.lap_prediction(
            anchor_ms=90_100.0,
            source_values=(
                30.0,
                13.0,
                2.0,
                1.0,
                1.0,
                90_000.0,
                90_100.0,
                200.0,
                0.0,
                30.0,
                20.0,
                50.0,
                0.0,
                0.0,
                1.0,
            ),
            lap=31,
            tyre_age=14.0,
            stint=2,
            pit_count=1,
            compound=TyreCompound.HARD,
            local_anchor_age=13.0,
        )


def test_pit_loss_is_charged_exactly_once() -> None:
    engine = _engine()
    state = _state()
    scenario = ScenarioAssumptions(4, pit_loss_mode="point")
    stay_out = engine._path(state, "AAA", Strategy("stay", "Stay out"), scenario, 0)
    stop = engine._path(
        state,
        "AAA",
        Strategy("stop", "Stop", (PitAction(3, TyreCompound.MEDIUM),)),
        scenario,
        0,
    )
    assert stop.final_time_ms - stay_out.final_time_ms == pytest.approx(20_000.0)


def test_seeded_paths_are_reproducible_and_seed_sensitive() -> None:
    first = _quantile_error(800.0, 1_100.0, 2216, 1, "AAA", 20)
    second = _quantile_error(800.0, 1_100.0, 2216, 1, "AAA", 20)
    changed = _quantile_error(800.0, 1_100.0, 2217, 1, "AAA", 20)
    assert first == second
    assert first != changed
    assert abs(first) <= 2_200.0


def test_lapped_classification_uses_completed_laps_before_time() -> None:
    engine = _engine()
    state = _state(target_laps=0, rival_laps=1)
    outcome = engine._path(state, "AAA", Strategy("stay", "Stay out"), ScenarioAssumptions(4), 0)
    assert outcome.position == 2
    assert outcome.classified_laps < 4


def test_priority_queue_handles_overtakes_pits_ties_and_multi_lap_deficits() -> None:
    engine = object.__new__(StrategyEngine)
    engine.composition = _AnchorComposition()  # type: ignore[assignment]
    state = replace(
        _state(),
        drivers=(
            _driver("AAA", laps=1, pace=75_000.0, position=2),
            _driver("BBB", laps=1, pace=80_000.0, position=1),
        ),
    )
    overtake = engine._path(state, "AAA", Strategy("stay", "Stay out"), ScenarioAssumptions(4), 0)
    pit_loss = engine._path(
        state,
        "AAA",
        Strategy("stop", "Stop", (PitAction(3, TyreCompound.MEDIUM),)),
        ScenarioAssumptions(4, pit_loss_mode="point"),
        0,
    )
    assert overtake.position == 1
    assert pit_loss.position == 2

    tied = replace(
        state,
        drivers=(
            _driver("AAA", laps=1, pace=80_000.0, position=2),
            _driver("BBB", laps=1, pace=80_000.0, position=1),
        ),
    )
    first = engine._path(tied, "AAA", Strategy("stay", "Stay out"), ScenarioAssumptions(4), 0)
    second = engine._path(tied, "BBB", Strategy("stay", "Stay out"), ScenarioAssumptions(4), 0)
    assert {first.position, second.position} == {1, 2}
    assert first.position == 2

    two_laps_down = replace(
        state,
        drivers=(
            _driver("AAA", laps=0, pace=80_000.0, position=2),
            _driver("BBB", laps=2, pace=80_000.0, position=1),
        ),
    )
    lapped = engine._path(
        two_laps_down,
        "AAA",
        Strategy("stay", "Stay out"),
        ScenarioAssumptions(4),
        0,
    )
    assert lapped.position == 2
    assert lapped.classified_laps == 2


def test_strategy_validation_rejects_past_pits_and_optional_compound_rule() -> None:
    target = _state().drivers[0]
    with pytest.raises(ValueError, match="not-yet-started"):
        validate_strategy(
            Strategy("past", "Past", (PitAction(1, TyreCompound.MEDIUM),)),
            driver=target,
            scheduled_total_laps=4,
            require_two_compounds=False,
        )
    with pytest.raises(ValueError, match="two dry compounds"):
        validate_strategy(
            Strategy("same", "Same", (PitAction(3, TyreCompound.HARD),)),
            driver=target,
            scheduled_total_laps=4,
            require_two_compounds=True,
        )


def test_lapped_driver_actions_use_own_lap_and_prior_compounds_count() -> None:
    target = replace(
        _driver("AAA", laps=28, pace=80_000.0, position=10),
        next_actionable_lap=30,
        used_compounds=(TyreCompound.SOFT, TyreCompound.HARD),
    )
    validate_strategy(
        Strategy("own-lap", "Own lap", (PitAction(30, TyreCompound.MEDIUM),)),
        driver=target,
        scheduled_total_laps=35,
        require_two_compounds=True,
    )
    with pytest.raises(ValueError, match="not-yet-started"):
        validate_strategy(
            Strategy("started", "Started", (PitAction(29, TyreCompound.MEDIUM),)),
            driver=target,
            scheduled_total_laps=35,
            require_two_compounds=False,
        )


def test_partial_lap_uses_only_remaining_time_and_fails_outside_model_support() -> None:
    engine = _engine()
    state = _state()
    partial = replace(
        state,
        source_cursor_ms=170_000,
        drivers=tuple(replace(driver, current_lap_elapsed_ms=70_000.0) for driver in state.drivers),
    )
    outcome = engine._path(partial, "AAA", Strategy("stay", "Stay out"), ScenarioAssumptions(4), 0)
    assert outcome.final_time_ms == pytest.approx(340_000.0)

    unsupported = replace(
        state,
        source_cursor_ms=181_000,
        drivers=tuple(replace(driver, current_lap_elapsed_ms=81_000.0) for driver in state.drivers),
    )
    with pytest.raises(StrategyUnavailableError, match="partial_lap_out_of_model_support"):
        engine._path(
            unsupported,
            "AAA",
            Strategy("stay", "Stay out"),
            ScenarioAssumptions(4),
            0,
        )


def test_strategy_fails_closed_at_authoritative_session_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    context = SimpleNamespace(
        session=SimpleNamespace(),
        replay=SimpleNamespace(state_at=lambda _cursor: SimpleNamespace()),
        builder=SimpleNamespace(session_is_finished_at=lambda _cursor: True),
    )
    monkeypatch.setattr(engine, "_context", lambda _session_id: context)
    with pytest.raises(StrategyUnavailableError, match="session_finished"):
        engine.build_state("fixture", "AAA", 350_000, ScenarioAssumptions(4), 2216)


def test_scheduled_distance_uses_only_valid_fastf1_published_metadata() -> None:
    assert (
        StrategyEngine._published_scheduled_laps(
            SimpleNamespace(provider_name="fastf1", provider_metadata={"total_laps": 70})
        )
        == 70
    )
    assert (
        StrategyEngine._published_scheduled_laps(
            SimpleNamespace(provider_name="other", provider_metadata={"total_laps": 70})
        )
        is None
    )
    assert (
        StrategyEngine._published_scheduled_laps(
            SimpleNamespace(provider_name="fastf1", provider_metadata={"total_laps": True})
        )
        is None
    )


def test_candidate_generator_uses_driver_own_laps_and_avoids_last_lap_stops() -> None:
    engine = _engine()
    late = engine.generate_candidates(
        driver_laps_completed=68,
        scheduled_total_laps=70,
        current_compound=TyreCompound.HARD,
    )
    assert [candidate.strategy_id for candidate in late] == ["stay-out"]

    lapped = engine.generate_candidates(
        driver_laps_completed=28,
        scheduled_total_laps=35,
        current_compound=TyreCompound.HARD,
    )
    actions = [action for candidate in lapped for action in candidate.actions]
    assert actions
    assert all(30 <= action.lap <= 35 for action in actions)


@pytest.mark.parametrize("boundary_lap", [4, 5])
def test_simulation_is_unchanged_when_strictly_future_records_are_mutated(
    monkeypatch: pytest.MonkeyPatch, boundary_lap: int
) -> None:
    session = _eligible_session()
    target_id = session.drivers[0].driver_id
    background_id = session.drivers[1].driver_id
    background_laps = tuple(
        LapRecord(
            session_id=session.metadata.session_id,
            driver_id=background_id,
            lap_number=number,
            lap_start_time_ms=91_000 + (number - 2) * 90_000,
            lap_end_time_ms=179_000 + (number - 2) * 90_000,
            lap_time_ms=90_000,
            stint_number=1,
            compound=TyreCompound.HARD,
            tyre_life_laps=float(number + 2),
            is_accurate=True,
            is_generated=False,
            is_deleted=False,
            raw_track_status="1",
            provenance=_provenance("fixture.strategy-background-laps"),
        )
        for number in range(2, 7)
    )
    background_positions = tuple(
        RacePositionRecord(
            session_id=session.metadata.session_id,
            driver_id=background_id,
            session_time_ms=lap.lap_end_time_ms or 0,
            position=2,
            lap_number=lap.lap_number,
            provenance=_provenance("fixture.strategy-background-positions"),
        )
        for lap in background_laps
    )
    session = replace(
        session,
        laps=session.laps + background_laps,
        race_positions=session.race_positions + background_positions,
    )
    cursor_ms = boundary_lap * 90_000
    future_pit = PitStopRecord(
        session_id=session.metadata.session_id,
        driver_id=background_id,
        stop_number=2,
        lap_number=boundary_lap + 1,
        pit_in_time_ms=cursor_ms + 30_000,
        pit_out_time_ms=cursor_ms + 50_000,
        pit_lane_duration_ms=20_000,
        provenance=_provenance("fixture.strictly-future-pit"),
    )
    mutated = replace(
        session,
        classifications=tuple(reversed(session.classifications)),
        laps=tuple(
            replace(lap, lap_time_ms=240_000, tyre_life_laps=99.0)
            if lap.lap_end_time_ms is not None and lap.lap_end_time_ms > cursor_ms
            else lap
            for lap in session.laps
        ),
        pit_stops=session.pit_stops + (future_pit,),
        weather=session.weather
        + (replace(session.weather[0], session_time_ms=cursor_ms + 10_000, rainfall=True),),
        race_control=session.race_control
        + (
            RaceControlRecord(
                session_id=session.metadata.session_id,
                session_time_ms=cursor_ms + 10_000,
                message="RED FLAG",
                track_status=TrackStatus.RED_FLAG,
                provenance=_provenance("fixture.strictly-future-control"),
            ),
        ),
        race_positions=session.race_positions
        + (
            RacePositionRecord(
                session_id=session.metadata.session_id,
                driver_id=target_id,
                session_time_ms=cursor_ms + 10_000,
                position=20,
                lap_number=boundary_lap,
                provenance=_provenance("fixture.strictly-future-position"),
            ),
        ),
    )

    def run(value: object) -> dict[str, object]:
        engine = _engine()
        canonical = value
        context = SimpleNamespace(
            session=canonical,
            replay=ReplayEngine(canonical, build_timeline(canonical)),
            builder=CanonicalFeatureBuilder(canonical),
        )
        monkeypatch.setattr(engine, "_context", lambda _session_id: context)
        return engine.simulate(
            session_id=str(session.metadata.session_id),
            driver_id=str(target_id),
            cursor_ms=cursor_ms,
            strategy=Strategy("stay", "Stay out"),
            scenario=ScenarioAssumptions(7),
            simulations=10,
            seed=2216,
        )

    assert run(mutated) == run(session)


def test_long_horizon_stay_out_leader_is_not_recommended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    state = _state()
    monkeypatch.setattr(engine, "build_state", lambda *_args, **_kwargs: state)
    result = engine.compare(
        session_id="fixture",
        driver_id="AAA",
        cursor_ms=100_000,
        strategies=(
            Strategy("stay", "Stay out"),
            Strategy("stop", "Stop", (PitAction(3, TyreCompound.MEDIUM),)),
        ),
        scenario=ScenarioAssumptions(4, pit_loss_mode="point"),
        simulations=10,
        seed=2216,
    )
    ranking = result["ranking"]
    assert isinstance(ranking, dict)
    assert ranking["leading_strategy_id"] == "stay"
    assert ranking["long_horizon_limited"] is True
    assert ranking["recommended_strategy_id"] is None
    assert ranking["status"] == "NO CLEAR PREFERENCE"


def test_long_horizon_stop_leader_is_also_not_recommended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object.__new__(StrategyEngine)
    engine.composition = _FastStopHorizonComposition()  # type: ignore[assignment]
    state = _state()
    monkeypatch.setattr(engine, "build_state", lambda *_args, **_kwargs: state)
    result = engine.compare(
        session_id="fixture",
        driver_id="AAA",
        cursor_ms=100_000,
        strategies=(
            Strategy("stay", "Stay out"),
            Strategy("stop", "Stop", (PitAction(3, TyreCompound.MEDIUM),)),
        ),
        scenario=ScenarioAssumptions(4, pit_loss_mode="point"),
        simulations=10,
        seed=2216,
    )
    ranking = result["ranking"]
    assert isinstance(ranking, dict)
    assert ranking["leading_strategy_id"] == "stop"
    assert ranking["long_horizon_limited"] is True
    assert ranking["recommended_strategy_id"] is None


def test_incomplete_background_field_blocks_a_clear_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    state = replace(_state(), excluded_drivers=(("RET", "terminal_retired"),))
    monkeypatch.setattr(engine, "build_state", lambda *_args, **_kwargs: state)

    def paths(
        _state_value: SimulationState,
        _driver_id: str,
        strategy: Strategy,
        _scenario: ScenarioAssumptions,
        simulations: int,
    ) -> list[_PathOutcome]:
        position = 1 if strategy.strategy_id == "left" else 2
        return [
            _PathOutcome(position, 4, 300_000.0 + position, 300_000.0, False)
            for _index in range(simulations)
        ]

    monkeypatch.setattr(engine, "_run_paths", paths)
    result = engine.compare(
        session_id="fixture",
        driver_id="AAA",
        cursor_ms=100_000,
        strategies=(Strategy("left", "Left"), Strategy("right", "Right")),
        scenario=ScenarioAssumptions(4),
        simulations=10,
        seed=2216,
    )
    ranking = result["ranking"]
    assert isinstance(ranking, dict)
    assert ranking["input_data_limited"] is True
    assert ranking["status"] == "NO CLEAR PREFERENCE"
    assert ranking["recommended_strategy_id"] is None


@pytest.mark.parametrize(
    ("wins", "expected_status"),
    [
        (599, "NO CLEAR PREFERENCE"),
        (600, "PREFERRED UNDER CURRENT ASSUMPTIONS"),
        (601, "PREFERRED UNDER CURRENT ASSUMPTIONS"),
    ],
)
def test_recommendation_threshold_edges_are_exact_and_deterministic(
    monkeypatch: pytest.MonkeyPatch, wins: int, expected_status: str
) -> None:
    engine = _engine()
    state = _state()
    monkeypatch.setattr(engine, "build_state", lambda *_args, **_kwargs: state)
    left = [
        _PathOutcome(1 if index < wins else 2, 4, 300_000.0, 300_000.0, False)
        for index in range(1_000)
    ]
    right = [
        _PathOutcome(2 if index < wins else 1, 4, 301_000.0, 300_000.0, False)
        for index in range(1_000)
    ]

    def paths(
        _state_value: SimulationState,
        _driver_id: str,
        strategy: Strategy,
        _scenario: ScenarioAssumptions,
        _simulations: int,
    ) -> list[_PathOutcome]:
        return left if strategy.strategy_id == "left" else right

    monkeypatch.setattr(engine, "_run_paths", paths)
    result = engine.compare(
        session_id="fixture",
        driver_id="AAA",
        cursor_ms=100_000,
        strategies=(Strategy("left", "Left"), Strategy("right", "Right")),
        scenario=ScenarioAssumptions(4),
        simulations=1_000,
        seed=2216,
    )
    ranking = result["ranking"]
    assert isinstance(ranking, dict)
    assert ranking["probability_leading_beats_runner_up"] == wins / 1_000
    assert ranking["status"] == expected_status


def test_pit_loss_bound_flip_forces_no_clear_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object.__new__(StrategyEngine)
    engine.composition = _PitSensitiveComposition()  # type: ignore[assignment]
    state = _state()
    monkeypatch.setattr(engine, "build_state", lambda *_args, **_kwargs: state)
    result = engine.compare(
        session_id="fixture",
        driver_id="AAA",
        cursor_ms=100_000,
        strategies=(
            Strategy("stay", "Stay out"),
            Strategy("stop", "Stop", (PitAction(3, TyreCompound.MEDIUM),)),
        ),
        scenario=ScenarioAssumptions(4, pit_loss_mode="point"),
        simulations=10,
        seed=2216,
    )
    ranking = result["ranking"]
    sensitivity = result["pit_loss_sensitivity"]
    assert isinstance(ranking, dict)
    assert isinstance(sensitivity, dict)
    assert sensitivity["lower-90"] == "stop"
    assert sensitivity["upper-90"] == "stay"
    assert ranking["pit_loss_sensitive"] is True
    assert ranking["recommended_strategy_id"] is None
    assert ranking["status"] == "NO CLEAR PREFERENCE"
