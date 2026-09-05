"""Immutable provider-neutral canonical records and their universal invariants."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from downforce_core.domain.enums import (
    DataQuality,
    DriverStatus,
    SessionType,
    TrackStatus,
    TyreCompound,
)
from downforce_core.domain.identifiers import (
    DriverId,
    SessionId,
    is_driver_id_for_session,
    validate_safe_identifier,
)
from downforce_core.domain.time import ensure_utc


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be nonempty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")
    if not value.strip():
        raise ValueError(
            f"{field_name} uses None for missing values; empty strings are not allowed"
        )
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")


def _validate_integer(value: int, field_name: str, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        qualifier = "nonnegative" if minimum == 0 else "positive"
        raise ValueError(f"{field_name} must be {qualifier}")


def _validate_optional_integer(value: int | None, field_name: str, *, minimum: int) -> None:
    if value is not None:
        _validate_integer(value, field_name, minimum=minimum)


def _validate_optional_number(
    value: float | None,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number or None")
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")


def _validate_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    if not isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _validate_session_id(session_id: SessionId) -> None:
    if not isinstance(session_id, SessionId):
        raise TypeError("session_id must be a SessionId")


def _validate_driver_id(driver_id: DriverId) -> None:
    if not isinstance(driver_id, DriverId):
        raise TypeError("driver_id must be a DriverId")


def _validate_driver_scope(session_id: SessionId, driver_id: DriverId) -> None:
    _validate_session_id(session_id)
    _validate_driver_id(driver_id)
    if not is_driver_id_for_session(driver_id, session_id):
        raise ValueError("driver_id must be scoped to session_id")


def _normalize_optional_utc(instance: object, field_name: str, value: datetime | None) -> None:
    if value is not None:
        object.__setattr__(
            instance,
            field_name,
            ensure_utc(value, field_name=field_name),
        )


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Source identity and knowledge-time evidence attached to canonical rows."""

    provider: str
    provider_version: str
    source: str
    retrieved_at: datetime
    source_record_id: str | None = None
    source_published_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.provider, "provider")
        _require_text(self.provider_version, "provider_version")
        _require_text(self.source, "source")
        _validate_optional_text(self.source_record_id, "source_record_id")
        object.__setattr__(
            self,
            "retrieved_at",
            ensure_utc(self.retrieved_at, field_name="retrieved_at"),
        )
        _normalize_optional_utc(self, "source_published_at", self.source_published_at)


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """Canonical identity and descriptive metadata for one session."""

    session_id: SessionId
    season: int
    event_name: str
    session_name: str
    session_type: SessionType
    provenance: SourceProvenance
    round_number: int | None = None
    country_code: str | None = None
    circuit_name: str | None = None
    scheduled_start_utc: datetime | None = None
    session_start_utc: datetime | None = None
    session_end_utc: datetime | None = None
    session_origin_utc: datetime | None = None
    data_quality: DataQuality = DataQuality.UNKNOWN

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_integer(self.season, "season", minimum=1950)
        if self.season > 9999:
            raise ValueError("season must be no greater than 9999")
        if self.session_id.season != self.season:
            raise ValueError("season must match the season encoded by session_id")
        _require_text(self.event_name, "event_name")
        _require_text(self.session_name, "session_name")
        if not isinstance(self.session_type, SessionType):
            raise TypeError("session_type must be a SessionType")
        if self.session_id.session_type is not self.session_type:
            raise ValueError("session_type must match the type encoded by session_id")
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.round_number, "round_number", minimum=1)
        if self.round_number is not None and self.session_id.event_selector.startswith("round-"):
            encoded_round = int(self.session_id.event_selector.removeprefix("round-"))
            if self.round_number != encoded_round:
                raise ValueError("round_number must match the round encoded by session_id")
        _validate_optional_text(self.country_code, "country_code")
        _validate_optional_text(self.circuit_name, "circuit_name")
        _normalize_optional_utc(self, "scheduled_start_utc", self.scheduled_start_utc)
        _normalize_optional_utc(self, "session_start_utc", self.session_start_utc)
        _normalize_optional_utc(self, "session_end_utc", self.session_end_utc)
        _normalize_optional_utc(self, "session_origin_utc", self.session_origin_utc)
        if self.session_start_utc is not None and self.session_end_utc is not None:
            if self.session_end_utc < self.session_start_utc:
                raise ValueError("session_end_utc must not precede session_start_utc")
        if not isinstance(self.data_quality, DataQuality):
            raise TypeError("data_quality must be a DataQuality")


