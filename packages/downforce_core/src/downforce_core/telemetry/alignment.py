"""Pure physical-distance alignment. Interpolation is derived, bounded, and never forced-fit."""

import math
from bisect import bisect_left
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .types import (
    DISCRETE_CHANNELS,
    AlignedTrace,
    AlignmentSettings,
    ChannelValue,
    ClockModel,
    Comparison,
    LapBounds,
    LapMetadata,
    LapTrace,
    PositionSamples,
    PrepareSettings,
    RawTelemetry,
    TraceRejected,
)

_DEFAULT_PREPARE = PrepareSettings()
_DEFAULT_ALIGNMENT = AlignmentSettings()


def _numeric(value: ChannelValue) -> float | None:
    if value is None or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value)


def _value(name: str, left: ChannelValue, right: ChannelValue, fraction: float) -> ChannelValue:
    if name in DISCRETE_CHANNELS:
        if name == "brake":
            return left if isinstance(left, bool) else None
        return left if isinstance(left, int) and not isinstance(left, bool) else None
    a, b = _numeric(left), _numeric(right)
    return None if a is None or b is None else a + fraction * (b - a)


def _order_flags(values: tuple[int, ...] | tuple[float, ...], name: str) -> list[str]:
    flags = []
    if any(b == a for a, b in zip(values, values[1:], strict=False)):
        flags.append(f"duplicate_{name}")
    if any(b < a for a, b in zip(values, values[1:], strict=False)):
        flags.append(f"decreasing_{name}")
    if any(not math.isfinite(v) for v in values):
        flags.append(f"nonfinite_{name}")
    return flags


def prepare_lap_trace(
    source: RawTelemetry,
    *,
    bounds: LapBounds,
    metadata: LapMetadata,
    settings: PrepareSettings = _DEFAULT_PREPARE,
) -> LapTrace:
    """Slice exact bounded edges with linear/hold interpolation; preserve source verbatim."""
    exclusions = (
        (metadata.pit_in, settings.allow_pit_laps, "pit_in"),
        (metadata.pit_out, settings.allow_pit_laps, "pit_out"),
        (metadata.deleted, settings.allow_deleted, "deleted"),
        (metadata.provider_generated, settings.allow_provider_generated, "provider_generated"),
        (not metadata.reliable, settings.allow_unreliable, "unreliable"),
    )
    flags = [reason for excluded, _, reason in exclusions if excluded]
    for excluded, allowed, reason in exclusions:
        if excluded and not allowed:
            raise TraceRejected(reason)
    if not source.session_time_ns:
        raise TraceRejected("empty_trace")
    if not math.isfinite(settings.max_edge_gap_seconds) or settings.max_edge_gap_seconds <= 0:
        raise TraceRejected("invalid_edge_gap_limit")
    original = source.session_time_ns
    rows = [(t, i, i, 0.0) for i, t in enumerate(original) if bounds.start_ns <= t <= bounds.end_ns]
    support = {i for _, i, _, _ in rows}
    for i, (left, right) in enumerate(zip(original, original[1:], strict=False)):
        if left < bounds.start_ns < right or left < bounds.end_ns < right:
            support.update((i, i + 1))
    if not support:
        raise TraceRejected("insufficient_lap_overlap")
    offset = min(support)
    times = original[offset : max(support) + 1]
    order = _order_flags(times, "timestamp")
    flags.extend(order)
    # Only local selected/bracketing support matters; malformed local rows are never sorted.
    if not order:
        for edge in (bounds.start_ns, bounds.end_ns):
            i = bisect_left(times, edge)
            if i < len(times) and times[i] == edge:
                continue
            if 0 < i < len(times):
                gap = times[i] - times[i - 1]
                if gap / 1e9 <= settings.max_edge_gap_seconds:
                    rows.append((edge, offset + i - 1, offset + i, (edge - times[i - 1]) / gap))
                    flags.append("interpolated_lap_edge")
                else:
                    flags.append("lap_edge_gap")
        rows.sort(key=lambda row: row[0])
    if len(rows) < 2:
        raise TraceRejected("insufficient_lap_overlap")
    start_covered = rows[0][0] == bounds.start_ns
    finish_covered = rows[-1][0] == bounds.end_ns
    if not start_covered or not finish_covered:
        flags.append("partial_lap")
        if not settings.allow_partial:
            raise TraceRejected("partial_lap")
    channels = {
        name: tuple(
            values[i] if i == j else _value(name, values[i], values[j], f) for _, i, j, f in rows
        )
        for name, values in source.channels.items()
    }
    elapsed = tuple((t - bounds.start_ns) / 1e9 for t, _, _, _ in rows)
    derived = source.distance_m is None
    if source.distance_m is not None:
        native = source.distance_m
        distance = tuple(
            native[i] if i == j else native[i] + f * (native[j] - native[i]) for _, i, j, f in rows
        )
    else:
        speed = channels.get("speed_kmh")
        if speed is None:
            raise TraceRejected("distance_requires_speed")
        numeric_speed = tuple(_numeric(v) for v in speed)
        if any(v is None or v < 0 for v in numeric_speed):
            raise TraceRejected("invalid_speed")
        integrated = [0.0]
        for i in range(1, len(elapsed)):
            a, b = numeric_speed[i - 1], numeric_speed[i]
            assert a is not None and b is not None
            integrated.append(integrated[-1] + (a + b) / 7.2 * (elapsed[i] - elapsed[i - 1]))
        distance = tuple(integrated)
        flags.append("distance_derived_trapezoidal_speed")
        if not start_covered:
            flags.append("distance_origin_unknown")
    flags.extend(_order_flags(distance, "distance"))
    if any(
        _numeric(v) is None or float(v) < 0 for v in channels.get("speed_kmh", ()) if v is not None
    ):
        flags.append("invalid_speed")
    return LapTrace(
        metadata,
        bounds,
        source,
        elapsed,
        distance,
        MappingProxyType(channels),
        tuple(dict.fromkeys(flags)),
        start_covered,
        finish_covered,
        derived,
    )


