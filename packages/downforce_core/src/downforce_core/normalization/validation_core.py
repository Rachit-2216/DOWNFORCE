"""Validation checks for canonical roster, lap, stint, and pit records."""

from __future__ import annotations

from collections import defaultdict

from downforce_core.domain.identifiers import DriverId
from downforce_core.domain.models import StintRecord
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


def _check_driver_roster(
    session: NormalizedSession,
    issues: list[ValidationIssue],
) -> set[DriverId]:
    if not session.drivers:
        _issue(
            issues,
            ValidationLevel.ERROR,
            "missing-drivers",
            "drivers",
            "at least one roster driver is required",
        )
    driver_ids = {driver.driver_id for driver in session.drivers}
    for driver_id in _duplicates(driver.driver_id for driver in session.drivers):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-driver-id",
            "drivers",
            "driver_id must be unique within a session",
            str(driver_id),
        )
    numbers = [
        driver.racing_number for driver in session.drivers if driver.racing_number is not None
    ]
    for number in _duplicates(numbers):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-driver-number",
            "drivers",
            "racing_number must be unique within a session",
            f"number={number}",
        )
    return driver_ids


def _check_classifications(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    issues: list[ValidationIssue],
) -> None:
    for driver_id in _duplicates(
        classification.driver_id for classification in session.classifications
    ):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-classification-key",
            "classifications",
            "one classification row is allowed per driver",
            str(driver_id),
        )
    for classification in session.classifications:
        _check_reference(
            issues,
            driver_id=classification.driver_id,
            driver_ids=driver_ids,
            table="classifications",
            row_key=str(classification.driver_id),
        )


def _check_laps(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    issues: list[ValidationIssue],
) -> set[tuple[DriverId, int]]:
    if not session.laps:
        _issue(
            issues,
            ValidationLevel.ERROR,
            "missing-laps",
            "laps",
            "at least one canonical lap is required",
        )
    keys = [(lap.driver_id, lap.lap_number) for lap in session.laps]
    for driver_id, lap_number in _duplicates(keys):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-lap-key",
            "laps",
            "driver/lap canonical key must be unique",
            f"driver={driver_id},lap={lap_number}",
        )
    previous_by_driver: dict[DriverId, tuple[int, int | None]] = {}
    for lap in session.laps:
        row_key = f"driver={lap.driver_id},lap={lap.lap_number}"
        _check_reference(
            issues,
            driver_id=lap.driver_id,
            driver_ids=driver_ids,
            table="laps",
            row_key=row_key,
        )
        previous = previous_by_driver.get(lap.driver_id)
        if previous is not None:
            previous_lap, previous_end = previous
            if lap.lap_number <= previous_lap or (
                previous_end is not None
                and lap.lap_end_time_ms is not None
                and lap.lap_end_time_ms < previous_end
            ):
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "nonmonotonic-lap-time",
                    "laps",
                    "laps must be sorted with nondecreasing observed end times",
                    row_key,
                )
        last_known_end = lap.lap_end_time_ms
        if last_known_end is None and previous is not None:
            last_known_end = previous[1]
        previous_by_driver[lap.driver_id] = (lap.lap_number, last_known_end)
        if (
            lap.lap_start_time_ms is not None
            and lap.lap_end_time_ms is not None
            and lap.lap_time_ms is not None
            and lap.lap_end_time_ms - lap.lap_start_time_ms != lap.lap_time_ms
        ):
            _issue(
                issues,
                ValidationLevel.WARNING,
                "lap-duration-mismatch",
                "laps",
                "observed lap duration differs from its start/end range",
                row_key,
            )
    return set(keys)


