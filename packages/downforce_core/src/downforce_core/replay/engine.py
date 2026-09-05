"""Checkpointed deterministic RaceState queries over a canonical timeline."""

from __future__ import annotations

from bisect import bisect_right
from typing import Protocol

from downforce_core.domain.events import RaceEvent
from downforce_core.domain.identifiers import SessionId
from downforce_core.domain.state import RaceState
from downforce_core.exceptions import ReplayCursorError, SessionDataIncompleteError
from downforce_core.normalization.models import NormalizedSession
from downforce_core.replay.lap_cursor import build_lap_cursors
from downforce_core.replay.reducer import MutableReplayState, apply_event
from downforce_core.replay.timeline import CanonicalTimeline, build_timeline


class ReplayRepository(Protocol):
    def load_session(
        self,
        session_id: str | SessionId,
        *,
        include_track_positions: bool = True,
    ) -> NormalizedSession: ...

    def load_events(self, session_id: str | SessionId) -> tuple[RaceEvent, ...]: ...


class ReplayEngine:
    """Replay only events at or before a cursor, with deterministic immutable snapshots."""

    def __init__(
        self,
        session: NormalizedSession,
        timeline: CanonicalTimeline | None = None,
        *,
        checkpoint_interval: int = 256,
    ) -> None:
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be positive")
        self._session = session
        self._timeline = timeline or build_timeline(session)
        if self._timeline.session_id != session.metadata.session_id:
            raise ValueError("timeline and session IDs do not match")
        self._times = tuple(event.session_time_ms for event in self._timeline.events)
        self._checkpoint_indices: list[int] = [0]
        initial = MutableReplayState.initial(session)
        self._checkpoints: dict[int, RaceState] = {0: initial.freeze(cursor=0, reference_lap=None)}
        mutable = initial
        for index, event in enumerate(self._timeline.events, start=1):
            apply_event(mutable, event)
            if index % checkpoint_interval == 0 or index == len(self._timeline.events):
                self._checkpoint_indices.append(index)
                self._checkpoints[index] = mutable.freeze(
                    cursor=event.session_time_ms,
                    reference_lap=self._timeline.reference_lap_at(event.session_time_ms),
                )

    @classmethod
    def from_repository(
        cls,
        repository: ReplayRepository,
        session_id: str | SessionId,
        *,
        checkpoint_interval: int = 256,
    ) -> ReplayEngine:
        session = repository.load_session(session_id, include_track_positions=False)
        events = repository.load_events(session_id)
        if not events:
            raise SessionDataIncompleteError("canonical session has no persisted event timeline")
        cursors = build_lap_cursors(session)
        timeline = CanonicalTimeline(
            session_id=session.metadata.session_id,
            events=events,
            lap_cursors={cursor.lap_number: cursor for cursor in cursors},
        )
        return cls(session, timeline, checkpoint_interval=checkpoint_interval)

    @property
    def timeline(self) -> CanonicalTimeline:
        return self._timeline

    def state_at(self, session_time_ms: int) -> RaceState:
        if isinstance(session_time_ms, bool) or not isinstance(session_time_ms, int):
            raise ReplayCursorError("time_ms must be an integer")
        if session_time_ms < 0:
            raise ReplayCursorError("time_ms must be nonnegative")
        if session_time_ms > self._timeline.max_time_ms:
            raise ReplayCursorError(
                f"time_ms exceeds the canonical timeline end ({self._timeline.max_time_ms})"
            )
        event_limit = bisect_right(self._times, session_time_ms)
        checkpoint_position = bisect_right(self._checkpoint_indices, event_limit) - 1
        checkpoint_index = self._checkpoint_indices[checkpoint_position]
        mutable = MutableReplayState.from_snapshot(self._checkpoints[checkpoint_index])
        for event in self._timeline.events[checkpoint_index:event_limit]:
            apply_event(mutable, event)
        return mutable.freeze(
            cursor=session_time_ms,
            reference_lap=self._timeline.reference_lap_at(session_time_ms),
        )

    def state_at_lap(self, lap_number: int, *, phase: str = "end") -> RaceState:
        if isinstance(lap_number, bool) or not isinstance(lap_number, int) or lap_number < 1:
            raise ReplayCursorError("lap must be a positive integer")
        if phase not in {"start", "end"}:
            raise ReplayCursorError("phase must be 'start' or 'end'")
        cursor = self._timeline.lap_cursors.get(lap_number)
        if cursor is None:
            raise ReplayCursorError(f"lap {lap_number} has no unambiguous P1 reference cursor")
        time_ms = cursor.start_time_ms if phase == "start" else cursor.end_time_ms
        return self.state_at(time_ms)


def state_to_dict(state: RaceState) -> dict[str, object]:
    """Stable JSON-compatible representation used by determinism tests and the API."""

    weather = None
    if state.weather is not None:
        weather = {
            "observed_at_ms": state.weather.observed_at_ms,
            "air_temperature_c": state.weather.air_temperature_c,
            "track_temperature_c": state.weather.track_temperature_c,
            "humidity_percent": state.weather.humidity_percent,
            "pressure_hpa": state.weather.pressure_hpa,
            "rainfall": state.weather.rainfall,
            "wind_speed_mps": state.weather.wind_speed_mps,
            "wind_direction_deg": state.weather.wind_direction_deg,
        }
    return {
        "replay_version": state.replay_version,
        "session_id": str(state.session_id),
        "session_time_ms": state.session_time_ms,
        "reference_lap": state.reference_lap,
        "track_status": state.track_status.value,
        "weather": weather,
        "drivers": [
            {
                "driver_id": str(driver.driver_id),
                "racing_number": driver.racing_number,
                "abbreviation": driver.abbreviation,
                "full_name": driver.full_name,
                "team_name": driver.team_name,
                "status": driver.status.value,
                "position": driver.position,
                "laps_completed": driver.laps_completed,
                "current_stint": driver.current_stint,
                "compound": driver.compound.value,
                "tyre_age_laps": driver.tyre_age_laps,
                "last_lap_time_ms": driver.last_lap_time_ms,
                "in_pit": driver.in_pit,
                "pit_stop_count": driver.pit_stop_count,
                "last_pit_lap": driver.last_pit_lap,
            }
            for _, driver in sorted(state.drivers.items(), key=lambda item: str(item[0]))
        ],
        "recent_race_control": [
            {
                "observed_at_ms": item.observed_at_ms,
                "message": item.message,
                "category": item.category,
                "scope": item.scope,
                "driver_id": None if item.driver_id is None else str(item.driver_id),
            }
            for item in state.recent_race_control
        ],
        "completeness": {name: value.value for name, value in sorted(state.completeness.items())},
        "data_quality": state.data_quality.value,
    }


__all__ = ["ReplayEngine", "state_to_dict"]
