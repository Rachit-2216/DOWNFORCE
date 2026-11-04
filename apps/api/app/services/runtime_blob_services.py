"""Small read-only service bridges; original telemetry and inference math is inherited."""

from __future__ import annotations

import json
import re
from threading import RLock
from typing import Any

from downforce_core.analytics.circuits import CircuitIdentityResolver
from downforce_core.analytics.engine import AnalyticsEngine, AnalyticsSnapshot
from downforce_core.archive.contracts import HistoricalCatalog
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactUnavailableError
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.stores_analytics import (
    BlobAnalyticsDerivedStore,
    BlobCircuitIdentityResolver,
)
from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore
from downforce_core.runtime_blob.stores_artifacts import BlobArtifactStore
from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore
from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository
from downforce_core.strategy.engine import StrategyEngine, StrategyUnavailableError
from downforce_core.telemetry.reader import TelemetryReader
from fastapi import HTTPException

from app.services.intelligence_service import IntelligenceService
from app.services.replay_service import ReplayService
from app.services.strategy_service import StrategyService
from app.services.telemetry_context_service import TelemetryContextService
from app.services.telemetry_service import TelemetryService


class BlobTelemetryReader(TelemetryReader):
    def __init__(self, store: BlobDetailedStore) -> None:
        super().__init__(store)
        self.blob_store = store

    def sessions(self) -> list[dict[str, Any]]:
        # Mirror the original catalog mapper, but enumerate authenticated identities only.
        result = []
        for session_id in self.blob_store.session_ids():
            manifest = self.store.manifest(session_id)
            metadata = manifest.get("metadata")
            if not isinstance(metadata, dict):
                raise StorageIntegrityError("runtime telemetry metadata is invalid")
            session = metadata.get("session", {})
            if not isinstance(session, dict):
                raise StorageIntegrityError("runtime telemetry session metadata is invalid")
            names = sorted(manifest["tables"])
            drivers = {
                name.removeprefix("car-data-") for name in names if name.startswith("car-data-")
            }
            if "laps" in names:
                laps = self.store.read_table(session_id, "laps", revision=manifest["revision"])
                if "driver_number" in laps.column_names:
                    drivers |= {
                        str(value)
                        for value in laps["driver_number"].to_pylist()
                        if value is not None
                    }
            result.append(
                {
                    "session_id": session_id,
                    "revision": manifest["revision"],
                    "season": session.get("season"),
                    "session_type": session.get("session_type"),
                    "event": session.get("event_name"),
                    "circuit": session.get("circuit", session.get("circuit_name")),
                    "drivers": sorted(drivers),
                    "available_tables": names,
                    "channel_availability": {
                        "car": "available"
                        if any(n.startswith("car-data-") for n in names)
                        else "unavailable",
                        "position": "available"
                        if any(n.startswith("pos-data-") for n in names)
                        else "unavailable",
                        "weather": "available" if "weather-data" in names else "unavailable",
                        "corners": "available" if "corners" in names else "unavailable",
                    },
                }
            )
        return result


class BlobTelemetryService(TelemetryService):
    def __init__(self, store: BlobDetailedStore, context: TelemetryContextService) -> None:
        super().__init__(store, context)
        self.blob_store = store
        self.reader = BlobTelemetryReader(store)

    def _pin(self, session_id: str, revision: str | None) -> str:
        try:
            validate_safe_identifier(session_id)
        except ValueError as exc:
            raise HTTPException(422, "Invalid telemetry session identifier") from exc
        if revision is not None and not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise HTTPException(422, "Invalid telemetry revision identifier")
        if revision is None and not self.blob_store.has_session(session_id):
            raise HTTPException(404, "Telemetry session not found")
        if revision is not None:
            path = f".downforce/detailed/canonical/{session_id}/revisions/{revision}/manifest.json"
            if path not in self.blob_store.cache.manifest.files:
                raise HTTPException(404, "Telemetry revision not found")
        # Inherited filesystem probes are safe only while genuine metadata is leased.
        with self.blob_store.lease_session_metadata(session_id, revision=revision):
            return super()._pin(session_id, revision)


class BlobIntelligenceService(IntelligenceService):
    def __init__(self, repository: BlobDownforceRepository, replay: ReplayService) -> None:
        super().__init__(repository, replay)
        self.engine.store = BlobArtifactStore(repository.cache)


class BlobAnalyticsEngine(AnalyticsEngine):
    def __init__(self, store: BlobHistoricalArchiveStore, cache: RuntimeBlobCache) -> None:
        # The original default resolver discovers raw provider zips at construction.
        super().__init__(store, circuit_resolver=CircuitIdentityResolver())
        self.cache = cache
        self.derived_store = BlobAnalyticsDerivedStore(cache)

    def snapshot(self, *, force: bool = False) -> AnalyticsSnapshot:
        if force:
            raise StorageIntegrityError("runtime analytics is read-only")
        with self._lock:
            if self._snapshot is None:
                resolver = BlobCircuitIdentityResolver(self.cache)
                catalog = self.store.load_catalog()
                stored = self.derived_store.load(catalog.source_revision)
                if (
                    stored is None
                    or stored.provider_circuit_identities != resolver.provider_identity_count()
                ):
                    raise StorageIntegrityError("runtime analytics circuit mapping is incompatible")
                self.circuit_resolver = resolver
                self._snapshot = AnalyticsSnapshot(
                    source_revision=catalog.source_revision,
                    observations=stored.observations,
                    catalog=catalog,
                    digest=stored.digest,
                    built_at_utc=stored.built_at_utc,
                    provider_circuit_identities=stored.provider_circuit_identities,
                )
            return self._snapshot

    def _build_snapshot(self, catalog: HistoricalCatalog) -> AnalyticsSnapshot:
        raise StorageIntegrityError("runtime analytics is read-only")

    def rebuild_manifest(self) -> dict[str, object]:
        raise StorageIntegrityError("runtime analytics is read-only")


class BlobStrategyService(StrategyService):
    def __init__(self, repository: BlobDownforceRepository) -> None:
        super().__init__(repository, repository.cache.release_root)
        self.cache = repository.cache
        self.artifact_store = BlobArtifactStore(self.cache)
        self._engine_lock = RLock()

    def _strategy_engine(self) -> StrategyEngine:
        with self._engine_lock:
            if self._engine is None:
                registry = "artifacts/ml/registry.json"
                try:
                    with self.cache.lease([registry]) as paths:
                        # Validate through the approved bridge before the original constructor.
                        self.artifact_store.load()
                        value = json.loads(paths[registry].read_text("utf-8"))
                        bundle = f"artifacts/ml/{value['path']}"
                        with self.cache.lease([bundle]):
                            self._engine = StrategyEngine(self.repository, self.project_root)
                except (
                    KeyError,
                    OSError,
                    ValueError,
                    TypeError,
                    RecursionError,
                    StorageIntegrityError,
                    ArtifactUnavailableError,
                ):
                    raise StrategyUnavailableError("missing_or_corrupt_model") from None
            # Model/Monte Carlo CPU work occurs after both file leases are released.
            return self._engine
