"""Deterministic quality and capability evaluation for broad archive races."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from downforce_core.archive.contracts import DataQuality, QualityStatus, RaceDataCapabilities


def _non_null_count(table: pa.Table, column: str) -> int:
    null_count = cast(int, table.column(column).null_count)
    row_count = cast(int, table.num_rows)
    return row_count - null_count


def _duplicate_count(rows: list[dict[str, object]], fields: tuple[str, ...]) -> int:
    identities = [tuple(row.get(field) for field in fields) for row in rows]
    return len(identities) - len(set(identities))


def _invalid_integer_count(
    rows: list[dict[str, object]],
    field: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    invalid = 0
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        number = int(cast(int, value))
        if number < minimum or (maximum is not None and number > maximum):
            invalid += 1
    return invalid


def evaluate_archive_race(
    results: pa.Table,
    laps: pa.Table,
    pit_stops: pa.Table,
    *,
    season: int,
    validated_at_utc: str | None = None,
    expected_session_id: str | None = None,
) -> tuple[RaceDataCapabilities, DataQuality]:
    """Return evidence-based capabilities and a machine-readable quality result."""

    reasons: list[str] = []
    result_rows = cast(list[dict[str, object]], results.to_pylist())
    lap_rows = cast(list[dict[str, object]], laps.to_pylist())
    pit_rows = cast(list[dict[str, object]], pit_stops.to_pylist())
    result_drivers = {str(row["driver_id"]) for row in result_rows}

    duplicate_results = 0
    if results.num_rows:
        unique_drivers = int(pc.count_distinct(results.column("driver_id")).as_py())
        duplicate_results = results.num_rows - unique_drivers
        if duplicate_results:
            reasons.append("duplicate_result_driver")
    else:
        reasons.append("missing_results")

    invalid_grid_positions = _invalid_integer_count(
        result_rows,
        "grid_position",
        minimum=0,
        maximum=30,
    )
    invalid_finish_positions = _invalid_integer_count(
        result_rows,
        "finish_position",
        minimum=1,
        maximum=30,
    )
    invalid_result_laps = _invalid_integer_count(
        result_rows, "laps_completed", minimum=0, maximum=200
    )
    if invalid_grid_positions:
        reasons.append("invalid_grid_position")
    if invalid_finish_positions:
        reasons.append("invalid_finish_position")
    if invalid_result_laps:
        reasons.append("invalid_result_lap_count")

    session_ids = {
        str(row["session_id"])
        for row in (*result_rows, *lap_rows, *pit_rows)
        if row.get("session_id") is not None
    }
    mismatched_session_ids = max(0, len(session_ids) - 1)
    unexpected_session_id_rows = (
        sum(
            str(row.get("session_id")) != expected_session_id
            for row in (*result_rows, *lap_rows, *pit_rows)
        )
        if expected_session_id is not None
        else 0
    )
    if mismatched_session_ids:
        reasons.append("mixed_session_identity")
    if unexpected_session_id_rows:
        reasons.append("unexpected_session_identity")

    invalid_lap_times = 0
    missing_lap_times = False
    if laps.num_rows:
        timed = pc.drop_null(laps.column("lap_time_ms"))
        if len(timed):
            invalid_lap_times = int(pc.sum(pc.less_equal(timed, 0)).as_py())
            if invalid_lap_times:
                reasons.append("non_positive_lap_time")
        else:
            missing_lap_times = True
            reasons.append("lap_times_not_present")
    elif results.num_rows:
        reasons.append("missing_lap_data")

    duplicate_laps = _duplicate_count(lap_rows, ("driver_id", "lap_number"))
    orphan_lap_drivers = sum(str(row["driver_id"]) not in result_drivers for row in lap_rows)
    invalid_lap_numbers = _invalid_integer_count(
        lap_rows,
        "lap_number",
        minimum=1,
        maximum=200,
    )
    invalid_lap_positions = _invalid_integer_count(
        lap_rows,
        "position",
        minimum=1,
        maximum=30,
    )
    non_monotonic_laps = 0
    last_lap_by_driver: dict[str, int] = {}
    for row in lap_rows:
        driver_id = str(row["driver_id"])
        lap_number = int(cast(int, row["lap_number"]))
        previous = last_lap_by_driver.get(driver_id)
        if previous is not None and lap_number <= previous:
            non_monotonic_laps += 1
        last_lap_by_driver[driver_id] = lap_number
    if duplicate_laps:
        reasons.append("duplicate_driver_lap")
    if orphan_lap_drivers:
        reasons.append("lap_driver_missing_result")
    if invalid_lap_numbers:
        reasons.append("invalid_lap_number")
    if invalid_lap_positions:
        reasons.append("invalid_lap_position")
    if non_monotonic_laps:
        reasons.append("non_monotonic_driver_laps")

    invalid_pit_durations = 0
    if pit_stops.num_rows:
        durations = pc.drop_null(pit_stops.column("duration_ms"))
        if len(durations):
            invalid_pit_durations = int(pc.sum(pc.less_equal(durations, 0)).as_py())
            if invalid_pit_durations:
                reasons.append("non_positive_pit_duration")

    duplicate_pits = _duplicate_count(pit_rows, ("driver_id", "stop_number"))
    orphan_pit_drivers = sum(str(row["driver_id"]) not in result_drivers for row in pit_rows)
    invalid_stop_numbers = _invalid_integer_count(pit_rows, "stop_number", minimum=1)
    invalid_pit_laps = _invalid_integer_count(
        pit_rows,
        "lap_number",
        minimum=1,
        maximum=200,
    )
    stops_by_driver: dict[str, set[int]] = {}
    for row in pit_rows:
        stops_by_driver.setdefault(str(row["driver_id"]), set()).add(
            int(cast(int, row["stop_number"]))
        )
    pit_sequence_gaps = sum(
        bool(stops) and stops != set(range(1, max(stops) + 1)) for stops in stops_by_driver.values()
    )
    if duplicate_pits:
        reasons.append("duplicate_driver_pit_stop")
    if orphan_pit_drivers:
        reasons.append("pit_driver_missing_result")
    if invalid_stop_numbers:
        reasons.append("invalid_pit_stop_number")
    if invalid_pit_laps:
        reasons.append("invalid_pit_lap_number")
    if pit_sequence_gaps:
        reasons.append("pit_stop_sequence_gap")

    capabilities = RaceDataCapabilities(
        results=results.num_rows > 0,
        grid=results.num_rows > 0 and _non_null_count(results, "grid_position") > 0,
        lap_times=laps.num_rows > 0 and _non_null_count(laps, "lap_time_ms") > 0,
        lap_positions=laps.num_rows > 0 and _non_null_count(laps, "position") > 0,
        pit_stops=pit_stops.num_rows > 0,
    )

    structural_defects = sum(
        (
            duplicate_results,
            invalid_grid_positions,
            invalid_finish_positions,
            invalid_result_laps,
            mismatched_session_ids,
            unexpected_session_id_rows,
            duplicate_laps,
            orphan_lap_drivers,
            invalid_lap_numbers,
            invalid_lap_positions,
            non_monotonic_laps,
            invalid_lap_times,
            duplicate_pits,
            orphan_pit_drivers,
            invalid_stop_numbers,
            invalid_pit_laps,
            invalid_pit_durations,
        )
    )
    if not results.num_rows:
        status = QualityStatus.UNUSABLE
    elif structural_defects:
        status = QualityStatus.DEGRADED
    elif not laps.num_rows:
        status = QualityStatus.PARTIAL
    elif missing_lap_times or pit_sequence_gaps or (season >= 2011 and not pit_stops.num_rows):
        status = QualityStatus.GOOD
        if season >= 2011 and not pit_stops.num_rows:
            reasons.append("pit_stop_rows_not_present")
    else:
        status = QualityStatus.VERIFIED
    timestamp = validated_at_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    quality = DataQuality(
        status=status,
        reasons=tuple(reasons),
        metrics={
            "result_rows": results.num_rows,
            "lap_rows": laps.num_rows,
            "pit_stop_rows": pit_stops.num_rows,
            "duplicate_result_drivers": duplicate_results,
            "invalid_grid_positions": invalid_grid_positions,
            "invalid_finish_positions": invalid_finish_positions,
            "invalid_result_lap_counts": invalid_result_laps,
            "mismatched_session_ids": mismatched_session_ids,
            "unexpected_session_id_rows": unexpected_session_id_rows,
            "duplicate_driver_laps": duplicate_laps,
            "orphan_lap_drivers": orphan_lap_drivers,
            "invalid_lap_numbers": invalid_lap_numbers,
            "invalid_lap_positions": invalid_lap_positions,
            "non_monotonic_driver_laps": non_monotonic_laps,
            "invalid_lap_times": invalid_lap_times,
            "duplicate_driver_pit_stops": duplicate_pits,
            "orphan_pit_drivers": orphan_pit_drivers,
            "invalid_pit_stop_numbers": invalid_stop_numbers,
            "invalid_pit_lap_numbers": invalid_pit_laps,
            "pit_stop_sequence_gaps": pit_sequence_gaps,
            "invalid_pit_durations": invalid_pit_durations,
            "lap_times_present": not missing_lap_times,
        },
        validated_at_utc=timestamp,
    )
    return capabilities, quality


__all__ = ["evaluate_archive_race"]
