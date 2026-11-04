"""Mathematical contracts for explicitly derived telemetry comparison."""

import math

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.telemetry.alignment import (
    align_laps,
    align_positions,
    prepare_arrow_trace,
    prepare_lap_trace,
    transform_coordinates,
)
from downforce_core.telemetry.types import (
    AlignmentSettings,
    ClockModel,
    LapBounds,
    LapMetadata,
    LapTrace,
    PositionSamples,
    PrepareSettings,
    RawTelemetry,
    TraceRejected,
)


def trace(speed: float, *, duration: int = 10, driver: str = "A", lap: int = 1) -> LapTrace:
    return prepare_lap_trace(
        RawTelemetry(
            tuple(i * 1_000_000_000 for i in range(duration + 1)),
            {"speed_kmh": (speed,) * (duration + 1)},
        ),
        bounds=LapBounds(0, duration * 1_000_000_000),
        metadata=LapMetadata(driver, lap, "revision-1", float(duration)),
    )


def test_constant_speed_signed_delta_is_actual_elapsed_time() -> None:
    result = align_laps(trace(36), trace(72, driver="B"))
    assert result.distance_m == tuple(float(i) for i in range(101))
    assert result.delta_seconds[-1] == pytest.approx(5)
    assert result.a.elapsed_seconds[-1] == pytest.approx(10)
    assert result.b.elapsed_seconds[-1] == pytest.approx(5)
    assert not result.finish_line_agreement_applicable
    assert result.observed_lap_delta_seconds == 0
    assert result.finish_delta_error_seconds is None


def test_identical_and_same_driver_different_lap() -> None:
    result = align_laps(trace(36), trace(36, lap=2))
    assert result.delta_seconds == (0.0,) * 101
    assert result.finish_line_agreement_applicable
    assert result.finish_delta_error_seconds == 0


def test_nan_negative_speed_reject_without_changing_source() -> None:
    for speed in (-1.0, math.nan, math.inf):
        raw = RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36.0, speed)})
        with pytest.raises(TraceRejected, match="invalid_speed"):
            prepare_lap_trace(
                raw, bounds=LapBounds(0, 1_000_000_000), metadata=LapMetadata("A", 1, "r")
            )
        assert raw.channels["speed_kmh"][0] == 36


def test_exact_lap_edges_are_bracketed_and_ns_input_is_preserved() -> None:
    raw = RawTelemetry(
        (-500_000_000, 500_000_000, 1_500_000_000),
        {"speed_kmh": (36.0, 72.0, 36.0), "gear": (2, 3, 4)},
    )
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 1_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    assert prepared.source.session_time_ns == raw.session_time_ns
    assert prepared.elapsed_seconds == (0.0, 0.5, 1.0)
    assert prepared.channels["speed_kmh"] == (54.0, 72.0, 54.0)
    assert prepared.channels["gear"] == (2, 3, 3)
    assert prepared.distance_m == pytest.approx((0, 8.75, 17.5))
    assert prepared.start_covered and prepared.finish_covered


def test_no_extrapolation_and_partial_requires_explicit_permission() -> None:
    raw = RawTelemetry((1_000_000_000, 2_000_000_000), {"speed_kmh": (36, 36)}, (10.0, 20.0))
    bounds, metadata = LapBounds(0, 3_000_000_000), LapMetadata("A", 1, "r")
    with pytest.raises(TraceRejected, match="partial_lap"):
        prepare_lap_trace(raw, bounds=bounds, metadata=metadata)
    prepared = prepare_lap_trace(
        raw, bounds=bounds, metadata=metadata, settings=PrepareSettings(allow_partial=True)
    )
    assert prepared.distance_m == (10, 20)
    result = align_laps(prepared, trace(36))
    assert result.distance_m[0] == 10 and result.distance_m[-1] == 20
    assert not result.finish_line_agreement_applicable


def test_gap_masks_are_honest_and_discrete_channels_hold() -> None:
    raw = RawTelemetry(
        (0, 1_000_000_000, 5_000_000_000),
        {
            "speed_kmh": (36, 36, 36),
            "gear": (2, 3, 4),
            "brake": (False, True, False),
            "drs_raw": (0, 14, 99),
        },
        (0.0, 10.0, 50.0),
    )
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 5_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    result = align_laps(prepared, trace(36))
    assert result.a.channels["gear"][5] == 2
    assert result.a.channels["brake"][5] is False
    assert result.a.channels["drs_raw"][15] is None
    assert result.delta_seconds[15] is None
    assert not result.valid_mask[15]
    assert result.valid_mask[50]


