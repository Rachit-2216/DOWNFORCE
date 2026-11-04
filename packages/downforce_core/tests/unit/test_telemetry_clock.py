import importlib

import pyarrow as pa
import pytest


def extract(table, laps):
    try:
        module = importlib.import_module("downforce_core.telemetry.clock")
    except ModuleNotFoundError:
        pytest.fail("a verified telemetry clock adapter is required")
    return module.extract_session_clock(table, laps)


def test_native_duration_retains_nanoseconds_and_never_uses_provider_time():
    table = pa.table(
        {
            "session_time": pa.array([1_000_000_007, None], type=pa.duration("ns")),
            "provider_time": pa.array([999, 999], type=pa.duration("s")),
        }
    )
    clock = extract(table, pa.table({}))
    assert clock.session_time_ns == (1_000_000_007, None)
    assert clock.source == "native-session-time"


def test_date_fallback_uses_consistent_lap_origin_without_losing_nanoseconds():
    origin = 1_700_000_000_000_000_003
    laps = pa.table(
        {
            "lap_start_time": pa.array([1_000_000_007, 2_000_000_009], pa.duration("ns")),
            "lap_start_timestamp": pa.array(
                [origin + 1_000_000_007, origin + 2_000_000_009], pa.timestamp("ns")
            ),
        }
    )
    table = pa.table({"timestamp": pa.array([origin + 3_000_000_011, None], pa.timestamp("ns"))})
    clock = extract(table, laps)
    assert clock.session_time_ns == (3_000_000_011, None)
    assert clock.origin_utc_ns == origin
    assert clock.source == "date-minus-verified-lap-origin"


def test_conflicting_origins_do_not_get_averaged_or_guessed():
    laps = pa.table(
        {
            "lap_start_time": pa.array([1, 2], pa.duration("ns")),
            "lap_start_timestamp": pa.array([101, 103], pa.timestamp("ns")),
        }
    )
    with pytest.raises(ValueError, match="inconsistent"):
        extract(pa.table({"timestamp": pa.array([104], pa.timestamp("ns"))}), laps)


def test_raw_time_alone_is_not_a_session_clock():
    with pytest.raises(ValueError, match="reliable session clock"):
        extract(pa.table({"provider_time": pa.array([5], pa.duration("ns"))}), pa.table({}))
