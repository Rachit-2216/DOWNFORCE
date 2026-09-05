"""Validation checks for timed observations and lazy telemetry indexes."""

from __future__ import annotations

from collections.abc import Iterable

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from downforce_core.domain.identifiers import DriverId
from downforce_core.domain.models import (
    RaceControlRecord,
    RacePositionRecord,
    WeatherRecord,
)
from downforce_core.normalization._validation_shared import (
    _check_reference,
    _duplicates,
    _issue,
)
from downforce_core.normalization.models import (
    NormalizedSession,
    ValidationIssue,
    ValidationLevel,
)

type _TimedRecord = WeatherRecord | RaceControlRecord | RacePositionRecord


def _check_one_timed_table(
    table: str,
    records_source: Iterable[_TimedRecord],
    driver_ids: set[DriverId],
    lap_keys: set[tuple[DriverId, int]],
    issues: list[ValidationIssue],
) -> None:
    records = tuple(records_source)
    times = [record.session_time_ms for record in records]
    if times != sorted(times):
        _issue(
            issues,
            ValidationLevel.ERROR,
            f"nonmonotonic-{table.replace('_', '-')}-time",
            table,
            "records must be sorted by nondecreasing session time",
        )
    keys: list[tuple[object, ...]] = []
    for record in records:
        driver_id: DriverId | None = None
        if isinstance(record, RacePositionRecord):
            driver_id = record.driver_id
        elif isinstance(record, RaceControlRecord):
            driver_id = record.driver_id
        if driver_id is not None:
            _check_reference(
                issues,
                driver_id=driver_id,
                driver_ids=driver_ids,
                table=table,
                row_key=f"driver={driver_id},time={record.session_time_ms}",
            )
        if (
            isinstance(record, RacePositionRecord)
            and record.lap_number is not None
            and (record.driver_id, record.lap_number) not in lap_keys
        ):
            _issue(
                issues,
                ValidationLevel.ERROR,
                "unknown-lap-reference",
                table,
                "race-position lap must exist in canonical laps for its driver",
                f"driver={record.driver_id},lap={record.lap_number}",
            )
        if isinstance(record, RaceControlRecord):
            discriminator: object = (
                record.source_kind,
                record.message,
                record.raw_status,
                record.category,
                record.scope,
                record.lap_number,
            )
        elif isinstance(record, RacePositionRecord):
            discriminator = record.lap_number
        else:
            discriminator = None
        keys.append((driver_id, record.session_time_ms, discriminator))
    for key in _duplicates(keys):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-canonical-key",
            table,
            "canonical observation key must be unique",
            str(key),
        )


def _check_timed_records(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    lap_keys: set[tuple[DriverId, int]],
    issues: list[ValidationIssue],
) -> None:
    _check_one_timed_table("weather", session.weather, driver_ids, lap_keys, issues)
    _check_one_timed_table("race_control", session.race_control, driver_ids, lap_keys, issues)
    _check_one_timed_table("race_positions", session.race_positions, driver_ids, lap_keys, issues)


def _check_track_positions(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    issues: list[ValidationIssue],
) -> None:
    table = session.track_positions.table
    if table.num_rows == 0:
        return
    times = table.column("session_time_ms")
    if table.num_rows > 1 and not bool(
        pc.all(pc.greater_equal(times.slice(1), times.slice(0, table.num_rows - 1))).as_py()
    ):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "nonmonotonic-track-positions-time",
            "track_positions",
            "records must be sorted by nondecreasing session time",
        )
    raw_drivers = table.column("driver_id")
    drivers = pc.cast(raw_drivers, pa.string())
    for raw_driver in sorted(pc.unique(drivers).to_pylist()):
        driver_id = DriverId(raw_driver)
        _check_reference(
            issues,
            driver_id=driver_id,
            driver_ids=driver_ids,
            table="track_positions",
            row_key=f"driver={driver_id}",
        )
    if table.num_rows > 1:
        keys = pa.table({"driver_id": drivers, "session_time_ms": times}).sort_by(
            [("driver_id", "ascending"), ("session_time_ms", "ascending")]
        )
        duplicate = pc.and_(
            pc.equal(
                keys.column("driver_id").slice(1),
                keys.column("driver_id").slice(0, keys.num_rows - 1),
            ),
            pc.equal(
                keys.column("session_time_ms").slice(1),
                keys.column("session_time_ms").slice(0, keys.num_rows - 1),
            ),
        )
        if bool(pc.any(duplicate).as_py()):
            _issue(
                issues,
                ValidationLevel.ERROR,
                "duplicate-canonical-key",
                "track_positions",
                "driver/session-time canonical key must be unique",
            )


def _check_telemetry(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    issues: list[ValidationIssue],
) -> None:
    for data_key in _duplicates(record.data_key for record in session.telemetry_index):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-telemetry-key",
            "telemetry_index",
            "telemetry data_key must be unique",
            str(data_key),
        )
    previous_by_driver: dict[DriverId, int] = {}
    for record in session.telemetry_index:
        _check_reference(
            issues,
            driver_id=record.driver_id,
            driver_ids=driver_ids,
            table="telemetry_index",
            row_key=record.data_key,
        )
        if record.sample_count == 0 and record.end_time_ms > record.start_time_ms:
            _issue(
                issues,
                ValidationLevel.WARNING,
                "empty-telemetry-range",
                "telemetry_index",
                "nonempty time range reports zero lazy samples",
                record.data_key,
            )
        previous = previous_by_driver.get(record.driver_id)
        if previous is not None and record.start_time_ms < previous:
            _issue(
                issues,
                ValidationLevel.ERROR,
                "nonmonotonic-telemetry-time",
                "telemetry_index",
                "telemetry ranges must be sorted by nondecreasing start time per driver",
                record.data_key,
            )
        previous_by_driver[record.driver_id] = record.start_time_ms


def check_observation_records(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    lap_keys: set[tuple[DriverId, int]],
    issues: list[ValidationIssue],
) -> None:
    """Validate normalized timed observations and lazy telemetry metadata."""

    _check_timed_records(session, driver_ids, lap_keys, issues)
    _check_track_positions(session, driver_ids, issues)
    _check_telemetry(session, driver_ids, issues)
