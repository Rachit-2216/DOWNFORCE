from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime

import pytest
from downforce_core.domain.enums import (
    DataQuality,
    DriverStatus,
    SessionType,
    TrackStatus,
    TyreCompound,
)
from downforce_core.domain.identifiers import make_driver_id, make_session_id
from downforce_core.domain.models import (
    DriverClassificationRecord,
    DriverRecord,
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
    SessionMetadata,
    SourceProvenance,
    StintRecord,
    TelemetryIndexRecord,
    TrackPositionRecord,
    WeatherRecord,
)
from downforce_core.exceptions import (
    DownforceError,
    NormalizationError,
    ProviderCapabilityError,
    ProviderUnavailableError,
    ReplayCursorError,
    SchemaVersionError,
    SessionDataIncompleteError,
    SessionNotFoundError,
    StorageIntegrityError,
)
from downforce_core.versions import (
    CANONICAL_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    REPLAY_VERSION,
    TIMELINE_VERSION,
)

SESSION_ID = make_session_id(2024, "British Grand Prix", SessionType.RACE)
DRIVER_ID = make_driver_id(SESSION_ID, "VER")
PROVENANCE = SourceProvenance(
    provider="fixture",
    provider_version="1.0",
    source="fixture.laps",
    retrieved_at=datetime(2024, 7, 7, 16, tzinfo=UTC),
)
CLASSIFICATION_PROVENANCE = SourceProvenance(
    provider="fixture",
    provider_version="1.0",
    source="fixture.classification",
    retrieved_at=datetime(2024, 7, 7, 18, tzinfo=UTC),
)


@pytest.mark.parametrize(
    ("enum_type", "known", "expected"),
    [
        (TyreCompound, "SOFT", TyreCompound.SOFT),
        (TyreCompound, "intermediate", TyreCompound.INTERMEDIATE),
        (DriverStatus, "Finished", DriverStatus.FINISHED),
        (DriverStatus, "Retired", DriverStatus.RETIRED),
        (TrackStatus, "4", TrackStatus.SAFETY_CAR),
        (TrackStatus, "Red Flag", TrackStatus.RED_FLAG),
        (SessionType, "FP1", SessionType.PRACTICE_1),
        (SessionType, "Race", SessionType.RACE),
        (DataQuality, "complete", DataQuality.COMPLETE),
    ],
)
def test_controlled_enums_parse_known_provider_values(
    enum_type: type[object], known: str, expected: object
) -> None:
    assert enum_type.from_raw(known) is expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", TrackStatus.CLEAR),
        ("2", TrackStatus.YELLOW),
        ("3", TrackStatus.UNKNOWN),
        ("4", TrackStatus.SAFETY_CAR),
        ("5", TrackStatus.RED_FLAG),
        ("6", TrackStatus.VIRTUAL_SAFETY_CAR),
        ("7", TrackStatus.VSC_ENDING),
        ("0", TrackStatus.UNKNOWN),
        ("99", TrackStatus.UNKNOWN),
        (None, TrackStatus.UNKNOWN),
    ],
)
def test_fastf1_track_status_codes_are_mapped_without_guessing(
    raw: str | None, expected: TrackStatus
) -> None:
    assert TrackStatus.from_raw(raw) is expected


def test_unknown_track_status_code_is_preserved_beside_fallback() -> None:
    record = RaceControlRecord(
        session_id=SESSION_ID,
        session_time_ms=0,
        message="DOUBLE YELLOW SEGMENT",
        provenance=PROVENANCE,
        track_status=TrackStatus.from_raw("3"),
        raw_status="3",
    )
    assert record.track_status is TrackStatus.UNKNOWN
    assert record.raw_status == "3"


@pytest.mark.parametrize(
    ("enum_type", "unknown"),
    [
        (TyreCompound, "HYPERSOFT"),
        (DriverStatus, "Mystery status"),
        (TrackStatus, "99"),
        (SessionType, "Warmup"),
        (DataQuality, "unrated"),
        (TyreCompound, None),
    ],
)
def test_controlled_enums_have_unknown_fallbacks(
    enum_type: type[object], unknown: str | None
) -> None:
    assert enum_type.from_raw(unknown).value == "unknown"  # type: ignore[attr-defined]


