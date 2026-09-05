from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from downforce_core.domain import (
    DriverRecord,
    LapRecord,
    RacePositionRecord,
    SessionMetadata,
    SessionType,
    SourceProvenance,
    StintRecord,
    TelemetryIndexRecord,
    WeatherRecord,
    make_driver_id,
    make_session_id,
)
from downforce_core.normalization.models import NormalizedSession
from downforce_core.normalization.validation import (
    ValidationIssue,
    ValidationLevel,
    ValidationReport,
    validate_normalized_session,
)
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    SessionRef,
)

SESSION_ID = make_session_id(2024, "British Grand Prix", SessionType.RACE)
DRIVER_ID = make_driver_id(SESSION_ID, 0)
UNKNOWN_DRIVER_ID = make_driver_id(SESSION_ID, 99)
PROVENANCE = SourceProvenance(
    provider="fixture",
    provider_version="1.0",
    source="fixture",
    retrieved_at=datetime(2024, 7, 8, tzinfo=UTC),
    source_record_id="fixture-row",
)


def _session(**overrides: object) -> NormalizedSession:
    metadata = SessionMetadata(
        session_id=SESSION_ID,
        season=2024,
        event_name="British Grand Prix",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=PROVENANCE,
    )
    driver = DriverRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        provenance=PROVENANCE,
        racing_number=0,
    )
    lap = LapRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        lap_number=1,
        provenance=PROVENANCE,
        lap_start_time_ms=0,
        lap_end_time_ms=90_000,
        lap_time_ms=89_000,
        stint_number=1,
    )
    values: dict[str, object] = {
        "metadata": metadata,
        "drivers": (driver,),
        "classifications": (),
        "laps": (lap,),
        "stints": (),
        "pit_stops": (),
        "weather": (),
        "race_control": (),
        "race_positions": (),
        "track_positions": (),
        "telemetry_index": (),
        "capabilities": ProviderCapabilities(
            drivers=True,
            laps=True,
            weather=False,
            race_control=False,
            race_positions=False,
            track_positions=False,
            car_telemetry=False,
            live=False,
        ),
        "completeness": {
            name: (
                DatasetAvailability.AVAILABLE
                if name in {DatasetName.DRIVERS, DatasetName.LAPS}
                else DatasetAvailability.NOT_REQUESTED
            )
            for name in DatasetName
        },
        "warnings": (),
        "provider_name": "fixture",
        "provider_version": "1.0",
        "retrieved_at": datetime(2024, 7, 8, tzinfo=UTC),
        "provider_metadata": {},
        "requested_session": SessionRef(2024, "British Grand Prix", "R"),
        "validation_report": ValidationReport(),
    }
    values.update(overrides)
    return NormalizedSession(**values)  # type: ignore[arg-type]


def test_validation_types_are_immutable_and_split_errors_from_warnings() -> None:
    error = ValidationIssue(
        level=ValidationLevel.ERROR,
        code="unknown-driver-reference",
        table="race_positions",
        message="driver is absent from roster",
        row_key="driver=99,time=1000",
    )
    warning = ValidationIssue(
        level=ValidationLevel.WARNING,
        code="lap-duration-mismatch",
        table="laps",
        message="lap duration differs from its observed range",
        row_key="driver=0,lap=1",
    )
    report = ValidationReport((warning, error))

    assert report.errors == (error,)
    assert report.warnings == (warning,)
    assert report.is_valid is False


def test_validation_reports_required_data_duplicates_references_and_ranges() -> None:
    valid = _session()
    warning_report = validate_normalized_session(valid)
    assert warning_report.errors == ()
    assert {issue.code for issue in warning_report.warnings} == {"lap-duration-mismatch"}

    duplicate_driver = valid.drivers[0]
    unknown_position = RacePositionRecord(
        session_id=SESSION_ID,
        driver_id=UNKNOWN_DRIVER_ID,
        session_time_ms=1_000,
        position=1,
        provenance=PROVENANCE,
        lap_number=1,
    )
    broken = replace(
        valid,
        drivers=(duplicate_driver, duplicate_driver),
        laps=(),
        race_positions=(unknown_position,),
    )
    report = validate_normalized_session(broken)

    assert {issue.code for issue in report.errors} >= {
        "duplicate-driver-id",
        "duplicate-driver-number",
        "missing-laps",
        "unknown-driver-reference",
    }


