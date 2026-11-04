from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from app.core.config import Settings
from app.main import create_app
from app.services.telemetry_service import TelemetryService
from downforce_core.detailed.storage import DetailedStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def publish(
    tmp_path: Path,
    *,
    speed: int = 180,
    season: int = 2024,
    deleted: bool = False,
    missing_car: bool = False,
    corners: bool = False,
) -> tuple[DetailedStore, str, str]:
    store = DetailedStore(tmp_path)
    identity = f"{season}-01-race"
    laps = pa.table(
        {
            "driver_number": ["44", "44", "1"],
            "lap_number": [1.0, 2.0, 1.0],
            "lap_start_time": pa.array(
                [10_000_000_000, 20_000_000_000, 10_000_000_000], pa.duration("ns")
            ),
            "lap_time": pa.array([10_000_000_000] * 3, pa.duration("ns")),
            "lap_start_timestamp": pa.array(
                [1_700_000_010_000_000_000, 1_700_000_020_000_000_000, 1_700_000_010_000_000_000],
                pa.timestamp("ns"),
            ),
            "is_accurate": [True] * 3,
            "deleted": [deleted, False, False],
            "provider_generated": [False] * 3,
            "compound": ["SOFT"] * 3,
            "stint": [1.0] * 3,
        }
    )
    car = pa.table(
        {
            "session_time": pa.array([i * 1_000_000_000 for i in range(9, 32)], pa.duration("ns")),
            "session_time_ms": [i * 1000.0 for i in range(9, 32)],
            "speed": [speed] * 23,
            "gear": [4] * 23,
            "brake": [False] * 23,
            "drs": [0] * 23,
            "provider_source": ["car"] * 23,
        }
    )
    tables = {"laps": laps, "car-data-1": car}
    if not missing_car:
        tables["car-data-44"] = car
    if corners:
        tables["corners"] = pa.table(
            {
                "Number": [1],
                "Letter": ["A"],
                "Distance": [250.0],
                "X": [1.0],
                "Y": [2.0],
                "Angle": [30.0],
            }
        )
    revision = store.publish(
        identity,
        tables,
        {
            "session": {
                "session_id": identity,
                "season": season,
                "session_type": "race",
                "event_name": "Fixture Grand Prix",
                "circuit": "Fixture Circuit",
                "state": "completed",
            }
        },
    )
    return store, identity, revision


def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(Settings(environment="test", log_level="CRITICAL", project_root=tmp_path))
    )


def test_empty_production_catalog_is_honest(tmp_path: Path) -> None:
    response = client(tmp_path).get("/api/v1/telemetry/sessions")
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["source"] == "production-canonical"
    assert ".downforce" not in response.text