@dataclass(frozen=True, slots=True)
class DriverRecord:
    """Session-scoped roster metadata that contains no final-result facts."""

    session_id: SessionId
    driver_id: DriverId
    provenance: SourceProvenance
    racing_number: int | None = None
    abbreviation: str | None = None
    full_name: str | None = None
    team_name: str | None = None
    country_code: str | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.racing_number, "racing_number", minimum=0)
        _validate_optional_text(self.abbreviation, "abbreviation")
        _validate_optional_text(self.full_name, "full_name")
        _validate_optional_text(self.team_name, "team_name")
        _validate_optional_text(self.country_code, "country_code")


@dataclass(frozen=True, slots=True)
class DriverClassificationRecord:
    """Post-session classification facts, isolated from replay-facing roster metadata."""

    session_id: SessionId
    driver_id: DriverId
    provenance: SourceProvenance
    classified_position: int | None = None
    status: DriverStatus = DriverStatus.UNKNOWN
    points: float | None = None
    raw_status: str | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.classified_position, "classified_position", minimum=1)
        if not isinstance(self.status, DriverStatus):
            raise TypeError("status must be a DriverStatus")
        _validate_optional_number(self.points, "points")
        _validate_optional_text(self.raw_status, "raw_status")


@dataclass(frozen=True, slots=True)
class LapRecord:
    """One canonical lap; every duration and session coordinate is integer milliseconds."""

    session_id: SessionId
    driver_id: DriverId
    lap_number: int
    provenance: SourceProvenance
    lap_start_time_ms: int | None = None
    lap_end_time_ms: int | None = None
    lap_time_ms: int | None = None
    sector_1_time_ms: int | None = None
    sector_2_time_ms: int | None = None
    sector_3_time_ms: int | None = None
    stint_number: int | None = None
    compound: TyreCompound = TyreCompound.UNKNOWN
    raw_compound: str | None = None
    tyre_life_laps: float | None = None
    is_personal_best: bool | None = None
    is_accurate: bool | None = None
    is_generated: bool | None = None
    is_deleted: bool | None = None
    deleted_reason: str | None = None
    raw_track_status: str | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.lap_number, "lap_number", minimum=1)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        for field_name in (
            "lap_start_time_ms",
            "lap_end_time_ms",
            "lap_time_ms",
            "sector_1_time_ms",
            "sector_2_time_ms",
            "sector_3_time_ms",
        ):
            _validate_optional_integer(getattr(self, field_name), field_name, minimum=0)
        if self.lap_start_time_ms is not None and self.lap_end_time_ms is not None:
            if self.lap_end_time_ms < self.lap_start_time_ms:
                raise ValueError("lap_end_time_ms must not precede lap_start_time_ms")
        _validate_optional_integer(self.stint_number, "stint_number", minimum=1)
        if not isinstance(self.compound, TyreCompound):
            raise TypeError("compound must be a TyreCompound")
        _validate_optional_text(self.raw_compound, "raw_compound")
        _validate_optional_number(self.tyre_life_laps, "tyre_life_laps", minimum=0)
        for field_name in ("is_personal_best", "is_accurate", "is_generated", "is_deleted"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{field_name} must be a bool or None")
        _validate_optional_text(self.deleted_reason, "deleted_reason")
        _validate_optional_text(self.raw_track_status, "raw_track_status")


@dataclass(frozen=True, slots=True)
class StintRecord:
    """A contiguous tyre stint with inclusive lap bounds and integer session times."""

    session_id: SessionId
    driver_id: DriverId
    stint_number: int
    start_lap: int
    provenance: SourceProvenance
    end_lap: int | None = None
    start_time_ms: int | None = None
    end_time_ms: int | None = None
    compound: TyreCompound = TyreCompound.UNKNOWN
    raw_compound: str | None = None
    tyre_life_start_laps: float | None = None
    tyre_life_end_laps: float | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.stint_number, "stint_number", minimum=1)
        _validate_integer(self.start_lap, "start_lap", minimum=1)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.end_lap, "end_lap", minimum=1)
        if self.end_lap is not None and self.end_lap < self.start_lap:
            raise ValueError("end_lap must not precede start_lap")
        _validate_optional_integer(self.start_time_ms, "start_time_ms", minimum=0)
        _validate_optional_integer(self.end_time_ms, "end_time_ms", minimum=0)
        if self.start_time_ms is not None and self.end_time_ms is not None:
            if self.end_time_ms < self.start_time_ms:
                raise ValueError("end_time_ms must not precede start_time_ms")
        if not isinstance(self.compound, TyreCompound):
            raise TypeError("compound must be a TyreCompound")
        _validate_optional_text(self.raw_compound, "raw_compound")
        _validate_optional_number(
            self.tyre_life_start_laps,
            "tyre_life_start_laps",
            minimum=0,
        )
        _validate_optional_number(self.tyre_life_end_laps, "tyre_life_end_laps", minimum=0)


