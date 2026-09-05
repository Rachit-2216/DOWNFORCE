"""Shared primitives for read-only normalized-session validation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable
from typing import Protocol

from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.normalization.models import (
    NormalizedSession,
    ValidationIssue,
    ValidationLevel,
)
from downforce_core.providers.base import DatasetAvailability, DatasetName


class _SessionBoundRecord(Protocol):
    @property
    def session_id(self) -> SessionId: ...


def _issue(
    issues: list[ValidationIssue],
    level: ValidationLevel,
    code: str,
    table: str,
    message: str,
    row_key: str | None = None,
) -> None:
    issues.append(ValidationIssue(level, code, table, message, row_key))


def _duplicates[KeyT: Hashable](values: Iterable[KeyT]) -> tuple[KeyT, ...]:
    counts = Counter(values)
    return tuple(sorted((value for value, count in counts.items() if count > 1), key=str))


def _check_completeness(session: NormalizedSession, issues: list[ValidationIssue]) -> None:
    for name in (DatasetName.DRIVERS, DatasetName.LAPS):
        if session.completeness[name] is not DatasetAvailability.AVAILABLE:
            _issue(
                issues,
                ValidationLevel.ERROR,
                "required-dataset-state",
                name.value.replace("-", "_"),
                "required canonical data must originate from an available provider dataset",
                f"availability={session.completeness[name].value}",
            )


def _check_table_session_ids(
    expected: SessionId,
    table: str,
    records: Iterable[_SessionBoundRecord],
    issues: list[ValidationIssue],
) -> None:
    for index, record in enumerate(records):
        if record.session_id != expected:
            _issue(
                issues,
                ValidationLevel.ERROR,
                "session-id-mismatch",
                table,
                "record session_id must match canonical session metadata",
                f"index={index}",
            )


def _check_session_ids(session: NormalizedSession, issues: list[ValidationIssue]) -> None:
    expected = session.metadata.session_id
    _check_table_session_ids(expected, "drivers", session.drivers, issues)
    _check_table_session_ids(expected, "classifications", session.classifications, issues)
    _check_table_session_ids(expected, "laps", session.laps, issues)
    _check_table_session_ids(expected, "stints", session.stints, issues)
    _check_table_session_ids(expected, "pit_stops", session.pit_stops, issues)
    _check_table_session_ids(expected, "weather", session.weather, issues)
    _check_table_session_ids(expected, "race_control", session.race_control, issues)
    _check_table_session_ids(expected, "race_positions", session.race_positions, issues)
    if session.track_positions.session_id != expected:
        _issue(
            issues,
            ValidationLevel.ERROR,
            "session-id-mismatch",
            "track_positions",
            "track-position dataset session_id must match canonical session metadata",
        )
    _check_table_session_ids(expected, "telemetry_index", session.telemetry_index, issues)


def _check_reference(
    issues: list[ValidationIssue],
    *,
    driver_id: DriverId,
    driver_ids: set[DriverId],
    table: str,
    row_key: str,
) -> None:
    if driver_id not in driver_ids:
        _issue(
            issues,
            ValidationLevel.ERROR,
            "unknown-driver-reference",
            table,
            "record references a driver absent from the roster",
            row_key,
        )