def test_catalog_and_laps_scope_native_production_revision(tmp_path: Path) -> None:
    _, identity, revision = publish(tmp_path)
    api = client(tmp_path)
    catalog = api.get(
        "/api/v1/telemetry/sessions", params={"season": 2024, "driver": "44", "limit": 1}
    ).json()
    assert catalog["total"] == 1
    assert catalog["items"][0]["revision"] == revision
    assert api.get("/api/v1/telemetry/sessions", params={"season": 2026}).json()["total"] == 0
    response = api.get(f"/api/v1/telemetry/sessions/{identity}/laps", params={"driver": "44"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["revision"] == revision
    assert [lap["lap_number"] for lap in payload["items"]] == [1, 2]
    assert payload["items"][0]["qualifying_phase"] is None
    assert payload["items"][0]["lap_time_seconds"] == 10.0


def test_trace_is_selected_lap_only_and_old_revision_remains_pinned(tmp_path: Path) -> None:
    _, identity, old = publish(tmp_path)
    api = client(tmp_path)
    path = f"/api/v1/telemetry/sessions/{identity}/trace"
    params: dict[str, str | int] = {
        "driver": "44",
        "lap": 1,
        "revision": old,
        "channels": "speed_kmh,gear,drs_raw",
    }
    response = api.get(path, params=params)
    assert response.status_code == 200, response.text
    value = response.json()
    assert len(value["distance_m"]) == 11
    assert len(value["source_samples"]["session_time_ns"]) == 11
    assert value["source_samples"]["source_sensors"] == ["car"] * 11
    assert value["channels"]["speed_kmh"] == [180] * 11
    assert value["source_samples"]["provenance"] == "PROVIDER_NATIVE"
    assert value["drs_interpretation"] == "UNKNOWN_PROVIDER_CODE"
    _, _, new = publish(tmp_path, speed=200)
    assert old != new
    assert api.get(path, params=params).json()["channels"]["speed_kmh"][0] == 180
    assert (
        api.get(path, params={"driver": "44", "lap": 1}).json()["channels"]["speed_kmh"][0] == 200
    )


def test_comparison_corners_and_microsectors_are_typed_derived(tmp_path: Path) -> None:
    _, identity, revision = publish(tmp_path)
    api = client(tmp_path)
    base = f"/api/v1/telemetry/sessions/{identity}"
    params: dict[str, str | int] = {
        "driver_a": "44",
        "lap_a": 1,
        "driver_b": "1",
        "lap_b": 1,
        "revision": revision,
    }
    response = api.get(base + "/compare", params=params)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["delta_seconds"] == [0.0] * 501
    assert value["finish_line_agreement_applicable"]
    assert value["a"]["driver"] == "44"
    windows = api.get(base + "/corners", params=params).json()
    assert windows["items"][0]["window"]["kind"] == "DISTANCE_WINDOW"
    assert windows["items"][0]["brake_label"] == "BRAKE_ACTIVE"
    bins = api.get(base + "/microsectors", params={**params, "count": 5}).json()
    assert len(bins["sectors"]) == 5
    assert bins["sectors"][0]["label"] == "DERIVED_DISTANCE_BIN_NOT_FIA_SECTOR"


def test_provider_corner_evidence_is_preserved(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path, corners=True)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/corners",
        params={"driver_a": "44", "lap_a": 1, "driver_b": "44", "lap_b": 2},
    )
    assert response.status_code == 200, response.text
    marker = response.json()["items"][0]["window"]["marker"]
    assert marker["number"] == 1 and marker["letter"] == "A"
    assert marker["angle"] == 30.0 and marker["distance_m"] == 250.0


@pytest.mark.parametrize(
    "params",
    [
        {"max_points": 20001},
        {"spacing_m": "NaN"},
        {"channels": "secret"},
        {"lap_a": 0},
        {"driver_a": "../44"},
    ],
)
def test_requests_validate_bounds_and_finite_settings(
    tmp_path: Path, params: dict[str, str | int]
) -> None:
    _, identity, _ = publish(tmp_path)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/compare",
        params={"driver_a": "44", "lap_a": 1, "driver_b": "44", "lap_b": 2, **params},
    )
    assert response.status_code == 422
    assert "error" in response.json()


def test_unavailable_data_and_selection_errors_are_safe(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path, missing_car=True)
    api = client(tmp_path)
    path = f"/api/v1/telemetry/sessions/{identity}/trace"
    missing = api.get(path, params={"driver": "44", "lap": 1})
    assert missing.status_code == 409
    unknown = api.get(path, params={"driver": "1", "lap": 999})
    assert unknown.status_code == 404
    session = api.get("/api/v1/telemetry/sessions/missing/trace", params={"driver": "1", "lap": 1})
    assert session.status_code == 404
    for response in (missing, unknown, session):
        assert ".downforce" not in response.text and "Traceback" not in response.text


def test_deleted_laps_require_explicit_optin_and_limits_never_downsample(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path, deleted=True)
    api = client(tmp_path)
    path = f"/api/v1/telemetry/sessions/{identity}/trace"
    assert api.get(path, params={"driver": "44", "lap": 1}).status_code == 409
    assert (
        api.get(path, params={"driver": "44", "lap": 1, "allow_deleted": True}).status_code == 200
    )
    assert api.get(path, params={"driver": "44", "lap": 2, "max_points": 2}).status_code == 413


def test_2026_drs_parser_default_not_rendered_as_off(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path, season=2026)
    value = (
        client(tmp_path)
        .get(f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1})
        .json()
    )
    assert value["channels"]["drs_raw"] == [None] * 11
    assert value["channel_availability"]["drs_raw"] == "not-recorded-provider-parser-default"


def test_cache_is_bounded_revision_keyed_and_defensively_copied(tmp_path: Path) -> None:
    publish(tmp_path)
    api = client(tmp_path)
    service = cast(TelemetryService, cast(FastAPI, api.app).state.telemetry_service)
    first = service.trace("2024-01-race", "44", 1)
    first.channels["speed_kmh"][0] = -1
    second = service.trace("2024-01-race", "44", 1)
    assert second.channels["speed_kmh"][0] == 180
    assert service.cache_entries <= service.cache_limit


