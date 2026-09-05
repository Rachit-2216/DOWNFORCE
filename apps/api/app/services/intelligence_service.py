"""Cached application boundary for offline historical ML inference."""

from __future__ import annotations

from threading import Lock

from downforce_core.ml import ArtifactUnavailableError, MLInferenceEngine
from downforce_core.storage import DownforceRepository

from app.services.replay_service import ReplayService


class IntelligenceService:
    def __init__(self, repository: DownforceRepository, replay: ReplayService) -> None:
        self.repository = repository
        self.replay = replay
        self.engine = MLInferenceEngine(repository, repository.layout.project_root)
        self._lock = Lock()
        self._cache: dict[tuple[str, str, str, int, str], dict[str, object]] = {}

    def status(self) -> dict[str, object]:
        return self.engine.status()

    @staticmethod
    def _unavailable(time_ms: int, reason: str) -> dict[str, object]:
        return {
            "availability": "unavailable",
            "reason": reason,
            "model_version": None,
            "dataset_digest": None,
            "assumptions": [],
            "as_of": {"time_ms": time_ms, "lap": None},
            "pace": None,
            "tyre_degradation": None,
            "pit_loss": None,
        }

    def intelligence(self, session_id: str, driver_id: str, time_ms: int) -> dict[str, object]:
        state = self.replay.state(session_id, time_ms=time_ms, lap=None, phase="end")
        drivers = state.get("drivers")
        driver = (
            next(
                (
                    item
                    for item in drivers
                    if isinstance(drivers, list)
                    and isinstance(item, dict)
                    and item.get("driver_id") == driver_id
                ),
                None,
            )
            if isinstance(drivers, list)
            else None
        )
        if driver is None:
            return self._unavailable(time_ms, "unknown_driver")
        if driver.get("in_pit") is True:
            return self._unavailable(time_ms, "driver_in_pit")
        if driver.get("status") in {
            "retired",
            "disqualified",
            "did-not-start",
            "did-not-finish",
            "not-classified",
        }:
            return self._unavailable(time_ms, "driver_not_running")
        if state.get("track_status") != "clear":
            return self._unavailable(time_ms, "neutralized_or_unknown_track")
        canonical_id, dataset_id = self.repository.active_dataset_identity(session_id)
        status = self.engine.status()
        digest = status.get("dataset_digest")
        if not isinstance(digest, str):
            return self._unavailable(time_ms, str(status.get("reason") or "model_unavailable"))
        try:
            result = self.engine.predict(canonical_id, driver_id, time_ms)
        except ArtifactUnavailableError as exc:
            return self._unavailable(time_ms, str(exc))
        as_of = result.get("as_of")
        boundary = as_of.get("time_ms") if isinstance(as_of, dict) else time_ms
        if not isinstance(boundary, int):
            boundary = time_ms
        key = (canonical_id, dataset_id, driver_id, boundary, digest)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            self._cache[key] = result
        return result


__all__ = ["IntelligenceService"]
