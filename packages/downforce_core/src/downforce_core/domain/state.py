"""Immutable factual RaceState snapshots returned by the replay engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from downforce_core.domain.enums import DataQuality, TrackStatus, TyreCompound
from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.providers.base import DatasetAvailability
from downforce_core.versions import REPLAY_VERSION


class ReplayDriverStatus(StrEnum):
    NOT_STARTED = "not-started"
    ACTIVE = "active"
    IN_PIT = "in-pit"
    RETIRED = "retired"
    FINISHED = "finished"
    DNS = "dns"
    DSQ = "dsq"
    UNKNOWN = "unknown"


TERMINAL_DRIVER_STATUSES = frozenset(
    {
        ReplayDriverStatus.RETIRED,
        ReplayDriverStatus.FINISHED,
        ReplayDriverStatus.DNS,
        ReplayDriverStatus.DSQ,
    }
)


@dataclass(frozen=True, slots=True)
class WeatherState:
    observed_at_ms: int
    air_temperature_c: float | None = None
    track_temperature_c: float | None = None
    humidity_percent: float | None = None
    pressure_hpa: float | None = None
    rainfall: bool | None = None
    wind_speed_mps: float | None = None
    wind_direction_deg: float | None = None

    def __post_init__(self) -> None:
        if self.observed_at_ms < 0:
            raise ValueError("weather observation time must be nonnegative")


@dataclass(frozen=True, slots=True)
class RaceControlState:
    observed_at_ms: int
    message: str
    category: str | None = None
    scope: str | None = None
    driver_id: DriverId | None = None

    def __post_init__(self) -> None:
        if self.observed_at_ms < 0:
            raise ValueError("race-control observation time must be nonnegative")
        if not self.message.strip():
            raise ValueError("race-control message must be nonempty")


@dataclass(frozen=True, slots=True)
class DriverState:
    driver_id: DriverId
    racing_number: int | None
    abbreviation: str | None
    full_name: str | None
    team_name: str | None
    status: ReplayDriverStatus = ReplayDriverStatus.NOT_STARTED
    position: int | None = None
    laps_completed: int = 0
    current_stint: int | None = None
    compound: TyreCompound = TyreCompound.UNKNOWN
    tyre_age_laps: float | None = None
    last_lap_time_ms: int | None = None
    in_pit: bool = False
    pit_stop_count: int = 0
    last_pit_lap: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.driver_id, DriverId):
            raise TypeError("driver_id must be a DriverId")
        if self.position is not None and self.position < 1:
            raise ValueError("position must be positive or None")
        for field_name in ("laps_completed", "pit_stop_count"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be nonnegative")
        if self.current_stint is not None and self.current_stint < 1:
            raise ValueError("current_stint must be positive or None")
        if self.tyre_age_laps is not None and self.tyre_age_laps < 0:
            raise ValueError("tyre_age_laps must be nonnegative or None")
        if self.last_lap_time_ms is not None and self.last_lap_time_ms < 0:
            raise ValueError("last_lap_time_ms must be nonnegative or None")
        if self.last_pit_lap is not None and self.last_pit_lap < 1:
            raise ValueError("last_pit_lap must be positive or None")


@dataclass(frozen=True, slots=True)
class RaceState:
    session_id: SessionId
    session_time_ms: int
    reference_lap: int | None
    track_status: TrackStatus
    weather: WeatherState | None
    drivers: Mapping[DriverId, DriverState]
    recent_race_control: tuple[RaceControlState, ...]
    completeness: Mapping[str, DatasetAvailability]
    data_quality: DataQuality
    replay_version: str = REPLAY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise TypeError("session_id must be a SessionId")
        if self.session_time_ms < 0:
            raise ValueError("session_time_ms must be nonnegative")
        if self.reference_lap is not None and self.reference_lap < 1:
            raise ValueError("reference_lap must be positive or None")
        if not isinstance(self.track_status, TrackStatus):
            raise TypeError("track_status must be a TrackStatus")
        if not isinstance(self.recent_race_control, tuple):
            raise TypeError("recent_race_control must be a tuple")
        if any(key != driver.driver_id for key, driver in self.drivers.items()):
            raise ValueError("driver state mapping keys must match driver IDs")
        if any(not isinstance(value, DatasetAvailability) for value in self.completeness.values()):
            raise TypeError("completeness values must be DatasetAvailability values")
        object.__setattr__(self, "drivers", MappingProxyType(dict(self.drivers)))
        object.__setattr__(self, "completeness", MappingProxyType(dict(self.completeness)))


__all__ = [
    "DriverState",
    "RaceControlState",
    "RaceState",
    "ReplayDriverStatus",
    "TERMINAL_DRIVER_STATUSES",
    "WeatherState",
]