def replace_table(store: DetailedStore, identity: str, name: str, table: pa.Table) -> str:
    manifest = store.manifest(identity)
    tables = {
        key: store.read_table(identity, key, revision=manifest["revision"])
        for key in manifest["tables"]
    }
    tables[name] = table
    return store.publish(identity, tables, manifest["metadata"])


def test_ambiguous_lap_returns_conflict_not_an_arbitrary_trace(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    laps = store.read_table(identity, "laps")
    replace_table(store, identity, "laps", pa.concat_tables([laps, laps.slice(0, 1)]))
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1}
    )
    assert response.status_code == 409


def test_constant_zero_provider_car_is_honestly_unusable(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path, speed=0)
    car = store.read_table(identity, "car-data-44")
    car = car.set_column(car.column_names.index("gear"), "gear", pa.array([0] * car.num_rows))
    car = car.append_column("rpm", pa.array([0] * car.num_rows)).append_column(
        "throttle", pa.array([0] * car.num_rows)
    )
    replace_table(store, identity, "car-data-44", car)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "allzero_source"
    assert store.read_table(identity, "car-data-44")["speed"].to_pylist() == [0] * 23


def test_reserved_session_and_nonfinite_raw_metadata_are_safe(tmp_path: Path) -> None:
    api = client(tmp_path)
    response = api.get("/api/v1/telemetry/sessions/con/trace", params={"driver": "44", "lap": 1})
    assert response.status_code == 422


