"""Exact provider-session clocks, never an implicit alias of raw API Time."""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa  # type: ignore[import-untyped]


@dataclass(frozen=True)
class SessionClock:
    session_time_ns: tuple[int | None, ...]
    source: str
    origin_utc_ns: int | None = None


def _duration_ns(table: pa.Table, name: str) -> tuple[int | None, ...]:
    column = table[name]
    if not pa.types.is_duration(column.type):
        raise ValueError(f"{name} must retain its native duration dtype")
    values = column.cast(pa.duration("ns")).cast(pa.int64()).to_pylist()
    return tuple(None if value is None else int(value) for value in values)


def _timestamp_ns(table: pa.Table, name: str) -> tuple[int | None, ...]:
    column = table[name]
    if not pa.types.is_timestamp(column.type):
        raise ValueError(f"{name} must retain its native timestamp dtype")
    # Arrow epoch conversion is integer based, including timezone-aware fields;
    # no host-local datetime.timestamp(), floats or millisecond rounding.
    values = column.cast(pa.timestamp("ns", tz=column.type.tz)).cast(pa.int64()).to_pylist()
    return tuple(None if value is None else int(value) for value in values)


def extract_session_clock(table: pa.Table, laps: pa.Table) -> SessionClock:
    if "session_time" in table.column_names:
        return SessionClock(_duration_ns(table, "session_time"), "native-session-time")
    required = {"lap_start_timestamp", "lap_start_time"}
    if "timestamp" not in table.column_names or not required.issubset(laps.column_names):
        raise ValueError("telemetry has no reliable session clock; raw Time is not sufficient")
    dates = _timestamp_ns(laps, "lap_start_timestamp")
    starts = _duration_ns(laps, "lap_start_time")
    origins = [
        date - start
        for date, start in zip(dates, starts, strict=True)
        if date is not None and start is not None
    ]
    if len(origins) < 2:
        raise ValueError("a reliable session clock requires two corroborating lap clock pairs")
    if len(set(origins)) != 1:
        raise ValueError("lap clock origins are inconsistent; Date cannot be aligned safely")
    origin = origins[0]
    times = tuple(
        None if date is None else date - origin for date in _timestamp_ns(table, "timestamp")
    )
    return SessionClock(times, "date-minus-verified-lap-origin", origin)
