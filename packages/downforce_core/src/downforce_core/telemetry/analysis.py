"""Provider corners or labelled distance windows, never invented turns or FIA sectors."""

import math
from collections.abc import Mapping

from .alignment import sample_trace
from .types import (
    AlignmentSettings,
    AnalysisWindow,
    ChannelValue,
    Comparison,
    CornerMarker,
    LapTrace,
    Microsector,
    MicrosectorSummary,
    TraceRejected,
    WindowComparison,
    WindowMetrics,
)

BRAKE_LABEL = "BRAKE_ACTIVE"


def decode_drs(raw_code: int | None, mapping: Mapping[int, str] | None = None) -> str:
    """No provider-specific meanings are guessed, including for parser-default zero."""
    return "UNKNOWN" if mapping is None or raw_code is None else mapping.get(raw_code, "UNKNOWN")


def build_windows(
    comparison: Comparison,
    *,
    markers: tuple[CornerMarker, ...] = (),
    half_width_m: float = 100,
    fallback_count: int = 10,
) -> tuple[AnalysisWindow, ...]:
    """Clip windows to common support, split overlaps at neighboring marker midpoints."""
    lo, hi = comparison.distance_m[0], comparison.distance_m[-1]
    if not math.isfinite(half_width_m) or half_width_m <= 0:
        raise TraceRejected("invalid_window_width")
    if markers:
        if any(not math.isfinite(marker.distance_m) for marker in markers):
            raise TraceRejected("invalid_corner_distance")
        ordered = sorted(
            markers, key=lambda marker: (marker.distance_m, marker.number, marker.letter)
        )
        if any(a.distance_m == b.distance_m for a, b in zip(ordered, ordered[1:], strict=False)):
            raise TraceRejected("duplicate_corner_distance")
        windows = []
        for i, marker in enumerate(ordered):
            if not lo <= marker.distance_m <= hi:
                continue
            start = max(lo, marker.distance_m - half_width_m)
            end = min(hi, marker.distance_m + half_width_m)
            if i:
                start = max(start, (ordered[i - 1].distance_m + marker.distance_m) / 2)
            if i + 1 < len(ordered):
                end = min(end, (marker.distance_m + ordered[i + 1].distance_m) / 2)
            if end > start:
                windows.append(AnalysisWindow(marker.label, start, end, "PROVIDER_CORNER", marker))
        return tuple(windows)
    if not 1 <= fallback_count <= 1_000:
        raise TraceRejected("invalid_distance_window_count")
    edges = tuple(lo + (hi - lo) * i / fallback_count for i in range(fallback_count)) + (hi,)
    return tuple(
        AnalysisWindow(f"Distance window {i + 1}", edges[i], edges[i + 1], "DISTANCE_WINDOW")
        for i in range(fallback_count)
    )


def _number(value: ChannelValue) -> float | None:
    return (
        None
        if value is None or isinstance(value, bool) or not math.isfinite(value)
        else float(value)
    )


def _points(trace: LapTrace, start: float, end: float) -> tuple[float, ...]:
    return tuple(sorted({start, end, *(d for d in trace.distance_m if start < d < end)}))


def _window_time(
    trace: LapTrace,
    start: float,
    end: float,
    settings: AlignmentSettings,
) -> float | None:
    points = _points(trace, start, end)
    # Endpoint observations alone cannot establish measured/interpolable support in between.
    probes = (*points, *((a + b) / 2 for a, b in zip(points, points[1:], strict=False)))
    if any(sample_trace(trace, d, settings)[0] is None for d in probes):
        return None
    first = sample_trace(trace, start, settings)[0]
    last = sample_trace(trace, end, settings)[0]
    return None if first is None or last is None else last - first


def _brake_events(
    trace: LapTrace,
    start: float,
    end: float,
    settings: AlignmentSettings,
    persistence: float,
) -> tuple[float | None, float | None]:
    """Hold-state runs must persist in actual elapsed seconds; onset then qualified release."""
    points = _points(trace, start, end)
    runs: list[tuple[bool, float, float, float]] = []
    for left, right in zip(points, points[1:], strict=False):
        t0, values = sample_trace(trace, left, settings)
        t1, _ = sample_trace(trace, right, settings)
        middle, _ = sample_trace(trace, (left + right) / 2, settings)
        active = values.get("brake")
        if t0 is None or t1 is None or middle is None or not isinstance(active, bool):
            continue
        if runs and runs[-1][0] == active and runs[-1][3] == t0:
            state, distance, begin, _ = runs[-1]
            runs[-1] = (state, distance, begin, t1)
        else:
            runs.append((active, left, t0, t1))
    onset = None
    previous_end = None
    for active, distance, t0, t1 in runs:
        if onset is not None and previous_end is not None and previous_end != t0:
            return onset, None  # A release somewhere inside the gap is not observed.
        previous_end = t1
        if t1 - t0 + 1e-12 < persistence:
            continue
        if active and onset is None:
            onset = distance
        elif not active and onset is not None:
            return onset, distance
    return onset, None