def test_partial_trace_requires_optin_and_gap_masks_survive_comparison(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    car = store.read_table(identity, "car-data-44")
    cropped = car.slice(3, 8)  # starts 12s, ends19s: no fake boundaries
    replace_table(store, identity, "car-data-44", cropped)
    api = client(tmp_path)
    path = f"/api/v1/telemetry/sessions/{identity}/trace"
    assert api.get(path, params={"driver": "44", "lap": 1}).status_code == 409
    partial = api.get(path, params={"driver": "44", "lap": 1, "allow_partial": True})
    assert partial.status_code == 200, partial.text
    assert not partial.json()["start_covered"] and not partial.json()["finish_covered"]
    assert "partial_lap" in partial.json()["flags"]


@pytest.mark.parametrize(
    "flag,allow",
    [
        ("pit_in_time", "allow_pit_laps"),
        ("provider_generated", "allow_provider_generated"),
        ("is_accurate", "allow_unreliable"),
    ],
)
def test_sensitive_lap_flags_need_explicit_optin(tmp_path: Path, flag: str, allow: str) -> None:
    store, identity, _ = publish(tmp_path)
    laps = store.read_table(identity, "laps")
    values = (
        pa.array([1_000_000_000, None, None], pa.duration("ns"))
        if flag == "pit_in_time"
        else pa.array(
            [
                flag != "is_accurate",
                True if flag == "is_accurate" else False,
                True if flag == "is_accurate" else False,
            ]
        )
    )
    if flag in laps.column_names:
        laps = laps.set_column(laps.column_names.index(flag), flag, values)
    else:
        laps = laps.append_column(flag, values)
    replace_table(store, identity, "laps", laps)
    api = client(tmp_path)
    path = f"/api/v1/telemetry/sessions/{identity}/trace"
    assert api.get(path, params={"driver": "44", "lap": 1}).status_code == 409
    assert api.get(path, params={"driver": "44", "lap": 1, allow: True}).status_code == 200


def test_nonfinite_native_channels_preserved_in_store_masked_on_wire(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    car = store.read_table(identity, "car-data-44")
    car = car.append_column("rpm", pa.array([float("inf")] * car.num_rows))
    replace_table(store, identity, "car-data-44", car)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace",
        params={"driver": "44", "lap": 1, "channels": "rpm,speed_kmh"},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["channels"]["rpm"] == [None] * 11
    assert value["channel_valid_masks"]["rpm"] == [False] * 11
    assert value["source_samples"]["channel_valid_masks"]["rpm"] == [False] * 11


def test_interior_gap_is_masked_not_interpolated_into_certainty(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    car = store.read_table(identity, "car-data-44")
    car = pa.concat_tables([car.slice(0, 4), car.slice(9)])  # 12→18 seconds
    replace_table(store, identity, "car-data-44", car)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/compare",
        params={"driver_a": "44", "lap_a": 1, "driver_b": "1", "lap_b": 1},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert False in value["valid_mask"]
    assert None in value["delta_seconds"]


def test_missing_clock_refuses_provider_time_alias(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    car = store.read_table(identity, "car-data-44").rename_columns(
        ["provider_time", "provider_time_ms", "speed", "gear", "brake", "drs", "provider_source"]
    )
    replace_table(store, identity, "car-data-44", car)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1}
    )
    assert response.status_code == 409


def test_cache_setting_identity_and_eviction_are_bounded(tmp_path: Path) -> None:
    publish(tmp_path)
    service = cast(TelemetryService, cast(FastAPI, client(tmp_path).app).state.telemetry_service)
    from app.schemas.telemetry import TraceSettings

    for i in range(20):
        service.trace(
            "2024-01-race", "44", 1, settings=TraceSettings(max_edge_gap_seconds=1.0 + i / 100)
        )
    assert service.cache_entries == service.cache_limit
    only_speed = service.trace(
        "2024-01-race", "44", 1, settings=TraceSettings(channels="speed_kmh")
    )
    assert list(only_speed.channels) == ["speed_kmh"]


def test_date_clock_uses_verified_lap_origin_with_exact_nanoseconds(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    car = store.read_table(identity, "car-data-44").drop(["session_time", "session_time_ms"])
    car = car.append_column(
        "timestamp",
        pa.array(
            [1_700_000_000_000_000_000 + i * 1_000_000_000 for i in range(9, 32)],
            pa.timestamp("ns"),
        ),
    )
    replace_table(store, identity, "car-data-44", car)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_samples"]["session_time_ns"][0] == 10_000_000_000


def test_candidate_store_never_becomes_an_implicit_production_catalog(tmp_path: Path) -> None:
    candidate = DetailedStore(
        tmp_path, detailed_root=tmp_path / ".downforce" / "detailed" / ".study"
    )
    candidate.publish(
        "2024-01-race",
        {"laps": pa.table({"driver_number": ["44"], "lap_number": [1.0]})},
        {"session": {"season": 2024}},
    )
    assert client(tmp_path).get("/api/v1/telemetry/sessions").json()["total"] == 0


def test_explicit_detailed_store_injection_uses_only_its_configured_canonical_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "fixture-project"
    project.mkdir()
    store, identity, revision = publish(project)
    injected = TestClient(
        create_app(
            Settings(environment="test", log_level="CRITICAL", project_root=tmp_path),
            detailed_store=store,
        )
    )
    value = injected.get("/api/v1/telemetry/sessions").json()
    assert value["items"][0]["session_id"] == identity
    assert value["items"][0]["revision"] == revision


def test_absent_positions_weather_remain_explicit_not_fake_data(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path)
    value = (
        client(tmp_path)
        .get(f"/api/v1/telemetry/sessions/{identity}/trace", params={"driver": "44", "lap": 1})
        .json()
    )
    assert not value["context"]["position_available"]
    assert not value["context"]["weather_available"]
    assert value["channel_availability"]["x"] == "unavailable"
    assert value["channels"]["x"] == [None] * 11
    assert "x" not in value["source_samples"]["channels"]


def test_grid_point_limit_returns_413_without_silent_resampling(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/compare",
        params={"driver_a": "44", "lap_a": 1, "driver_b": "1", "lap_b": 1, "spacing_m": 0.001},
    )
    assert response.status_code == 413


def test_failed_revision_selection_never_uses_current_active_revision(tmp_path: Path) -> None:
    _, identity, _ = publish(tmp_path)
    response = client(tmp_path).get(
        f"/api/v1/telemetry/sessions/{identity}/trace",
        params={"driver": "44", "lap": 1, "revision": "f" * 64},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_microsector_count_below_math_minimum_is_422_before_reads(
    tmp_path: Path, count: int
) -> None:
    _, identity, _ = publish(tmp_path)

    class ReadRecordingStore(DetailedStore):
        manifest_reads = 0

        def manifest(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
            self.manifest_reads += 1
            return super().manifest(session_id, revision=revision)

    store = ReadRecordingStore(tmp_path)
    api = TestClient(
        create_app(
            Settings(environment="test", log_level="CRITICAL", project_root=tmp_path),
            detailed_store=store,
        )
    )
    response = api.get(
        f"/api/v1/telemetry/sessions/{identity}/microsectors",
        params={"driver_a": "44", "lap_a": 1, "driver_b": "1", "lap_b": 1, "count": count},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert store.manifest_reads == 0