def prepare_arrow_trace(
    table: Any,
    *,
    clock: ClockModel,
    bounds: LapBounds,
    metadata: LapMetadata,
    channel_columns: Mapping[str, str],
    distance_column: str | None = None,
    settings: PrepareSettings = _DEFAULT_PREPARE,
) -> LapTrace:
    """Arrow duration/timestamp clocks are cast directly to ns; Time is never aliased."""
    import pyarrow as pa  # type: ignore[import-untyped]
    import pyarrow.compute as pc  # type: ignore[import-untyped]

    name = "SessionTime" if clock.kind == "session_time" else "Date"
    if name not in table.column_names:
        raise TraceRejected(f"missing_explicit_clock:{name}")
    column = table[name]
    expected = (
        pa.types.is_duration(column.type)
        if clock.kind == "session_time"
        else (pa.types.is_timestamp(column.type))
    )
    if not expected:
        raise TraceRejected("clock_requires_arrow_duration_or_timestamp")
    target = pa.duration("ns") if clock.kind == "session_time" else pa.timestamp("ns", tz="UTC")
    ns = pc.cast(pc.cast(column, target), pa.int64()).to_pylist()
    if any(t is None for t in ns):
        raise TraceRejected("null_clock")
    origin = clock.origin_utc_ns or 0
    source = RawTelemetry(
        tuple(int(t) - origin for t in ns),
        {key: tuple(table[value].to_pylist()) for key, value in channel_columns.items()},
        None if distance_column is None else tuple(table[distance_column].to_pylist()),
    )
    return prepare_lap_trace(source, bounds=bounds, metadata=metadata, settings=settings)


def _check_trace(trace: LapTrace) -> None:
    bad = [
        flag
        for flag in trace.flags
        if flag.startswith(("duplicate_", "decreasing_", "nonfinite_"))
        or flag in {"distance_origin_unknown", "invalid_speed"}
    ]
    if bad:
        raise TraceRejected(",".join(bad))
    if len(trace.distance_m) < 2:
        raise TraceRejected("insufficient_distance_support")


def sample_trace(
    trace: LapTrace,
    distance_m: float,
    settings: AlignmentSettings = _DEFAULT_ALIGNMENT,
) -> tuple[float | None, dict[str, ChannelValue]]:
    """Sample the derived t(d); gap masks apply to all channels, never extrapolate."""
    _check_trace(trace)
    ds = trace.distance_m
    i = bisect_left(ds, distance_m)
    if i < len(ds) and ds[i] == distance_m:
        return trace.elapsed_seconds[i], {
            key: _value(key, values[i], values[i], 0) for key, values in trace.channels.items()
        }
    if i == 0 or i == len(ds):
        return None, {key: None for key in trace.channels}
    j = i - 1
    if ds[i] - ds[j] > settings.max_gap_m or (
        trace.elapsed_seconds[i] - trace.elapsed_seconds[j] > settings.max_gap_seconds
    ):
        return None, {key: None for key in trace.channels}
    fraction = (distance_m - ds[j]) / (ds[i] - ds[j])
    elapsed = trace.elapsed_seconds[j] + fraction * (
        trace.elapsed_seconds[i] - trace.elapsed_seconds[j]
    )
    return elapsed, {
        name: _value(name, values[j], values[i], fraction)
        for name, values in trace.channels.items()
    }


def _aligned(trace: LapTrace, grid: tuple[float, ...], settings: AlignmentSettings) -> AlignedTrace:
    sampled = [sample_trace(trace, d, settings) for d in grid]
    return AlignedTrace(
        tuple(t for t, _ in sampled),
        MappingProxyType(
            {name: tuple(values[name] for _, values in sampled) for name in trace.channels}
        ),
        tuple(t is not None for t, _ in sampled),
    )


