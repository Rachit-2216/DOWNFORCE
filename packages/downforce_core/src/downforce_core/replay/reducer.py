"""Single transition boundary for canonical RaceEvent application."""

from __future__ import annotations

from dataclasses import dataclass

from downforce_core.domain.enums import DataQuality, TrackStatus, TyreCompound
from downforce_core.domain.events import EventValue, RaceEvent, RaceEventType
from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.domain.state import (
    TERMINAL_DRIVER_STATUSES,
    DriverState,
    RaceControlState,
    RaceState,
    ReplayDriverStatus,
    WeatherState,
)
from downforce_core.normalization.models import NormalizedSession
from downforce_core.providers.base import DatasetAvailability


@dataclass(slots=True)
class MutableDriverState:
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

    @classmethod
    def from_snapshot(cls, value: DriverState) -> MutableDriverState:
        return cls(**{name: getattr(value, name) for name in cls.__dataclass_fields__})

    def freeze(self) -> DriverState:
        return DriverState(**{name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(slots=True)
class MutableReplayState:
    session_id: SessionId
    session_time_ms: int
    track_status: TrackStatus
    weather: WeatherState | None
    drivers: dict[DriverId, MutableDriverState]
    recent_race_control: list[RaceControlState]
    completeness: dict[str, DatasetAvailability]
    data_quality: DataQuality

    @classmethod
    def initial(cls, session: NormalizedSession) -> MutableReplayState:
        return cls(
            session_id=session.metadata.session_id,
            session_time_ms=0,
            track_status=TrackStatus.UNKNOWN,
            weather=None,
            drivers={
                driver.driver_id: MutableDriverState(
                    driver_id=driver.driver_id,
                    racing_number=driver.racing_number,
                    abbreviation=driver.abbreviation,
                    full_name=driver.full_name,
                    team_name=driver.team_name,
                )
                for driver in session.drivers
            },
            recent_race_control=[],
            completeness={name.value: state for name, state in session.completeness.items()},
            data_quality=session.metadata.data_quality,
        )

    @classmethod
    def from_snapshot(cls, state: RaceState) -> MutableReplayState:
        return cls(
            session_id=state.session_id,
            session_time_ms=state.session_time_ms,
            track_status=state.track_status,
            weather=state.weather,
            drivers={
                driver_id: MutableDriverState.from_snapshot(driver)
                for driver_id, driver in state.drivers.items()
            },
            recent_race_control=list(state.recent_race_control),
            completeness=dict(state.completeness),
            data_quality=state.data_quality,
        )

    def freeze(self, *, cursor: int, reference_lap: int | None) -> RaceState:
        return RaceState(
            session_id=self.session_id,
            session_time_ms=cursor,
            reference_lap=reference_lap,
            track_status=self.track_status,
            weather=self.weather,
            drivers={driver_id: driver.freeze() for driver_id, driver in self.drivers.items()},
            recent_race_control=tuple(self.recent_race_control),
            completeness=self.completeness,
            data_quality=self.data_quality,
        )


def _payload(event: RaceEvent, name: str) -> EventValue:
    if name not in event.payload:
        raise ValueError(f"{event.event_type.value} payload omitted {name}")
    return event.payload[name]


def _optional_int(event: RaceEvent, name: str) -> int | None:
    value = event.payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or null")
    return value


def _optional_float(event: RaceEvent, name: str) -> float | None:
    value = event.payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric or null")
    return float(value)


def _optional_text(event: RaceEvent, name: str) -> str | None:
    value = event.payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text or null")
    return value


def _driver(state: MutableReplayState, event: RaceEvent) -> MutableDriverState:
    if event.driver_id is None:
        raise ValueError(f"{event.event_type.value} requires a driver_id")
    try:
        return state.drivers[event.driver_id]
    except KeyError as exc:
        raise ValueError(f"event references unknown driver {event.driver_id}") from exc


def _activate(driver: MutableDriverState) -> None:
    if driver.status not in TERMINAL_DRIVER_STATUSES and not driver.in_pit:
        driver.status = ReplayDriverStatus.ACTIVE


def _apply_stint(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    stint = _payload(event, "stint_number")
    compound = _payload(event, "compound")
    if isinstance(stint, bool) or not isinstance(stint, int) or stint < 1:
        raise ValueError("stint_number must be positive")
    if not isinstance(compound, str):
        raise TypeError("compound must be text")
    driver.current_stint = stint
    driver.compound = TyreCompound(compound)
    driver.tyre_age_laps = _optional_float(event, "tyre_age_laps")
    _activate(driver)


def _apply_pit_entry(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    if driver.status in TERMINAL_DRIVER_STATUSES:
        return
    if not driver.in_pit:
        driver.pit_stop_count += 1
    driver.in_pit = True
    driver.last_pit_lap = _optional_int(event, "lap_number")
    driver.status = ReplayDriverStatus.IN_PIT


def _apply_pit_exit(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    driver.in_pit = False
    _activate(driver)


def _apply_position(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    position = _payload(event, "position")
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise ValueError("position must be positive")
    driver.position = position
    _activate(driver)


def _apply_lap(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    lap = _payload(event, "lap_number")
    if isinstance(lap, bool) or not isinstance(lap, int) or lap < 1:
        raise ValueError("lap_number must be positive")
    driver.laps_completed = max(driver.laps_completed, lap)
    driver.last_lap_time_ms = _optional_int(event, "lap_time_ms")
    stint = _optional_int(event, "stint_number")
    if stint is not None:
        driver.current_stint = stint
    compound = _optional_text(event, "compound")
    if compound is not None:
        driver.compound = TyreCompound(compound)
    tyre_age = _optional_float(event, "tyre_age_laps")
    if tyre_age is not None:
        driver.tyre_age_laps = tyre_age
    _activate(driver)


def _apply_weather(state: MutableReplayState, event: RaceEvent) -> None:
    rainfall = event.payload.get("rainfall")
    if rainfall is not None and type(rainfall) is not bool:
        raise TypeError("rainfall must be boolean or null")
    state.weather = WeatherState(
        observed_at_ms=event.session_time_ms,
        air_temperature_c=_optional_float(event, "air_temperature_c"),
        track_temperature_c=_optional_float(event, "track_temperature_c"),
        humidity_percent=_optional_float(event, "humidity_percent"),
        pressure_hpa=_optional_float(event, "pressure_hpa"),
        rainfall=rainfall,
        wind_speed_mps=_optional_float(event, "wind_speed_mps"),
        wind_direction_deg=_optional_float(event, "wind_direction_deg"),
    )


def _apply_control(state: MutableReplayState, event: RaceEvent) -> None:
    message = _payload(event, "message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("race-control message must be nonempty")
    state.recent_race_control.append(
        RaceControlState(
            observed_at_ms=event.session_time_ms,
            message=message,
            category=_optional_text(event, "category"),
            scope=_optional_text(event, "scope"),
            driver_id=event.driver_id,
        )
    )
    del state.recent_race_control[:-20]


def _apply_track_status(state: MutableReplayState, event: RaceEvent) -> None:
    value = _payload(event, "track_status")
    if not isinstance(value, str):
        raise TypeError("track_status must be text")
    state.track_status = TrackStatus(value)


def _apply_driver_status(state: MutableReplayState, event: RaceEvent) -> None:
    driver = _driver(state, event)
    value = _payload(event, "status")
    if not isinstance(value, str):
        raise TypeError("status must be text")
    next_status = ReplayDriverStatus(value)
    if driver.status in TERMINAL_DRIVER_STATUSES:
        return
    driver.status = next_status
    driver.in_pit = next_status is ReplayDriverStatus.IN_PIT


def apply_event(state: MutableReplayState, event: RaceEvent) -> None:
    """Apply exactly one event while preserving terminal and monotonic invariants."""

    if event.session_id != state.session_id:
        raise ValueError("event belongs to another session")
    if event.session_time_ms < state.session_time_ms:
        raise ValueError("replay event time moved backward")
    state.session_time_ms = event.session_time_ms
    transitions = {
        RaceEventType.DRIVER_STINT_CHANGED: _apply_stint,
        RaceEventType.DRIVER_PIT_ENTERED: _apply_pit_entry,
        RaceEventType.DRIVER_PIT_EXITED: _apply_pit_exit,
        RaceEventType.DRIVER_POSITION_CHANGED: _apply_position,
        RaceEventType.DRIVER_LAP_COMPLETED: _apply_lap,
        RaceEventType.WEATHER_OBSERVED: _apply_weather,
        RaceEventType.RACE_CONTROL_EVENT: _apply_control,
        RaceEventType.TRACK_STATUS_CHANGED: _apply_track_status,
        RaceEventType.DRIVER_STATUS_CHANGED: _apply_driver_status,
    }
    transition = transitions.get(event.event_type)
    if transition is not None:
        transition(state, event)


__all__ = ["MutableReplayState", "apply_event"]