def test_validation_reports_nonmonotonic_times_and_stint_inconsistency() -> None:
    valid = _session()
    second_lap = LapRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        lap_number=2,
        provenance=PROVENANCE,
        lap_start_time_ms=80_000,
        lap_end_time_ms=85_000,
        lap_time_ms=5_000,
        stint_number=1,
    )
    stint = StintRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        stint_number=1,
        start_lap=1,
        end_lap=3,
        provenance=PROVENANCE,
    )
    report = validate_normalized_session(
        replace(valid, laps=(valid.laps[0], second_lap), stints=(stint,))
    )

    assert "nonmonotonic-lap-time" in {issue.code for issue in report.errors}
    assert "stint-range-mismatch" in {issue.code for issue in report.warnings}


def test_validation_rejects_overlapping_and_backward_stint_sequences() -> None:
    valid = _session()
    first = StintRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        stint_number=1,
        start_lap=1,
        end_lap=3,
        start_time_ms=0,
        end_time_ms=270_000,
        provenance=PROVENANCE,
    )
    overlapping = StintRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        stint_number=2,
        start_lap=3,
        end_lap=5,
        start_time_ms=260_000,
        end_time_ms=450_000,
        provenance=PROVENANCE,
    )
    backward = StintRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        stint_number=3,
        start_lap=2,
        end_lap=2,
        start_time_ms=180_000,
        end_time_ms=200_000,
        provenance=PROVENANCE,
    )

    report = validate_normalized_session(replace(valid, stints=(first, overlapping, backward)))

    codes = {issue.code for issue in report.errors}
    assert "overlapping-stint-range" in codes
    assert "overlapping-stint-time" in codes
    assert "nonmonotonic-stint-range" in codes


def test_validation_reports_foreign_session_rows_and_unsorted_telemetry_ranges() -> None:
    valid = _session()
    foreign_weather = WeatherRecord(
        session_id=make_session_id(2024, "Monaco Grand Prix", SessionType.RACE),
        session_time_ms=1_000,
        provenance=PROVENANCE,
    )
    later = TelemetryIndexRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        start_time_ms=100,
        end_time_ms=200,
        data_key="telemetry-later",
        channel_names=("Speed",),
        sample_count=2,
        provenance=PROVENANCE,
    )
    earlier = TelemetryIndexRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        start_time_ms=0,
        end_time_ms=50,
        data_key="telemetry-earlier",
        channel_names=("Speed",),
        sample_count=2,
        provenance=PROVENANCE,
    )

    report = validate_normalized_session(
        replace(
            valid,
            weather=(foreign_weather,),
            telemetry_index=(later, earlier),
        )
    )

    assert {issue.code for issue in report.errors} >= {
        "session-id-mismatch",
        "nonmonotonic-telemetry-time",
    }


def test_validation_accepts_existing_race_position_lap_reference() -> None:
    valid = _session()
    position = RacePositionRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        session_time_ms=90_000,
        position=1,
        provenance=PROVENANCE,
        lap_number=1,
    )

    report = validate_normalized_session(replace(valid, race_positions=(position,)))

    assert "unknown-lap-reference" not in {issue.code for issue in report.errors}


def test_validation_rejects_unknown_race_position_lap_reference() -> None:
    valid = _session()
    position = RacePositionRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        session_time_ms=90_000,
        position=1,
        provenance=PROVENANCE,
        lap_number=999,
    )

    report = validate_normalized_session(replace(valid, race_positions=(position,)))

    issue = next(issue for issue in report.errors if issue.code == "unknown-lap-reference")
    assert issue.table == "race_positions"
    assert issue.row_key == f"driver={DRIVER_ID},lap=999"
