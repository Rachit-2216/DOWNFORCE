"""Authenticate one product release and wire the approved read-only stores."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.bundle import BundledReleaseReader
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.manifest import ManifestLimits, RuntimeManifest
from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore
from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore
from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository
from downforce_core.runtime_blob.transport import ObjectReader, PrivateBlobReader
from fastapi import FastAPI

from app.core.config import Settings
from app.services.analytics_service import AnalyticsService
from app.services.replay_service import ReplayService
from app.services.runtime_blob_services import (
    BlobAnalyticsEngine,
    BlobIntelligenceService,
    BlobStrategyService,
    BlobTelemetryService,
)
from app.services.telemetry_context_service import TelemetryContextService


def _release_body(reader: ObjectReader) -> bytes:
    limit = ManifestLimits().max_manifest_bytes
    body = bytearray()
    try:
        with ExitStack() as stack:
            stream = reader.iter_bytes("release.json", max_bytes=limit)
            close = getattr(stream, "close", None)
            if close is not None:
                stack.callback(close)
            for chunk in stream:
                if not isinstance(chunk, bytes) or len(chunk) > limit - len(body):
                    raise StorageIntegrityError("runtime release stream is malformed or oversized")
                body.extend(chunk)
    except Exception:
        raise StorageIntegrityError("runtime release stream is unavailable or invalid") from None
    return bytes(body)


def create_blob_app(
    settings: Settings,
    app_factory: Callable[..., FastAPI],
    *,
    reader: ObjectReader | None = None,
    manifest_body: bytes | None = None,
) -> FastAPI:
    if settings.storage_backend != "vercel_blob":
        raise StorageIntegrityError("runtime Blob builder requires explicit cloud configuration")
    # Settings validate completeness; never discover credentials or a provider index.
    if reader is None:
        if settings.blob_token is None:
            raise StorageIntegrityError("runtime Blob server credential is unavailable")
        reader = PrivateBlobReader(
            settings.blob_origin or "",
            settings.blob_release_prefix or "",
            settings.blob_token.get_secret_value(),
        )
    manifest = RuntimeManifest.from_bytes(
        _release_body(reader) if manifest_body is None else manifest_body,
        settings.blob_release_sha256 or "",
    )
    cache = RuntimeBlobCache(
        settings.blob_cache_root,
        manifest,
        reader,
        max_disk_bytes=settings.blob_cache_max_bytes,
        max_cache_files=settings.blob_cache_max_files,
        lock_timeout=settings.blob_cache_lock_timeout,
    )
    return _create_readonly_app(settings, app_factory, cache)


def create_bundled_app(settings: Settings, app_factory: Callable[..., FastAPI]) -> FastAPI:
    if settings.storage_backend != "bundled":
        raise StorageIntegrityError("runtime bundle builder requires bundled storage configuration")
    # Authenticate the packaged pin, manifest and inventory before creating any cache.
    # A missing or corrupt bundle must never fall back to an empty local repository.
    reader = BundledReleaseReader(settings.bundle_root, settings.bundle_release_sha256)
    cache = RuntimeBlobCache(
        settings.bundle_cache_root,
        reader.manifest,
        reader,
        max_disk_bytes=settings.bundle_cache_max_bytes,
        max_cache_files=settings.bundle_cache_max_files,
        lock_timeout=settings.bundle_cache_lock_timeout,
    )
    application = _create_readonly_app(settings, app_factory, cache)
    application.state.runtime_bundle_reader = reader
    return application


def _create_readonly_app(
    settings: Settings, app_factory: Callable[..., FastAPI], cache: RuntimeBlobCache
) -> FastAPI:
    repository = BlobDownforceRepository(cache)
    archive = BlobHistoricalArchiveStore(cache)
    detailed = BlobDetailedStore(cache)
    replay = ReplayService(repository)
    engine = BlobAnalyticsEngine(archive, cache)
    application = app_factory(
        settings=settings,
        repository=repository,
        detailed_store=detailed,
        archive_store=archive,
        telemetry_service=BlobTelemetryService(
            detailed, TelemetryContextService(repository, archive)
        ),
        analytics_service=AnalyticsService(engine),
        replay_service=replay,
        intelligence_service=BlobIntelligenceService(repository, replay),
        strategy_service=BlobStrategyService(repository),
    )
    application.state.runtime_blob_cache = cache
    return application