@pytest.mark.parametrize(
    "times,distances,flag",
    [
        ((0, 0, 1_000_000_000), (0.0, 0.0, 10.0), "duplicate_timestamp"),
        ((0, 2_000_000_000, 1_000_000_000), (0.0, 20.0, 10.0), "decreasing_timestamp"),
        ((0, 1_000_000_000, 2_000_000_000), (0.0, 20.0, 10.0), "decreasing_distance"),
    ],
)
def test_malformed_samples_retained_but_not_silently_sorted(
    times: tuple[int, ...],
    distances: tuple[float, ...],
    flag: str,
) -> None:
    raw = RawTelemetry(times, {"speed_kmh": (36, 36, 36)}, distances)
    prepared = prepare_lap_trace(
        raw,
        bounds=LapBounds(0, 2_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
        settings=PrepareSettings(allow_partial=True),
    )
    assert prepared.source == raw
    assert flag in prepared.flags
    with pytest.raises(TraceRejected, match=flag):
        align_laps(prepared, trace(36))


@pytest.mark.parametrize("field", ["pit_in", "pit_out", "deleted", "provider_generated"])
def test_excluded_lap_reasons(field: str) -> None:
    metadata = LapMetadata(
        "A",
        1,
        "r",
        pit_in=field == "pit_in",
        pit_out=field == "pit_out",
        deleted=field == "deleted",
        provider_generated=field == "provider_generated",
    )
    with pytest.raises(TraceRejected, match=field):
        prepare_lap_trace(
            RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36, 36)}),
            bounds=LapBounds(0, 1_000_000_000),
            metadata=metadata,
        )


def test_variable_speed_native_distance_elapsed_function() -> None:
    prepared = prepare_lap_trace(
        RawTelemetry(
            (0, 1_000_000_000, 2_000_000_000), {"speed_kmh": (36, 72, 108)}, (0.0, 15.0, 40.0)
        ),
        bounds=LapBounds(0, 2_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
    )
    result = align_laps(prepared, trace(36), settings=AlignmentSettings(spacing_m=5))
    assert result.a.elapsed_seconds[2] == pytest.approx(2 / 3)
    assert result.a.elapsed_seconds[-1] == 2
    assert result.delta_seconds[-1] == -2


def test_transform_is_display_only() -> None:
    source = ((1.0, 0.0), (0.0, 1.0))
    actual = transform_coordinates(source, angle_degrees=90)
    assert actual[0] == pytest.approx((0, 1))
    assert actual[1] == pytest.approx((-1, 0))
    assert source == ((1.0, 0.0), (0.0, 1.0))


def test_arrow_duration_ns_and_explicit_date_origin_agree() -> None:
    times = (1, 500_000_001, 1_000_000_001)
    origin = 1_800_000_000_000_000_000
    bounds = LapBounds(1, 1_000_000_001)
    metadata = LapMetadata("A", 1, "r")
    duration_table = pa.table(
        {"SessionTime": pa.array(times, type=pa.duration("ns")), "Speed": [36, 36, 36]}
    )
    date_table = pa.table(
        {
            "Date": pa.array([origin + t for t in times], type=pa.timestamp("ns", tz="UTC")),
            "Speed": [36, 36, 36],
        }
    )
    a = prepare_arrow_trace(
        duration_table,
        clock=ClockModel("session_time"),
        bounds=bounds,
        metadata=metadata,
        channel_columns={"speed_kmh": "Speed"},
    )
    b = prepare_arrow_trace(
        date_table,
        clock=ClockModel("date", origin),
        bounds=bounds,
        metadata=metadata,
        channel_columns={"speed_kmh": "Speed"},
    )
    assert a.source.session_time_ns == b.source.session_time_ns == times
    assert a.distance_m == b.distance_m == (0, 5, 10)


def test_provider_time_is_never_an_implicit_session_time_alias() -> None:
    table = pa.table(
        {"Time": pa.array([0, 1_000_000_000], type=pa.duration("ns")), "Speed": [36, 36]}
    )
    with pytest.raises(TraceRejected, match="missing_explicit_clock:SessionTime"):
        prepare_arrow_trace(
            table,
            clock=ClockModel("session_time"),
            bounds=LapBounds(0, 1_000_000_000),
            metadata=LapMetadata("A", 1, "r"),
            channel_columns={"speed_kmh": "Speed"},
        )
    with pytest.raises(TraceRejected, match="explicit_utc_origin"):
        ClockModel("date")


def test_zero_speed_distance_ties_rejected_not_epsilon_repaired() -> None:
    raw = RawTelemetry((0, 1_000_000_000, 2_000_000_000), {"speed_kmh": (0, 0, 36)})
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 2_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    assert prepared.distance_m == (0, 0, 5)
    assert "duplicate_distance" in prepared.flags
    with pytest.raises(TraceRejected, match="duplicate_distance"):
        align_laps(prepared, trace(36))


def test_variable_speed_trapezoidal_integration_not_uniform_samples() -> None:
    raw = RawTelemetry((0, 1_000_000_000, 3_000_000_000), {"speed_kmh": (36, 72, 108)})
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 3_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    assert prepared.distance_m == (0, 15, 65)
    assert "distance_derived_trapezoidal_speed" in prepared.flags


def test_invalid_discrete_native_values_are_masked_not_fractionalized() -> None:
    raw = RawTelemetry(
        (0, 1_000_000_000),
        {"speed_kmh": (36, 36), "gear": (2.5, 3), "brake": (0.5, True), "drs_raw": (14.5, 14)},
        (0, 10),
    )
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 1_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    result = align_laps(prepared, prepared)
    assert result.a.channels["gear"][0] is None
    assert result.a.channels["brake"][0] is None
    assert result.a.channels["drs_raw"][0] is None
    assert result.a.channels["gear"][-1] == 3
    assert prepared.source.channels["gear"][0] == 2.5


def test_independent_position_clock_mask_and_no_nan_coordinates() -> None:
    source = PositionSamples((0, 1_000_000_000, 5_000_000_000), (0, 10, math.nan), (0, 20, 50))
    aligned = align_positions(source, (-1, 500_000_000, 2_000_000_000, 5_000_000_000))
    assert aligned[0] == (None, None, None)
    assert aligned[1] == (5, 10, None)
    assert aligned[2] == (None, None, None)
    assert aligned[3] == (None, 50, None)
    assert source.x[-1] is not None and math.isnan(source.x[-1])


def test_finish_delta_diagnostic_does_not_force_observed_delta() -> None:
    a = prepare_lap_trace(
        RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36, 36)}, (0, 10)),
        bounds=LapBounds(0, 1_000_000_000),
        metadata=LapMetadata("A", 1, "r", 1.25),
    )
    b = prepare_lap_trace(
        RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36, 36)}, (0, 10)),
        bounds=LapBounds(0, 1_000_000_000),
        metadata=LapMetadata("B", 1, "r", 1.0),
    )
    result = align_laps(a, b)
    assert result.end_delta_seconds == 0
    assert result.observed_lap_delta_seconds == 0.25
    assert result.finish_delta_error_seconds == -0.25


