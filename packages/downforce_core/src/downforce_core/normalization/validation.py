"""Read-only validation for immutable normalized session aggregates."""

from __future__ import annotations

from downforce_core.normalization._validation_shared import (
    _check_completeness,
    _check_session_ids,
)
from downforce_core.normalization.models import (
    NormalizedSession,
    ValidationIssue,
    ValidationLevel,
    ValidationReport,
)
from downforce_core.normalization.validation_core import check_core_records
from downforce_core.normalization.validation_observations import check_observation_records


def validate_normalized_session(session: NormalizedSession) -> ValidationReport:
    """Return issues without modifying or repairing canonical records."""

    if not isinstance(session, NormalizedSession):
        raise TypeError("session must be a NormalizedSession")
    issues: list[ValidationIssue] = []
    _check_completeness(session, issues)
    _check_session_ids(session, issues)
    driver_ids, lap_keys = check_core_records(session, issues)
    check_observation_records(session, driver_ids, lap_keys, issues)
    issues.sort(
        key=lambda issue: (
            issue.level.value,
            issue.table,
            issue.code,
            issue.row_key or "",
            issue.message,
        )
    )
    return ValidationReport(tuple(issues))


__all__ = [
    "ValidationIssue",
    "ValidationLevel",
    "ValidationReport",
    "validate_normalized_session",
]