def _check_stints(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    lap_keys: set[tuple[DriverId, int]],
    issues: list[ValidationIssue],
) -> None:
    keys = [(stint.driver_id, stint.stint_number) for stint in session.stints]
    for driver_id, number in _duplicates(keys):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-stint-key",
            "stints",
            "driver/stint canonical key must be unique",
            f"driver={driver_id},stint={number}",
        )
    laps_by_stint: dict[tuple[DriverId, int], list[int]] = defaultdict(list)
    for lap in session.laps:
        if lap.stint_number is not None:
            laps_by_stint[(lap.driver_id, lap.stint_number)].append(lap.lap_number)
    previous_by_driver: dict[DriverId, StintRecord] = {}
    for stint in session.stints:
        row_key = f"driver={stint.driver_id},stint={stint.stint_number}"
        _check_reference(
            issues,
            driver_id=stint.driver_id,
            driver_ids=driver_ids,
            table="stints",
            row_key=row_key,
        )
        observed = laps_by_stint.get((stint.driver_id, stint.stint_number), [])
        if not observed or min(observed) != stint.start_lap or max(observed) != stint.end_lap:
            _issue(
                issues,
                ValidationLevel.WARNING,
                "stint-range-mismatch",
                "stints",
                "derived stint bounds do not match observed stint laps",
                row_key,
            )
        if (stint.driver_id, stint.start_lap) not in lap_keys:
            _issue(
                issues,
                ValidationLevel.WARNING,
                "stint-start-lap-missing",
                "stints",
                "stint start lap is absent from canonical laps",
                row_key,
            )
        if stint.end_lap is not None and (stint.driver_id, stint.end_lap) not in lap_keys:
            _issue(
                issues,
                ValidationLevel.WARNING,
                "stint-end-lap-missing",
                "stints",
                "stint end lap is absent from canonical laps",
                row_key,
            )
        previous = previous_by_driver.get(stint.driver_id)
        if previous is not None:
            if stint.start_lap < previous.start_lap:
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "nonmonotonic-stint-range",
                    "stints",
                    "stints must be sorted by nondecreasing start lap per driver",
                    row_key,
                )
            if previous.end_lap is not None and stint.start_lap <= previous.end_lap:
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "overlapping-stint-range",
                    "stints",
                    "inclusive stint lap ranges must not overlap",
                    row_key,
                )
            if (
                previous.end_time_ms is not None
                and stint.start_time_ms is not None
                and stint.start_time_ms < previous.end_time_ms
            ):
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "overlapping-stint-time",
                    "stints",
                    "stint time ranges must not overlap or move backward",
                    row_key,
                )
        previous_by_driver[stint.driver_id] = stint


def _check_pits(
    session: NormalizedSession,
    driver_ids: set[DriverId],
    lap_keys: set[tuple[DriverId, int]],
    issues: list[ValidationIssue],
) -> None:
    keys = [(pit.driver_id, pit.stop_number) for pit in session.pit_stops]
    for driver_id, stop in _duplicates(keys):
        _issue(
            issues,
            ValidationLevel.ERROR,
            "duplicate-pit-key",
            "pit_stops",
            "driver/stop canonical key must be unique",
            f"driver={driver_id},stop={stop}",
        )
    previous_by_driver: dict[DriverId, int] = {}
    for pit in session.pit_stops:
        row_key = f"driver={pit.driver_id},stop={pit.stop_number}"
        _check_reference(
            issues,
            driver_id=pit.driver_id,
            driver_ids=driver_ids,
            table="pit_stops",
            row_key=row_key,
        )
        if pit.lap_number is not None and (pit.driver_id, pit.lap_number) not in lap_keys:
            _issue(
                issues,
                ValidationLevel.WARNING,
                "pit-lap-missing",
                "pit_stops",
                "pit observation references a lap absent from canonical laps",
                row_key,
            )
        if pit.pit_in_time_ms is not None and pit.pit_out_time_ms is not None:
            expected = pit.pit_out_time_ms - pit.pit_in_time_ms
            if pit.pit_lane_duration_ms != expected:
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "pit-duration-mismatch",
                    "pit_stops",
                    "pit lane duration must equal exit minus entry",
                    row_key,
                )
        observed_time = (
            pit.pit_in_time_ms if pit.pit_in_time_ms is not None else pit.pit_out_time_ms
        )
        previous_time = previous_by_driver.get(pit.driver_id)
        if observed_time is not None:
            if previous_time is not None and observed_time < previous_time:
                _issue(
                    issues,
                    ValidationLevel.ERROR,
                    "nonmonotonic-pit-time",
                    "pit_stops",
                    "pit stops must be sorted by nondecreasing observation time per driver",
                    row_key,
                )
            previous_by_driver[pit.driver_id] = observed_time


def check_core_records(
    session: NormalizedSession,
    issues: list[ValidationIssue],
) -> tuple[set[DriverId], set[tuple[DriverId, int]]]:
    """Validate core records and return roster identifiers and canonical lap keys."""

    driver_ids = _check_driver_roster(session, issues)
    _check_classifications(session, driver_ids, issues)
    lap_keys = _check_laps(session, driver_ids, issues)
    _check_stints(session, driver_ids, lap_keys, issues)
    _check_pits(session, driver_ids, lap_keys, issues)
    return driver_ids, lap_keys
