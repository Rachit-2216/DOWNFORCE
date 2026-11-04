"""Physical window calculations and honest derived microsectors."""

import pytest
from downforce_core.telemetry.alignment import align_laps, prepare_lap_trace
from downforce_core.telemetry.analysis import (
    analyze_windows,
    build_windows,
    decode_drs,
    microsectors,
)
from downforce_core.telemetry.types import (
    AnalysisWindow,
    Comparison,
    CornerMarker,
    LapBounds,
    LapMetadata,
    RawTelemetry,
    TraceRejected,
)


def pair() -> Comparison:
    a = prepare_lap_trace(
        RawTelemetry(
            tuple(i * 1_000_000_000 for i in range(11)),
            {
                "speed_kmh": (100, 90, 80, 70, 60, 50, 60, 70, 80, 90, 100),
                "gear": (6, 6, 5, 5, 4, 3, 4, 5, 5, 6, 6),
                "brake": (False, False, True, True, True, False, False, False, False, False, False),
                "throttle_percent": (100, 100, 0, 0, 0, 0, 40, 100, 100, 100, 100),
            },
            tuple(float(i * 10) for i in range(11)),
        ),
        bounds=LapBounds(0, 10_000_000_000),
        metadata=LapMetadata("A", 1, "r", 10),
    )
    b = prepare_lap_trace(
        RawTelemetry(
            tuple(i * 500_000_000 for i in range(11)),
            {"speed_kmh": (72,) * 11},
            tuple(float(i * 10) for i in range(11)),
        ),
        bounds=LapBounds(0, 5_000_000_000),
        metadata=LapMetadata("B", 1, "r", 5),
    )
    return align_laps(a, b)


def test_provider_corner_labels_retained_and_windows_nonoverlapping() -> None:
    markers = (
        CornerMarker(4, "A", 45, 1, 2, 30, "provider"),
        CornerMarker(4, "B", 55, 3, 4, 40, "provider"),
    )
    windows = build_windows(pair(), markers=markers, half_width_m=20)
    assert [w.label for w in windows] == ["4A", "4B"]
    assert windows[0].end_m <= windows[1].start_m
    assert windows[0].marker is markers[0]
    assert windows[0].start_m == 25 and windows[1].end_m == 75


def test_missing_markers_use_distance_windows_not_fake_corners() -> None:
    windows = build_windows(pair(), fallback_count=5)
    assert len(windows) == 5
    assert all(w.kind == "DISTANCE_WINDOW" and w.marker is None for w in windows)
    assert all("Turn" not in w.label for w in windows)
    assert windows[0].start_m == 0 and windows[-1].end_m == 100


def test_window_metrics_exact_boundaries_minimum_gear_and_signed_time() -> None:
    result = analyze_windows(pair(), (AnalysisWindow("5", 15, 85, "DISTANCE_WINDOW"),))[0]
    assert result.a.entry_speed_kmh == 85
    assert result.a.exit_speed_kmh == 85
    assert result.a.minimum_speed_kmh == 50
    assert result.a.gear_at_minimum == 3
    assert result.a.time_seconds == 7
    assert result.b.time_seconds == 3.5
    assert result.delta_seconds == 3.5
    assert result.a.brake_onset_m == 20
    assert result.a.brake_release_m == 50
    assert result.a.throttle_pickup_m == 55
    assert result.a.full_throttle_m == pytest.approx(69 + 2 / 3)


def test_microsector_sum_uses_actual_elapsed_functions_not_lap_time_fit() -> None:
    summary = microsectors(pair(), count=20)
    assert len(summary.sectors) == 20
    assert summary.total_delta_seconds == 5
    assert sum(s.delta_seconds or 0 for s in summary.sectors) == pytest.approx(5)
    assert summary.sum_error_seconds == pytest.approx(0)
    assert all(s.label == "DERIVED_DISTANCE_BIN_NOT_FIA_SECTOR" for s in summary.sectors)
    assert summary.largest_gain_b_index == 1  # deterministic first tie
    with pytest.raises(TraceRejected, match="at_least_five"):
        microsectors(pair(), count=4)


