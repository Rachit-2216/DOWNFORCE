"""Provider-to-raw-to-canonical ingestion orchestration with truthful cache behavior."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter

from downforce_core.exceptions import NormalizationError, SessionNotFoundError
from downforce_core.normalization.metadata import normalize_metadata
from downforce_core.normalization.pipeline import normalize_session
from downforce_core.providers.base import LoadOptions, RaceDataProvider, SessionRef
from downforce_core.replay.timeline import build_timeline
from downforce_core.storage.raw import commit_raw_snapshot
from downforce_core.storage.repository import DownforceRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    session_id: str
    snapshot_id: str
    dataset_id: str
    cache_hit: bool
    provider_called: bool
    raw_snapshot_reused: bool
    table_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    timings_ms: Mapping[str, float]


async def ingest_session(
    repository: DownforceRepository,
    reference: SessionRef,
    provider_factory: Callable[[], RaceDataProvider],
    *,
    force: bool = False,
) -> IngestionResult:
    """Ingest one session; a valid no-force cache hit never initializes its provider."""

    started = perf_counter()
    if not force:
        try:
            cached = repository.load_manifest(reference.session_id)
        except SessionNotFoundError:
            pass
        else:
            logger.info(
                "historical ingestion cache hit",
                extra={"session_id": cached.session_id, "dataset_id": cached.dataset_id},
            )
            return IngestionResult(
                session_id=cached.session_id,
                snapshot_id=cached.snapshot_id,
                dataset_id=cached.dataset_id,
                cache_hit=True,
                provider_called=False,
                raw_snapshot_reused=True,
                table_counts={name: artifact.row_count for name, artifact in cached.tables.items()},
                warnings=cached.warnings,
                timings_ms={"total": (perf_counter() - started) * 1_000},
            )

    provider = provider_factory()
    logger.info(
        "historical ingestion started",
        extra={
            "provider": provider.name,
            "season": reference.season,
            "event": str(reference.event),
            "session": reference.session.value,
            "force": force,
        },
    )
    provider_started = perf_counter()
    provider_session = await provider.load_session(
        reference,
        LoadOptions(force_refresh=force),
    )
    provider_ms = (perf_counter() - provider_started) * 1_000
    try:
        canonical_session_id = str(normalize_metadata(provider_session).session_id)
    except (TypeError, ValueError, KeyError) as exc:
        raise NormalizationError(f"provider session metadata cannot be normalized: {exc}") from exc

    raw_started = perf_counter()
    raw = commit_raw_snapshot(repository.layout, canonical_session_id, provider_session)
    raw_ms = (perf_counter() - raw_started) * 1_000

    normalization_started = perf_counter()
    normalized = normalize_session(raw.session)
    normalization_ms = (perf_counter() - normalization_started) * 1_000

    timeline_started = perf_counter()
    timeline = build_timeline(normalized)
    timeline_ms = (perf_counter() - timeline_started) * 1_000

    storage_started = perf_counter()
    manifest = repository.write_session(
        normalized,
        raw.snapshot_id,
        events=timeline.events,
    )
    storage_ms = (perf_counter() - storage_started) * 1_000
    total_ms = (perf_counter() - started) * 1_000
    timings = {
        "provider": provider_ms,
        "raw_commit": raw_ms,
        "normalization": normalization_ms,
        "timeline": timeline_ms,
        "canonical_commit": storage_ms,
        "total": total_ms,
    }
    logger.info(
        "historical ingestion completed",
        extra={
            "session_id": manifest.session_id,
            "snapshot_id": manifest.snapshot_id,
            "dataset_id": manifest.dataset_id,
            "raw_snapshot_reused": raw.reused,
            "warnings": len(manifest.warnings),
            "timings_ms": timings,
        },
    )
    return IngestionResult(
        session_id=manifest.session_id,
        snapshot_id=manifest.snapshot_id,
        dataset_id=manifest.dataset_id,
        cache_hit=False,
        provider_called=True,
        raw_snapshot_reused=raw.reused,
        table_counts={name: artifact.row_count for name, artifact in manifest.tables.items()},
        warnings=manifest.warnings,
        timings_ms=timings,
    )


__all__ = ["IngestionResult", "ingest_session"]