def align_laps(
    a: LapTrace,
    b: LapTrace,
    *,
    settings: AlignmentSettings = _DEFAULT_ALIGNMENT,
) -> Comparison:
    """Compare physical common support. Delta is t_A(d)-t_B(d), never LapTime fitted."""
    _check_trace(a)
    _check_trace(b)
    if (
        not math.isfinite(settings.spacing_m)
        or settings.spacing_m <= 0
        or not 2 <= settings.max_points <= 20_000
        or not math.isfinite(settings.max_gap_seconds)
        or settings.max_gap_seconds <= 0
        or not math.isfinite(settings.max_gap_m)
        or settings.max_gap_m <= 0
    ):
        raise TraceRejected("invalid_alignment_settings")
    start, end = max(a.distance_m[0], b.distance_m[0]), min(a.distance_m[-1], b.distance_m[-1])
    if end <= start:
        raise TraceRejected("no_common_distance_overlap")
    if settings.grid == "reference_union":
        points = {start, end}
        for distances in (a.distance_m, b.distance_m):
            for d in distances:
                if start <= d <= end:
                    points.add(d)
                    if len(points) > settings.max_points:
                        raise TraceRejected("grid_point_limit")
        grid = tuple(sorted(points))
    else:
        count = math.ceil((end - start) / settings.spacing_m)
        if count + 1 > settings.max_points:
            raise TraceRejected("grid_point_limit")
        grid = tuple(start + i * settings.spacing_m for i in range(count)) + (end,)
    if len(grid) > settings.max_points:
        raise TraceRejected("grid_point_limit")
    aa, bb = _aligned(a, grid, settings), _aligned(b, grid, settings)
    delta = tuple(
        None if x is None or y is None else x - y
        for x, y in zip(aa.elapsed_seconds, bb.elapsed_seconds, strict=True)
    )
    finish = (
        a.start_covered
        and b.start_covered
        and a.finish_covered
        and b.finish_covered
        and start == a.distance_m[0] == b.distance_m[0]
        and end == a.distance_m[-1] == b.distance_m[-1]
    )
    observed = (
        None
        if a.metadata.lap_time_seconds is None or b.metadata.lap_time_seconds is None
        else a.metadata.lap_time_seconds - b.metadata.lap_time_seconds
    )
    error = None if not finish or observed is None or delta[-1] is None else delta[-1] - observed
    flags = [] if finish else ["common_domain_cropped_or_partial"]
    if any(d is None for d in delta):
        flags.append("interpolation_gaps")
    return Comparison(
        a,
        b,
        grid,
        aa,
        bb,
        delta,
        tuple(v is not None for v in delta),
        delta[-1],
        observed,
        finish,
        error,
        tuple(flags),
        settings,
    )


def align_positions(
    source: PositionSamples,
    target_session_time_ns: tuple[int, ...],
    *,
    max_gap_seconds: float = 2,
) -> tuple[tuple[float | None, float | None, float | None], ...]:
    """Independent time-grid coordinate interpolation without extrapolation."""
    count = len(source.session_time_ns)
    if (
        count != len(source.x)
        or count != len(source.y)
        or (source.z is not None and count != len(source.z))
    ):
        raise TraceRejected("position_length_mismatch")
    if _order_flags(source.session_time_ns, "timestamp"):
        raise TraceRejected("invalid_position_clock")
    if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
        raise TraceRejected("invalid_position_gap_limit")
    result = []
    axes = (source.x, source.y, source.z)
    for t in target_session_time_ns:
        i = bisect_left(source.session_time_ns, t)
        if i < count and t == source.session_time_ns[i]:
            result.append(tuple(None if axis is None else _numeric(axis[i]) for axis in axes))
        elif (
            i == 0
            or i == count
            or ((source.session_time_ns[i] - source.session_time_ns[i - 1]) / 1e9 > max_gap_seconds)
        ):
            result.append((None, None, None))
        else:
            fraction = (t - source.session_time_ns[i - 1]) / (
                source.session_time_ns[i] - source.session_time_ns[i - 1]
            )
            result.append(
                tuple(
                    None
                    if axis is None
                    else _numeric(_value("coordinate", axis[i - 1], axis[i], fraction))
                    for axis in axes
                )
            )
    return tuple((row[0], row[1], row[2]) for row in result)


def transform_coordinates(
    coordinates: tuple[tuple[float, float], ...],
    *,
    angle_degrees: float = 0,
    scale: float = 1,
    offset: tuple[float, float] = (0, 0),
) -> tuple[tuple[float, float], ...]:
    """Separate display-only transform; neither position source nor comparison changes."""
    angle = math.radians(angle_degrees)
    c, s = math.cos(angle), math.sin(angle)
    return tuple(
        (scale * (c * x - s * y) + offset[0], scale * (s * x + c * y) + offset[1])
        for x, y in coordinates
    )