def test_missing_speed_is_allowed_only_with_native_distance() -> None:
    raw = RawTelemetry((0, 1_000_000_000), {"gear": (2, 3)}, (0, 10))
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 1_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    assert "speed_kmh" not in align_laps(prepared, prepared).a.channels
    with pytest.raises(TraceRejected, match="distance_requires_speed"):
        prepare_lap_trace(
            RawTelemetry(raw.session_time_ns, raw.channels),
            bounds=prepared.bounds,
            metadata=prepared.metadata,
        )


def test_partial_derived_distance_origin_rejected_as_unknown() -> None:
    prepared = prepare_lap_trace(
        RawTelemetry((1_000_000_000, 2_000_000_000), {"speed_kmh": (36, 36)}),
        bounds=LapBounds(0, 2_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
        settings=PrepareSettings(allow_partial=True),
    )
    with pytest.raises(TraceRejected, match="distance_origin_unknown"):
        align_laps(prepared, trace(36))


def test_gap_edges_are_not_filled_or_forced_to_lap_bounds() -> None:
    prepared = prepare_lap_trace(
        RawTelemetry(
            (-5_000_000_000, 1_000_000_000, 2_000_000_000),
            {"speed_kmh": (36, 36, 36)},
            (-50, 10, 20),
        ),
        bounds=LapBounds(0, 2_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
        settings=PrepareSettings(allow_partial=True),
    )
    assert prepared.elapsed_seconds == (1, 2)
    assert prepared.distance_m == (10, 20)
    assert "lap_edge_gap" in prepared.flags


def test_grid_limit_and_reference_union_keep_endpoints() -> None:
    with pytest.raises(TraceRejected, match="grid_point_limit"):
        align_laps(trace(36), trace(36), settings=AlignmentSettings(max_points=10))
    result = align_laps(trace(36), trace(36), settings=AlignmentSettings(grid="reference_union"))
    assert result.distance_m == tuple(float(i * 10) for i in range(11))
    assert result.distance_m[0] == 0 and result.distance_m[-1] == 100


def test_unreliable_requires_optin_retaining_quality_flag() -> None:
    raw = RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36, 36)})
    bounds, metadata = LapBounds(0, 1_000_000_000), LapMetadata("A", 1, "r", reliable=False)
    with pytest.raises(TraceRejected, match="unreliable"):
        prepare_lap_trace(raw, bounds=bounds, metadata=metadata)
    prepared = prepare_lap_trace(
        raw, bounds=bounds, metadata=metadata, settings=PrepareSettings(allow_unreliable=True)
    )
    assert "unreliable" in prepared.flags


