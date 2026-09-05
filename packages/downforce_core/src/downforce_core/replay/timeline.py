"""Build a compact, deduplicated and byte-stable significant-event timeline."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from downforce_core.domain.enums import TrackStatus
from downforce_core.domain.events import (
    EVENT_PRIORITY,
    EventValue,
    RaceEvent,
    RaceEventType,
)
from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.normalization.models import NormalizedSession
from downforce_core.replay.lap_cursor import LapCursor, build_lap_cursors


@dataclass(frozen=True, slots=True)
class _Candidate:
    session_time_ms: int
    event_type: RaceEventType
    source: str
    source_key: str | None
    payload: Mapping[str, EventValue]
    driver_id: DriverId | None = None

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.session_time_ms,
            EVENT_PRIORITY[self.event_type],
            self.event_type.value,
            "" if self.driver_id is None else str(self.driver_id),
            self.source,
            self.source_key or "",
            json.dumps(
                _thaw_payload(self.payload),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


def _thaw_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_payload(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_payload(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class CanonicalTimeline:
    session_id: SessionId
    events: tuple[RaceEvent, ...]
    lap_cursors: Mapping[int, LapCursor]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(event.session_id != self.session_id for event in self.events):
            raise ValueError("timeline contains an event from another session")
        if tuple(event.sort_key for event in self.events) != tuple(
            sorted(event.sort_key for event in self.events)
        ):
            raise ValueError("timeline events must be deterministically sorted")
        if tuple(event.sequence for event in self.events) != tuple(range(len(self.events))):
            raise ValueError("timeline sequences must be contiguous and zero-based")
        cursors = dict(self.lap_cursors)
        if any(number != cursor.lap_number for number, cursor in cursors.items()):
            raise ValueError("lap cursor key does not match its lap number")
        object.__setattr__(self, "lap_cursors", MappingProxyType(cursors))

    @property
    def max_time_ms(self) -> int:
        return self.events[-1].session_time_ms if self.events else 0

    def reference_lap_at(self, session_time_ms: int) -> int | None:
        completed = [
            number
            for number, cursor in self.lap_cursors.items()
            if cursor.end_time_ms <= session_time_ms
        ]
        return max(completed, default=None)


def _source_key(value: str | None, fallback: str) -> str:
    return value or fallback


def _candidate_events(session: NormalizedSession) -> list[_Candidate]:
    candidates: list[_Candidate] = [
        _Candidate(
            session_time_ms=0,
            event_type=RaceEventType.SESSION_MARKER,
            source="downforce.timeline",
            source_key="session-origin",
            payload={"marker": "session-origin"},
        )
    ]
    for weather in session.weather:
        candidates.append(
            _Candidate(
                session_time_ms=weather.session_time_ms,
                event_type=RaceEventType.WEATHER_OBSERVED,
                source=weather.provenance.source,
                source_key=_source_key(
                    weather.provenance.source_record_id,
                    f"weather-{weather.session_time_ms}",
                ),
                payload={
                    "air_temperature_c": weather.air_temperature_c,
                    "track_temperature_c": weather.track_temperature_c,
                    "humidity_percent": weather.humidity_percent,
                    "pressure_hpa": weather.pressure_hpa,
                    "rainfall": weather.rainfall,
                    "wind_speed_mps": weather.wind_speed_mps,
                    "wind_direction_deg": weather.wind_direction_deg,
                },
            )
        )
    last_track_status = TrackStatus.UNKNOWN
    for control in sorted(
        session.race_control,
        key=lambda value: (value.session_time_ms, value.provenance.source_record_id or ""),
    ):
        key = _source_key(
            control.provenance.source_record_id,
            f"control-{control.session_time_ms}-{control.message}",
        )
        candidates.append(
            _Candidate(
                session_time_ms=control.session_time_ms,
                event_type=RaceEventType.RACE_CONTROL_EVENT,
                driver_id=control.driver_id,
                source=control.provenance.source,
                source_key=key,
                payload={
                    "message": control.message,
                    "category": control.category,
                    "scope": control.scope,
                    "lap_number": control.lap_number,
                    "source_kind": control.source_kind,
                },
            )
        )
        if control.track_status not in {TrackStatus.UNKNOWN, last_track_status}:
            candidates.append(
                _Candidate(
                    session_time_ms=control.session_time_ms,
                    event_type=RaceEventType.TRACK_STATUS_CHANGED,
                    source=control.provenance.source,
                    source_key=f"{key}-track-status",
                    payload={"track_status": control.track_status.value},
                )
            )
            last_track_status = control.track_status
    for stint in session.stints:
        if stint.start_time_ms is None:
            continue
        candidates.append(
            _Candidate(
                session_time_ms=stint.start_time_ms,
                event_type=RaceEventType.DRIVER_STINT_CHANGED,
                driver_id=stint.driver_id,
                source=stint.provenance.source,
                source_key=_source_key(
                    stint.provenance.source_record_id,
                    f"stint-{stint.driver_id}-{stint.stint_number}",
                ),
                payload={
                    "stint_number": stint.stint_number,
                    "compound": stint.compound.value,
                    "tyre_age_laps": stint.tyre_life_start_laps,
                    "start_lap": stint.start_lap,
                },
            )
        )
    for stop in session.pit_stops:
        key = _source_key(
            stop.provenance.source_record_id,
            f"pit-{stop.driver_id}-{stop.stop_number}",
        )
        payload: dict[str, EventValue] = {
            "stop_number": stop.stop_number,
            "lap_number": stop.lap_number,
        }
        if stop.pit_in_time_ms is not None:
            candidates.append(
                _Candidate(
                    session_time_ms=stop.pit_in_time_ms,
                    event_type=RaceEventType.DRIVER_PIT_ENTERED,
                    driver_id=stop.driver_id,
                    source=stop.provenance.source,
                    source_key=f"{key}-in",
                    payload=payload,
                )
            )
        if stop.pit_out_time_ms is not None:
            candidates.append(
                _Candidate(
                    session_time_ms=stop.pit_out_time_ms,
                    event_type=RaceEventType.DRIVER_PIT_EXITED,
                    driver_id=stop.driver_id,
                    source=stop.provenance.source,
                    source_key=f"{key}-out",
                    payload=payload,
                )
            )
    for position in session.race_positions:
        candidates.append(
            _Candidate(
                session_time_ms=position.session_time_ms,
                event_type=RaceEventType.DRIVER_POSITION_CHANGED,
                driver_id=position.driver_id,
                source=position.provenance.source,
                source_key=_source_key(
                    position.provenance.source_record_id,
                    f"position-{position.driver_id}-{position.session_time_ms}",
                ),
                payload={
                    "position": position.position,
                    "lap_number": position.lap_number,
                },
            )
        )
    for lap in session.laps:
        if lap.lap_end_time_ms is None or lap.is_deleted is True:
            continue
        candidates.append(
            _Candidate(
                session_time_ms=lap.lap_end_time_ms,
                event_type=RaceEventType.DRIVER_LAP_COMPLETED,
                driver_id=lap.driver_id,
                source=lap.provenance.source,
                source_key=_source_key(
                    lap.provenance.source_record_id,
                    f"lap-{lap.driver_id}-{lap.lap_number}",
                ),
                payload={
                    "lap_number": lap.lap_number,
                    "lap_time_ms": lap.lap_time_ms,
                    "stint_number": lap.stint_number,
                    "compound": lap.compound.value,
                    "tyre_age_laps": lap.tyre_life_laps,
                },
            )
        )
    return candidates


def build_timeline(session: NormalizedSession) -> CanonicalTimeline:
    """Build a significant timeline without reading final classification facts."""

    candidates = _candidate_events(session)
    unique = {candidate.identity: candidate for candidate in candidates}
    ordered = [unique[key] for key in sorted(unique)]
    events = tuple(
        RaceEvent(
            session_id=session.metadata.session_id,
            session_time_ms=candidate.session_time_ms,
            sequence=sequence,
            event_type=candidate.event_type,
            driver_id=candidate.driver_id,
            source=candidate.source,
            source_key=candidate.source_key,
            payload=candidate.payload,
        )
        for sequence, candidate in enumerate(ordered)
    )
    cursors = build_lap_cursors(session)
    return CanonicalTimeline(
        session_id=session.metadata.session_id,
        events=events,
        lap_cursors={cursor.lap_number: cursor for cursor in cursors},
    )


__all__ = ["CanonicalTimeline", "build_timeline"]