def test_canonical_versions_are_explicit_nonempty_strings() -> None:
    versions = (
        CANONICAL_SCHEMA_VERSION,
        NORMALIZATION_VERSION,
        TIMELINE_VERSION,
        REPLAY_VERSION,
    )
    assert all(isinstance(version, str) and version for version in versions)


@pytest.mark.parametrize(
    "error_type",
    [
        ProviderUnavailableError,
        SessionNotFoundError,
        ProviderCapabilityError,
        NormalizationError,
        SchemaVersionError,
        ReplayCursorError,
        SessionDataIncompleteError,
        StorageIntegrityError,
    ],
)
def test_typed_errors_share_downforce_root(error_type: type[DownforceError]) -> None:
    error = error_type("actionable context")
    assert isinstance(error, DownforceError)
    assert str(error) == "actionable context"


def test_models_expose_unit_bearing_and_distinct_position_fields() -> None:
    model_fields = {
        model.__name__: {field.name for field in fields(model)}
        for model in (
            LapRecord,
            PitStopRecord,
            WeatherRecord,
            RaceControlRecord,
            RacePositionRecord,
            TrackPositionRecord,
            TelemetryIndexRecord,
        )
    }

    assert {
        "lap_start_time_ms",
        "lap_end_time_ms",
        "lap_time_ms",
        "sector_1_time_ms",
        "sector_2_time_ms",
        "sector_3_time_ms",
    } <= model_fields["LapRecord"]
    assert {
        "pit_in_time_ms",
        "pit_out_time_ms",
        "pit_lane_duration_ms",
        "stationary_duration_ms",
    } <= model_fields["PitStopRecord"]
    assert "pit_duration_ms" not in model_fields["PitStopRecord"]
    assert {"session_time_ms", "air_temperature_c", "wind_speed_mps"} <= model_fields[
        "WeatherRecord"
    ]
    assert "position" in model_fields["RacePositionRecord"]
    assert {"x_m", "y_m", "z_m"} <= model_fields["TrackPositionRecord"]
    assert "position" not in model_fields["TrackPositionRecord"]
    assert "x_m" not in model_fields["RacePositionRecord"]
    assert "channel_names" in model_fields["TelemetryIndexRecord"]
    assert "samples" not in model_fields["TelemetryIndexRecord"]


def test_all_required_canonical_records_are_immutable_and_accept_explicit_nulls() -> None:
    records = (
        SessionMetadata(
            session_id=SESSION_ID,
            season=2024,
            event_name="British Grand Prix",
            session_name="Race",
            session_type=SessionType.RACE,
            provenance=PROVENANCE,
            session_start_utc=None,
            data_quality=DataQuality.PARTIAL,
        ),
        DriverRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            provenance=PROVENANCE,
            racing_number=1,
            abbreviation="VER",
            full_name=None,
        ),
        DriverClassificationRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            provenance=CLASSIFICATION_PROVENANCE,
            classified_position=1,
            status=DriverStatus.FINISHED,
            points=25.0,
            raw_status="Finished",
        ),
        LapRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            lap_number=1,
            provenance=PROVENANCE,
            lap_start_time_ms=0,
            lap_end_time_ms=90_000,
            lap_time_ms=90_000,
            sector_1_time_ms=None,
            compound=TyreCompound.UNKNOWN,
            raw_compound="HYPERSOFT",
        ),
        StintRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            stint_number=1,
            start_lap=1,
            provenance=PROVENANCE,
            end_lap=None,
            compound=TyreCompound.SOFT,
            raw_compound="SOFT",
        ),
        PitStopRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            stop_number=1,
            provenance=PROVENANCE,
            lap_number=20,
            pit_in_time_ms=1_800_000,
            pit_out_time_ms=None,
            pit_lane_duration_ms=None,
            stationary_duration_ms=None,
        ),
        WeatherRecord(
            session_id=SESSION_ID,
            session_time_ms=0,
            provenance=PROVENANCE,
            air_temperature_c=21.5,
            track_temperature_c=None,
            rainfall=None,
        ),
        RaceControlRecord(
            session_id=SESSION_ID,
            session_time_ms=1_000,
            message="YELLOW FLAG",
            provenance=PROVENANCE,
            track_status=TrackStatus.YELLOW,
            raw_status="YELLOW",
            driver_id=None,
        ),
        RacePositionRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            session_time_ms=90_000,
            position=1,
            provenance=PROVENANCE,
            lap_number=1,
        ),
        TrackPositionRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            session_time_ms=1_000,
            x_m=100.0,
            y_m=200.0,
            provenance=PROVENANCE,
            z_m=None,
            raw_status="OnTrack",
        ),
        TelemetryIndexRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            start_time_ms=0,
            end_time_ms=90_000,
            data_key="telemetry-ver-lap-1",
            channel_names=("Speed", "RPM"),
            sample_count=900,
            provenance=PROVENANCE,
            lap_number=1,
        ),
    )

    assert len(records) == 11
    for record in records:
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            record.session_id = SESSION_ID  # type: ignore[misc]