def test_cropped_start_domain_does_not_claim_full_finish_agreement() -> None:
    partial = prepare_lap_trace(
        RawTelemetry((1_000_000_000, 10_000_000_000), {"speed_kmh": (36, 36)}, (10, 100)),
        bounds=LapBounds(0, 10_000_000_000),
        metadata=LapMetadata("A", 1, "r", 10),
        settings=PrepareSettings(allow_partial=True),
    )
    result = align_laps(partial, trace(36))
    assert result.end_delta_seconds == 0
    assert not result.finish_line_agreement_applicable
    assert result.finish_delta_error_seconds is None


def test_large_physical_gap_mask_even_when_clock_gap_small() -> None:
    prepared = prepare_lap_trace(
        RawTelemetry((0, 1_000_000_000), {"speed_kmh": (540, 540)}, (0, 150)),
        bounds=LapBounds(0, 1_000_000_000),
        metadata=LapMetadata("A", 1, "r"),
    )
    result = align_laps(prepared, prepared)
    assert result.valid_mask[0] and result.valid_mask[-1]
    assert not any(result.valid_mask[1:-1])


def test_pit_and_deleted_optin_keeps_reasons() -> None:
    prepared = prepare_lap_trace(
        RawTelemetry((0, 1_000_000_000), {"speed_kmh": (36, 36)}),
        bounds=LapBounds(0, 1_000_000_000),
        metadata=LapMetadata("A", 1, "r", pit_in=True, deleted=True),
        settings=PrepareSettings(allow_pit_laps=True, allow_deleted=True),
    )
    assert "pit_in" in prepared.flags and "deleted" in prepared.flags


def test_raw_channel_mapping_is_immutable() -> None:
    channels = {"speed_kmh": (36, 36)}
    raw = RawTelemetry((0, 1_000_000_000), channels)
    channels["speed_kmh"] = (72, 72)
    assert raw.channels["speed_kmh"] == (36, 36)


def test_arrow_null_clock_rejected_instead_of_dropping_sample() -> None:
    table = pa.table(
        {
            "SessionTime": pa.array([0, None, 1_000_000_000], type=pa.duration("ns")),
            "Speed": [36, 36, 36],
        }
    )
    with pytest.raises(TraceRejected, match="null_clock"):
        prepare_arrow_trace(
            table,
            clock=ClockModel("session_time"),
            bounds=LapBounds(0, 1_000_000_000),
            metadata=LapMetadata("A", 1, "r"),
            channel_columns={"speed_kmh": "Speed"},
        )


def test_outside_lap_duplicates_do_not_poison_valid_local_support() -> None:
    raw = RawTelemetry(
        (0, 0, 1_000_000_000, 2_000_000_000, 3_000_000_000),
        {"speed_kmh": (36,) * 5},
        (0, 0, 10, 20, 30),
    )
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(1_000_000_000, 2_000_000_000), metadata=LapMetadata("A", 2, "r")
    )
    assert "duplicate_timestamp" not in prepared.flags
    assert prepared.source.session_time_ns == raw.session_time_ns
    assert align_laps(prepared, prepared).delta_seconds == (0.0,) * 11


def test_lap_with_only_two_outside_bracketing_samples_has_exact_edges() -> None:
    raw = RawTelemetry((-500_000_000, 1_500_000_000), {"speed_kmh": (36, 36)})
    prepared = prepare_lap_trace(
        raw, bounds=LapBounds(0, 1_000_000_000), metadata=LapMetadata("A", 1, "r")
    )
    assert prepared.elapsed_seconds == (0, 1)
    assert prepared.distance_m == (0, 10)
