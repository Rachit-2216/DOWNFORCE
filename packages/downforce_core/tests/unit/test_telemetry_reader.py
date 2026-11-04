import importlib
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.telemetry.reader import TelemetryReader


def reader(store: DetailedStore) -> TelemetryReader:
    try:
        module = importlib.import_module("downforce_core.telemetry.reader")
    except ModuleNotFoundError:
        pytest.fail("a revision-bound telemetry reader is required")
    return cast(TelemetryReader, module.TelemetryReader(store))


def publish(
    tmp_path: Path, *, season: int = 2024, speed: int = 180
) -> tuple[DetailedStore, str, str]:
    store = DetailedStore(tmp_path)
    laps = pa.table(
        {
            "driver_number": ["44", "44"],
            "lap_number": [1.0, 2.0],
            "lap_start_time": pa.array([10_000_000_000, 20_000_000_000], pa.duration("ns")),
            "lap_time": pa.array([10_000_000_000, 10_000_000_000], pa.duration("ns")),
            "lap_start_timestamp": pa.array(
                [1_700_000_010_000_000_000, 1_700_000_020_000_000_000], pa.timestamp("ns")
            ),
            "is_accurate": [True, True],
            "deleted": [False, False],
            "provider_generated": [False, False],
            "compound": ["SOFT", "SOFT"],
        }
    )
    car = pa.table(
        {
            "session_time": pa.array([i * 1_000_000_000 for i in range(9, 32)], pa.duration("ns")),
            "speed": [speed] * 23,
            "gear": [4] * 23,
            "brake": [False] * 23,
            "drs": [0] * 23,
        }
    )
    identity = f"{season}-01-race"
    revision = store.publish(
        identity,
        {"laps": laps, "car-data-44": car},
        {"session": {"session_id": identity, "season": season, "state": "completed"}},
    )
    return store, identity, revision


def test_reader_pins_revision_and_exact_observed_lap_bounds(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path)
    loaded = reader(store).load_lap(identity, "44", 1, revision=revision)
    assert loaded.metadata.revision == revision
    assert loaded.bounds.start_ns == 10_000_000_000
    assert loaded.bounds.end_ns == 20_000_000_000
    assert loaded.source.session_time_ns[0] == 9_000_000_000
    assert loaded.source.channels["gear"] == (4,) * 23
    assert loaded.context["compound"] == "SOFT"
    assert loaded.clock_source == "native-session-time"


def test_raw_parser_default_drs_in_2026_is_not_an_observed_off_state(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path, season=2026)
    loaded = reader(store).load_lap(identity, "44", 1, revision=revision)
    assert loaded.source.channels["drs_raw"] == (None,) * 23
    assert loaded.channel_availability["drs_raw"] == "not-recorded-provider-parser-default"
    assert store.read_table(identity, "car-data-44")["drs"].to_pylist() == [0] * 23


def test_reader_rejects_unknown_or_ambiguous_lap_and_driver(tmp_path: Path) -> None:
    store, identity, _ = publish(tmp_path)
    service = reader(store)
    with pytest.raises(ValueError, match="lap"):
        service.load_lap(identity, "44", 99)
    with pytest.raises(ValueError, match="driver"):
        service.load_lap(identity, "../../other", 1)


def test_old_revision_does_not_turn_into_new_active_trace(tmp_path: Path) -> None:
    store, identity, old_revision = publish(tmp_path, speed=180)
    _, _, new_revision = publish(tmp_path, speed=200)
    service = reader(store)
    assert new_revision != old_revision
    assert (
        service.load_lap(identity, "44", 1, revision=old_revision).source.channels["speed_kmh"][0]
        == 180
    )
    assert service.load_lap(identity, "44", 1).source.channels["speed_kmh"][0] == 200