def test_raw_unknown_values_are_preserved_without_empty_string_sentinels() -> None:
    lap = LapRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        lap_number=1,
        provenance=PROVENANCE,
        compound=TyreCompound.UNKNOWN,
        raw_compound="HYPERSOFT",
    )
    assert lap.compound is TyreCompound.UNKNOWN
    assert lap.raw_compound == "HYPERSOFT"

    classification = DriverClassificationRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        provenance=CLASSIFICATION_PROVENANCE,
        status=DriverStatus.UNKNOWN,
        raw_status="Power Unit",
    )
    assert classification.status is DriverStatus.UNKNOWN
    assert classification.raw_status == "Power Unit"

    with pytest.raises(ValueError, match="empty strings"):
        DriverRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            provenance=PROVENANCE,
            abbreviation="",
        )


def test_session_metadata_identity_fields_must_match_session_id() -> None:
    with pytest.raises(ValueError, match="season must match"):
        SessionMetadata(
            session_id=SESSION_ID,
            season=2025,
            event_name="British Grand Prix",
            session_name="Race",
            session_type=SessionType.RACE,
            provenance=PROVENANCE,
        )

    round_session_id = make_session_id(2024, 12, SessionType.RACE)
    with pytest.raises(ValueError, match="round_number must match"):
        SessionMetadata(
            session_id=round_session_id,
            season=2024,
            event_name="British Grand Prix",
            session_name="Race",
            session_type=SessionType.RACE,
            provenance=PROVENANCE,
            round_number=11,
        )

    matching_round = SessionMetadata(
        session_id=round_session_id,
        season=2024,
        event_name="British Grand Prix",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=PROVENANCE,
        round_number=12,
    )
    assert matching_round.round_number == 12

    named_alias = SessionMetadata(
        session_id=SESSION_ID,
        season=2024,
        event_name="Provider-specific event alias",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=PROVENANCE,
        round_number=12,
    )
    assert named_alias.round_number == 12
    with pytest.raises(ValueError, match="session_type must match"):
        SessionMetadata(
            session_id=SESSION_ID,
            season=2024,
            event_name="British Grand Prix",
            session_name="Race",
            session_type=SessionType.QUALIFYING,
            provenance=PROVENANCE,
        )


def test_canonical_observation_times_and_counts_reject_negative_values() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        WeatherRecord(
            session_id=SESSION_ID,
            session_time_ms=-1,
            provenance=PROVENANCE,
        )
    with pytest.raises(ValueError, match="positive"):
        RacePositionRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            session_time_ms=0,
            position=0,
            provenance=PROVENANCE,
        )
    with pytest.raises(ValueError, match="nonnegative"):
        TelemetryIndexRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            start_time_ms=0,
            end_time_ms=1,
            data_key="telemetry-ver-lap-1",
            channel_names=(),
            sample_count=-1,
            provenance=PROVENANCE,
        )
    with pytest.raises(TypeError):
        TrackPositionRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            session_time_ms=0,
            x_m=None,  # type: ignore[arg-type]
            y_m=1.0,
            provenance=PROVENANCE,
        )


