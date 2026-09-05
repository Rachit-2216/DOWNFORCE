"""Immutable provider-neutral significant events for deterministic historical replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType

from downforce_core.domain.identifiers import DriverId, SessionId, is_driver_id_for_session
from downforce_core.versions import NORMALIZATION_VERSION

type EventScalar = None | bool | int | float | str
type EventValue = EventScalar | tuple[EventValue, ...] | Mapping[str, EventValue]


class RaceEventType(StrEnum):
    SESSION_MARKER = "session-marker"
    TRACK_STATUS_CHANGED = "track-status-changed"
    WEATHER_OBSERVED = "weather-observed"
    RACE_CONTROL_EVENT = "race-control-event"
    DRIVER_STINT_CHANGED = "driver-stint-changed"
    DRIVER_PIT_ENTERED = "driver-pit-entered"
    DRIVER_POSITION_CHANGED = "driver-position-changed"
    DRIVER_LAP_COMPLETED = "driver-lap-completed"
    DRIVER_PIT_EXITED = "driver-pit-exited"
    DRIVER_STATUS_CHANGED = "driver-status-changed"


EVENT_PRIORITY: Mapping[RaceEventType, int] = MappingProxyType(
    {
        RaceEventType.SESSION_MARKER: 0,
        RaceEventType.TRACK_STATUS_CHANGED: 10,
        RaceEventType.WEATHER_OBSERVED: 20,
        RaceEventType.RACE_CONTROL_EVENT: 30,
        RaceEventType.DRIVER_STINT_CHANGED: 40,
        RaceEventType.DRIVER_PIT_ENTERED: 50,
        RaceEventType.DRIVER_POSITION_CHANGED: 60,
        RaceEventType.DRIVER_LAP_COMPLETED: 70,
        RaceEventType.DRIVER_PIT_EXITED: 80,
        RaceEventType.DRIVER_STATUS_CHANGED: 90,
    }
)


def _freeze_event_value(value: object, *, path: str) -> EventValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, EventValue] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"{path} keys must be nonempty strings")
            frozen[key] = _freeze_event_value(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_event_value(child, path=f"{path}[]") for child in value)
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _thaw(value: EventValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def canonical_event_payload(payload: Mapping[str, EventValue]) -> str:
    return json.dumps(
        _thaw(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class RaceEvent:
    session_id: SessionId
    session_time_ms: int
    sequence: int
    event_type: RaceEventType
    source: str
    source_key: str | None
    payload: Mapping[str, EventValue]
    driver_id: DriverId | None = None
    normalization_version: str = NORMALIZATION_VERSION
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise TypeError("session_id must be a SessionId")
        for name in ("session_time_ms", "sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.event_type, RaceEventType):
            raise TypeError("event_type must be a RaceEventType")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be nonempty")
        if self.source_key is not None and (
            not isinstance(self.source_key, str) or not self.source_key.strip()
        ):
            raise ValueError("source_key must be nonempty or None")
        if self.driver_id is not None:
            if not isinstance(self.driver_id, DriverId):
                raise TypeError("driver_id must be a DriverId or None")
            if not is_driver_id_for_session(self.driver_id, self.session_id):
                raise ValueError("driver_id must be scoped to session_id")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        frozen = _freeze_event_value(self.payload, path="payload")
        if not isinstance(frozen, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", frozen)
        identity = "\x1f".join(
            (
                str(self.session_id),
                str(self.session_time_ms),
                str(EVENT_PRIORITY[self.event_type]),
                str(self.sequence),
                self.event_type.value,
                "" if self.driver_id is None else str(self.driver_id),
                self.source,
                self.source_key or "",
                canonical_event_payload(frozen),
                self.normalization_version,
            )
        )
        object.__setattr__(self, "event_id", f"event-{sha256(identity.encode()).hexdigest()}")

    @property
    def priority(self) -> int:
        return EVENT_PRIORITY[self.event_type]

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.session_time_ms, self.priority, self.sequence)


__all__ = [
    "EVENT_PRIORITY",
    "EventValue",
    "RaceEvent",
    "RaceEventType",
    "canonical_event_payload",
]