def test_microsector_gaps_are_not_hidden_by_valid_endpoints() -> None:
    p = pair()
    sparse = prepare_lap_trace(
        RawTelemetry((0, 5_000_000_000, 10_000_000_000), {"speed_kmh": (36, 36, 36)}, (0, 50, 100)),
        bounds=LapBounds(0, 10_000_000_000),
        metadata=LapMetadata("C", 1, "r"),
    )
    result = microsectors(align_laps(sparse, p.trace_b), count=10)
    assert result.total_delta_seconds is None
    assert result.sum_error_seconds is None
    assert all(s.delta_seconds is None for s in result.sectors)


def test_brake_debounce_rejects_short_flicker() -> None:
    times = (0, 100_000_000, 150_000_000, 500_000_000, 1_000_000_000, 2_000_000_000)
    distances = (0, 1, 1.5, 5, 10, 20)
    prepared = prepare_lap_trace(
        RawTelemetry(
            times,
            {"speed_kmh": (36,) * 6, "brake": (False, True, False, True, False, False)},
            distances,
        ),
        bounds=LapBounds(0, 2_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
    )
    result = analyze_windows(
        align_laps(prepared, prepared),
        (AnalysisWindow("distance", 0, 20, "DISTANCE_WINDOW"),),
        persistence_seconds=0.2,
    )[0]
    assert result.a.brake_onset_m == 5
    assert result.a.brake_release_m == 10


def test_drs_no_guessed_mapping_and_missing_channels_honest() -> None:
    assert decode_drs(14) == "UNKNOWN"
    assert decode_drs(99, {14: "ACTIVE"}) == "UNKNOWN"
    assert decode_drs(14, {14: "ACTIVE"}) == "ACTIVE"
    result = analyze_windows(pair(), build_windows(pair(), fallback_count=5))[0]
    assert result.b.gear_at_minimum is None
    assert result.b.brake_onset_m is None
    assert "missing_brake" in result.b.flags


def test_windows_clipped_to_common_range_and_no_nonpositive_widths() -> None:
    windows = build_windows(
        pair(),
        markers=(
            CornerMarker(1, "", -100, None, None, None, "provider"),
            CornerMarker(2, "", 0, None, None, None, "provider"),
            CornerMarker(3, "", 100, None, None, None, "provider"),
        ),
        half_width_m=20,
    )
    assert [w.label for w in windows] == ["2", "3"]
    assert all(0 <= w.start_m < w.end_m <= 100 for w in windows)


def test_no_brake_release_can_be_claimed_across_missing_support() -> None:
    raw = RawTelemetry(
        (0, 1_000_000_000, 5_000_000_000, 6_000_000_000),
        {"speed_kmh": (36, 36, 36, 36), "brake": (True, True, False, False)},
        (0, 10, 50, 60),
    )
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 6_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    result = analyze_windows(
        align_laps(prepared, prepared), (AnalysisWindow("distance", 0, 60, "DISTANCE_WINDOW"),)
    )[0]
    assert result.a.brake_onset_m == 0
    assert result.a.brake_release_m is None
    assert "window_interpolation_gap" in result.a.flags


def test_microsector_extrema_include_worst_bins() -> None:
    summary = microsectors(pair())
    assert summary.slowest_a_index == 1
    assert summary.slowest_b_index == 1
    assert summary.accuracy == "LINEAR_NATIVE_TIME_DISTANCE_WITH_GAP_MASKS"


def test_window_thresholds_and_persistence_are_explicit_output_contract() -> None:
    result = analyze_windows(
        pair(), build_windows(pair(), fallback_count=5), persistence_seconds=0.3
    )[0]
    assert result.persistence_seconds == 0.3
    assert result.throttle_pickup_threshold_percent == 20
    assert result.full_throttle_threshold_percent == 98
    assert result.brake_label == "BRAKE_ACTIVE"


def test_duplicate_corner_distance_not_silently_overlapped() -> None:
    with pytest.raises(TraceRejected, match="duplicate_corner_distance"):
        build_windows(
            pair(),
            markers=(
                CornerMarker(1, "", 50, None, None, None, "provider"),
                CornerMarker(2, "", 50, None, None, None, "provider"),
            ),
        )
