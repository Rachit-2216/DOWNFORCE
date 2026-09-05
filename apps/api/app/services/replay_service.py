"""Repository-backed replay and significant-timeline application service."""

from __future__ import annotations

import json
from threading import Lock

from downforce_core.domain.events import RaceEventType, canonical_event_payload
from downforce_core.exceptions import ReplayCursorError
from downforce_core.replay import ReplayEngine, state_to_dict
from downforce_core.storage import DownforceRepository


class ReplayService:
    def __init__(self, repository: DownforceRepository) -> None:
        self.repository = repository
        self._lock = Lock()
        self._engines: dict[tuple[str, str], ReplayEngine] = {}

    def _engine(self, session_id: str) -> ReplayEngine:
        while True:
            canonical_id, dataset_id = self.repository.active_dataset_identity(session_id)
            key = (canonical_id, dataset_id)
            with self._lock:
                cached = self._engines.get(key)
            if cached is not None:
                return cached

            engine = ReplayEngine.from_repository(self.repository, canonical_id)
            if self.repository.active_dataset_identity(canonical_id) != key:
                continue
            with self._lock:
                cached = self._engines.get(key)
                if cached is not None:
                    return cached
                self._engines = {
                    existing: value
                    for existing, value in self._engines.items()
                    if existing[0] != canonical_id
                }
                self._engines[key] = engine
                return engine

    def state(
        self,
        session_id: str,
        *,
        time_ms: int | None,
        lap: int | None,
        phase: str,
    ) -> dict[str, object]:
        if (time_ms is None) == (lap is None):
            raise ReplayCursorError("supply exactly one of time_ms or lap")
        engine = self._engine(session_id)
        state = (
            engine.state_at(time_ms)
            if time_ms is not None
            else engine.state_at_lap(lap if lap is not None else 0, phase=phase)
        )
        return state_to_dict(state)

    def timeline(
        self,
        session_id: str,
        *,
        from_ms: int | None,
        to_ms: int | None,
        types: frozenset[RaceEventType] | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        events = self._engine(session_id).timeline.events
        selected = [
            event
            for event in events
            if (from_ms is None or event.session_time_ms >= from_ms)
            and (to_ms is None or event.session_time_ms <= to_ms)
            and (types is None or event.event_type in types)
        ]
        page = selected[offset : offset + limit]
        return {
            "items": [
                {
                    "event_id": event.event_id,
                    "session_time_ms": event.session_time_ms,
                    "priority": event.priority,
                    "sequence": event.sequence,
                    "event_type": event.event_type.value,
                    "driver_id": None if event.driver_id is None else str(event.driver_id),
                    "source": event.source,
                    "source_key": event.source_key,
                    "payload": json.loads(canonical_event_payload(event.payload)),
                }
                for event in page
            ],
            "offset": offset,
            "limit": limit,
            "total": len(selected),
        }


__all__ = ["ReplayService"]