def _throttle_onset(
    trace: LapTrace,
    start: float,
    end: float,
    threshold: float,
    settings: AlignmentSettings,
    persistence: float,
) -> float | None:
    """First linearly resolved threshold crossing with persistent >= threshold support."""
    points = _points(trace, start, end)
    run_distance: float | None = None
    run_start_time: float | None = None
    last_time: float | None = None
    for left, right in zip(points, points[1:], strict=False):
        t0, v0 = sample_trace(trace, left, settings)
        t1, v1 = sample_trace(trace, right, settings)
        middle, _ = sample_trace(trace, (left + right) / 2, settings)
        a, b = _number(v0.get("throttle_percent")), _number(v1.get("throttle_percent"))
        if t0 is None or t1 is None or middle is None or a is None or b is None:
            run_distance, run_start_time, last_time = None, None, None
            continue
        if last_time is not None and last_time != t0:
            run_distance, run_start_time = None, None
        if a < threshold <= b:
            fraction = (threshold - a) / (b - a)
            run_distance = left + fraction * (right - left)
            run_start_time = t0 + fraction * (t1 - t0)
        elif a >= threshold and run_distance is None:
            run_distance, run_start_time = left, t0
        if a >= threshold > b:
            fraction = (threshold - a) / (b - a)
            end_time = t0 + fraction * (t1 - t0)
        else:
            end_time = t1
        if run_start_time is not None and end_time - run_start_time + 1e-12 >= persistence:
            return run_distance
        if b < threshold:
            run_distance, run_start_time = None, None
        last_time = t1
    return None


def _metrics(
    trace: LapTrace,
    window: AnalysisWindow,
    settings: AlignmentSettings,
    persistence: float,
) -> WindowMetrics:
    points = _points(trace, window.start_m, window.end_m)
    rows = [sample_trace(trace, d, settings)[1] for d in points]
    speeds = [_number(row.get("speed_kmh")) for row in rows]
    known = [(float(speed), i) for i, speed in enumerate(speeds) if speed is not None]
    minimum, index = min(known) if known else (None, 0)
    gear = rows[index].get("gear") if known else None
    gear_at_minimum = gear if isinstance(gear, int) and not isinstance(gear, bool) else None
    duration = _window_time(trace, window.start_m, window.end_m, settings)
    flags = [
        f"missing_{name}"
        for name in ("speed_kmh", "gear", "brake", "throttle_percent")
        if name not in trace.channels
    ]
    if duration is None:
        flags.append("window_interpolation_gap")
        flags.append("minimum_speed_partial_support")
    onset, release = _brake_events(trace, window.start_m, window.end_m, settings, persistence)
    pickup = _throttle_onset(trace, points[index], window.end_m, 20, settings, persistence)
    full = _throttle_onset(trace, points[index], window.end_m, 98, settings, persistence)
    return WindowMetrics(
        speeds[0],
        minimum,
        speeds[-1],
        gear_at_minimum,
        duration,
        onset,
        release,
        pickup,
        full,
        tuple(flags),
    )


def analyze_windows(
    comparison: Comparison,
    windows: tuple[AnalysisWindow, ...],
    *,
    persistence_seconds: float = 0.2,
) -> tuple[WindowComparison, ...]:
    """Entry/exit are window boundaries, min at native vertices; brake means boolean active."""
    if not math.isfinite(persistence_seconds) or persistence_seconds <= 0:
        raise TraceRejected("invalid_persistence_threshold")
    results = []
    lo, hi = comparison.distance_m[0], comparison.distance_m[-1]
    for window in windows:
        if not lo <= window.start_m < window.end_m <= hi:
            raise TraceRejected("window_outside_common_support")
        a = _metrics(comparison.trace_a, window, comparison.settings, persistence_seconds)
        b = _metrics(comparison.trace_b, window, comparison.settings, persistence_seconds)
        delta = (
            None
            if a.time_seconds is None or b.time_seconds is None
            else (a.time_seconds - b.time_seconds)
        )
        results.append(
            WindowComparison(window, a, b, delta, persistence_seconds=persistence_seconds)
        )
    return tuple(results)


def microsectors(comparison: Comparison, *, count: int = 20) -> MicrosectorSummary:
    """>=5 configured physical-distance bins, explicitly derived and not FIA sectors."""
    if not 5 <= count <= 1_000:
        raise TraceRejected("microsectors_require_at_least_five_and_at_most_1000")
    lo, hi = comparison.distance_m[0], comparison.distance_m[-1]
    edges = tuple(lo + (hi - lo) * i / count for i in range(count)) + (hi,)
    sectors = []
    for i in range(count):
        a = _window_time(comparison.trace_a, edges[i], edges[i + 1], comparison.settings)
        b = _window_time(comparison.trace_b, edges[i], edges[i + 1], comparison.settings)
        delta = None if a is None or b is None else a - b
        sectors.append(Microsector(i + 1, edges[i], edges[i + 1], a, b, delta))
    complete = all(sector.delta_seconds is not None for sector in sectors)
    first, last = comparison.delta_seconds[0], comparison.delta_seconds[-1]
    total = None if not complete or first is None or last is None else last - first
    error = None if total is None else sum(s.delta_seconds or 0 for s in sectors) - total
    deltas = [(s.delta_seconds, s.index) for s in sectors if s.delta_seconds is not None]
    times_a = [(s.time_a_seconds, s.index) for s in sectors if s.time_a_seconds is not None]
    times_b = [(s.time_b_seconds, s.index) for s in sectors if s.time_b_seconds is not None]
    gain_a = min(deltas)[1] if deltas and min(deltas)[0] < 0 else None
    gain_b = (
        min(deltas, key=lambda item: (-item[0], item[1]))[1]
        if (deltas and max(deltas)[0] > 0)
        else None
    )
    flags = () if complete else ("incomplete_microsector_support",)
    return MicrosectorSummary(
        tuple(sectors),
        total,
        error,
        gain_a,
        gain_b,
        min(times_a)[1] if times_a else None,
        min(times_b)[1] if times_b else None,
        flags,
        min(times_a, key=lambda item: (-item[0], item[1]))[1] if times_a else None,
        min(times_b, key=lambda item: (-item[0], item[1]))[1] if times_b else None,
    )