@dataclass(frozen=True, slots=True)
class PitStopRecord:
    """A pit observation with distinct lane-transit and stationary durations.

    ``pit_lane_duration_ms`` spans pit entry to pit exit. ``stationary_duration_ms`` remains
    unknown unless a source observes the stopped interval independently.
    """

    session_id: SessionId
    driver_id: DriverId
    stop_number: int
    provenance: SourceProvenance
    lap_number: int | None = None
    pit_in_time_ms: int | None = None
    pit_out_time_ms: int | None = None
    pit_lane_duration_ms: int | None = None
    stationary_duration_ms: int | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.stop_number, "stop_number", minimum=1)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.lap_number, "lap_number", minimum=1)
        for field_name in (
            "pit_in_time_ms",
            "pit_out_time_ms",
            "pit_lane_duration_ms",
            "stationary_duration_ms",
        ):
            _validate_optional_integer(getattr(self, field_name), field_name, minimum=0)
        if self.pit_in_time_ms is not None and self.pit_out_time_ms is not None:
            if self.pit_out_time_ms < self.pit_in_time_ms:
                raise ValueError("pit_out_time_ms must not precede pit_in_time_ms")


@dataclass(frozen=True, slots=True)
class WeatherRecord:
    """Timestamped weather in explicit SI or named source units."""

    session_id: SessionId
    session_time_ms: int
    provenance: SourceProvenance
    air_temperature_c: float | None = None
    track_temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    rainfall: bool | None = None
    wind_speed_mps: float | None = None
    wind_direction_deg: float | None = None

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_integer(self.session_time_ms, "session_time_ms", minimum=0)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_number(self.air_temperature_c, "air_temperature_c")
        _validate_optional_number(self.track_temperature_c, "track_temperature_c")
        _validate_optional_number(
            self.humidity_percent,
            "humidity_percent",
            minimum=0,
            maximum=100,
        )
        _validate_optional_number(self.pressure_hpa, "pressure_hpa", minimum=0)
        if self.rainfall is not None and type(self.rainfall) is not bool:
            raise TypeError("rainfall must be a bool or None")
        _validate_optional_number(self.wind_speed_mps, "wind_speed_mps", minimum=0)
        _validate_optional_number(
            self.wind_direction_deg,
            "wind_direction_deg",
            minimum=0,
            maximum=360,
        )