def test_provenance_requires_aware_utc_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceProvenance(
            provider="fixture",
            provider_version="1.0",
            source="fixture.laps",
            retrieved_at=datetime(2024, 7, 7, 16),
        )


def test_roster_type_cannot_carry_final_classification_facts() -> None:
    roster = (
        DriverRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            provenance=PROVENANCE,
            racing_number=0,
            abbreviation="VER",
        ),
    )
    roster_fields = {field.name for field in fields(DriverRecord)}
    assert {
        "classified_position",
        "status",
        "points",
        "raw_status",
        "final_classification_position",
        "final_status",
        "final_points",
        "raw_final_status",
    }.isdisjoint(roster_fields)
    assert not hasattr(roster[0], "final_status")

    classification = DriverClassificationRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        provenance=CLASSIFICATION_PROVENANCE,
        classified_position=1,
        status=DriverStatus.FINISHED,
        points=25.0,
        raw_status="Finished",
    )
    assert classification.driver_id == roster[0].driver_id
    assert classification.provenance.source == "fixture.classification"


def test_historical_driver_number_zero_is_valid_but_negative_is_not() -> None:
    driver = DriverRecord(
        session_id=SESSION_ID,
        driver_id=make_driver_id(SESSION_ID, 0),
        provenance=PROVENANCE,
        racing_number=0,
    )
    assert driver.racing_number == 0

    with pytest.raises(ValueError, match="nonnegative"):
        DriverRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            provenance=PROVENANCE,
            racing_number=-1,
        )


def test_session_metadata_carries_distinct_aware_fastf1_session_origin() -> None:
    origin = datetime(2024, 7, 7, 13, 59, 58, tzinfo=UTC)
    metadata = SessionMetadata(
        session_id=SESSION_ID,
        season=2024,
        event_name="British Grand Prix",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=PROVENANCE,
        scheduled_start_utc=datetime(2024, 7, 7, 13, 55, tzinfo=UTC),
        session_start_utc=datetime(2024, 7, 7, 14, tzinfo=UTC),
        session_origin_utc=origin,
    )

    assert metadata.scheduled_start_utc == datetime(2024, 7, 7, 13, 55, tzinfo=UTC)
    assert metadata.session_start_utc == datetime(2024, 7, 7, 14, tzinfo=UTC)
    assert metadata.session_origin_utc == origin
    with pytest.raises(ValueError, match="timezone-aware"):
        SessionMetadata(
            session_id=SESSION_ID,
            season=2024,
            event_name="British Grand Prix",
            session_name="Race",
            session_type=SessionType.RACE,
            provenance=PROVENANCE,
            session_origin_utc=datetime(2024, 7, 7, 13, 59, 58),
        )


def test_lap_preserves_fastf1_generation_deletion_and_track_status_facts() -> None:
    lap = LapRecord(
        session_id=SESSION_ID,
        driver_id=DRIVER_ID,
        lap_number=1,
        provenance=PROVENANCE,
        is_generated=True,
        is_deleted=False,
        deleted_reason="TRACK LIMITS",
        raw_track_status="12",
    )

    assert lap.is_generated is True
    assert lap.is_deleted is False
    assert lap.deleted_reason == "TRACK LIMITS"
    assert lap.raw_track_status == "12"

    with pytest.raises(TypeError, match="is_generated"):
        LapRecord(
            session_id=SESSION_ID,
            driver_id=DRIVER_ID,
            lap_number=1,
            provenance=PROVENANCE,
            is_generated=1,  # type: ignore[arg-type]
        )


def test_race_control_preserves_explicit_source_kind() -> None:
    record = RaceControlRecord(
        session_id=SESSION_ID,
        session_time_ms=0,
        message="Started",
        provenance=PROVENANCE,
        source_kind="session_status",
    )

    assert record.source_kind == "session_status"