@dataclass(frozen=True, slots=True)
class RaceControlRecord:
    """A timestamped race-control message with normalized and raw status."""

    session_id: SessionId
    session_time_ms: int
    message: str
    provenance: SourceProvenance
    track_status: TrackStatus = TrackStatus.UNKNOWN
    raw_status: str | None = None
    category: str | None = None
    scope: str | None = None
    source_kind: str | None = None
    lap_number: int | None = None
    driver_id: DriverId | None = None

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        _validate_integer(self.session_time_ms, "session_time_ms", minimum=0)
        _require_text(self.message, "message")
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        if not isinstance(self.track_status, TrackStatus):
            raise TypeError("track_status must be a TrackStatus")
        _validate_optional_text(self.raw_status, "raw_status")
        _validate_optional_text(self.category, "category")
        _validate_optional_text(self.scope, "scope")
        _validate_optional_text(self.source_kind, "source_kind")
        _validate_optional_integer(self.lap_number, "lap_number", minimum=1)
        if self.driver_id is not None:
            _validate_driver_scope(self.session_id, self.driver_id)


@dataclass(frozen=True, slots=True)
class RacePositionRecord:
    """A driver's ordinal race position at a session timestamp."""

    session_id: SessionId
    driver_id: DriverId
    session_time_ms: int
    position: int
    provenance: SourceProvenance
    lap_number: int | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.session_time_ms, "session_time_ms", minimum=0)
        _validate_integer(self.position, "position", minimum=1)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.lap_number, "lap_number", minimum=1)


@dataclass(frozen=True, slots=True)
class TrackPositionRecord:
    """A driver's geometric track coordinate, separate from ordinal race position."""

    session_id: SessionId
    driver_id: DriverId
    session_time_ms: int
    x_m: float
    y_m: float
    provenance: SourceProvenance
    z_m: float | None = None
    raw_status: str | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.session_time_ms, "session_time_ms", minimum=0)
        _validate_number(self.x_m, "x_m")
        _validate_number(self.y_m, "y_m")
        _validate_optional_number(self.z_m, "z_m")
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_text(self.raw_status, "raw_status")


@dataclass(frozen=True, slots=True)
class TelemetryIndexRecord:
    """A lazy telemetry range index; sample payloads are intentionally not canonical rows."""

    session_id: SessionId
    driver_id: DriverId
    start_time_ms: int
    end_time_ms: int
    data_key: str
    channel_names: tuple[str, ...]
    sample_count: int
    provenance: SourceProvenance
    lap_number: int | None = None

    def __post_init__(self) -> None:
        _validate_driver_scope(self.session_id, self.driver_id)
        _validate_integer(self.start_time_ms, "start_time_ms", minimum=0)
        _validate_integer(self.end_time_ms, "end_time_ms", minimum=0)
        if self.end_time_ms < self.start_time_ms:
            raise ValueError("end_time_ms must not precede start_time_ms")
        validate_safe_identifier(self.data_key, field_name="data_key")
        if not isinstance(self.channel_names, tuple):
            raise TypeError("channel_names must be an immutable tuple")
        for channel in self.channel_names:
            _require_text(channel, "channel_names item")
        if len(set(self.channel_names)) != len(self.channel_names):
            raise ValueError("channel_names must not contain duplicates")
        _validate_integer(self.sample_count, "sample_count", minimum=0)
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("provenance must be SourceProvenance")
        _validate_optional_integer(self.lap_number, "lap_number", minimum=1)


__all__ = [
    "DriverClassificationRecord",
    "DriverRecord",
    "LapRecord",
    "PitStopRecord",
    "RaceControlRecord",
    "RacePositionRecord",
    "SessionMetadata",
    "SourceProvenance",
    "StintRecord",
    "TelemetryIndexRecord",
    "TrackPositionRecord",
    "WeatherRecord",
]
