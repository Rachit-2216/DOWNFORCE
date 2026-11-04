"""Read-only canonical repository preserving complete legacy dataset validation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.events import RaceEvent
from downforce_core.domain.identifiers import SessionId
from downforce_core.exceptions import SessionNotFoundError, StorageIntegrityError
from downforce_core.normalization.models import NormalizedSession
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.storage.layout import StorageLayout
from downforce_core.storage.manifest import SessionManifest
from downforce_core.storage.repository import DownforceRepository, SessionSummary
from downforce_core.storage.schemas import CanonicalTableName
from downforce_core.versions import (
    CANONICAL_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    REPLAY_VERSION,
    STORAGE_FORMAT_VERSION,
    TIMELINE_VERSION,
)


class _ReadOnlyLayout(StorageLayout):
    def __init__(self, project_root: Path) -> None:
        # Inherit the original pure path helpers without StorageLayout's writer mkdirs.
        object.__setattr__(self, "project_root", project_root)


class BlobDownforceRepository(DownforceRepository):
    def __init__(self, cache: RuntimeBlobCache) -> None:
        self.cache = cache
        self.layout = _ReadOnlyLayout(cache.release_root)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.cache.release_root).as_posix()

    def resolve_session_id(self, session_id: str | SessionId) -> str:
        path = self._relative(self.layout.alias_pointer(str(session_id)))
        if path not in self.cache.manifest.files:
            return str(session_id)
        try:
            with self.cache.lease([path]):
                return super().resolve_session_id(session_id)
        except (KeyError, OSError, UnicodeError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError("runtime session alias is unavailable or invalid") from None

    def active_dataset_identity(self, session_id: str | SessionId) -> tuple[str, str]:
        canonical = self.resolve_session_id(session_id)
        path = self._relative(self.layout.active_pointer(canonical))
        if path not in self.cache.manifest.files:
            raise SessionNotFoundError(f"canonical session is not available: {session_id}")
        try:
            with self.cache.lease([path]):
                return super().active_dataset_identity(session_id)
        except (KeyError, OSError, UnicodeError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime canonical pointer is unavailable or invalid"
            ) from None

    def _load_dataset_manifest(self, session_id: str, dataset_id: str) -> SessionManifest:
        root = self.layout.normalized_dataset(session_id, dataset_id)
        path = self._relative(root / "manifest.json")
        try:
            with self.cache.lease([path]):
                return super()._load_dataset_manifest(session_id, dataset_id)
        except (KeyError, OSError, UnicodeError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime canonical manifest is unavailable or invalid"
            ) from None

    def _verify_manifest(self, manifest: SessionManifest, root: Path) -> None:
        # Only detect headers for which the original validator must stop before IO.
        # Delegate the actual checks, ordering, and error messages to that validator.
        if (
            manifest.canonical_schema_version != CANONICAL_SCHEMA_VERSION
            or manifest.normalization_version != NORMALIZATION_VERSION
            or manifest.storage_format_version != STORAGE_FORMAT_VERSION
            or (manifest.timeline_version is None) != (manifest.replay_version is None)
            or manifest.timeline_version not in {None, TIMELINE_VERSION}
            or manifest.replay_version not in {None, REPLAY_VERSION}
        ):
            super()._verify_manifest(manifest, root)
            return
        paths = [self._relative(root / "manifest.json")]
        paths.extend(
            self._relative(root / "tables" / f"{name.value}.parquet")
            for name in CanonicalTableName
            if manifest.tables[name.value].materialized
        )
        try:
            with self.cache.lease(paths):
                super()._verify_manifest(manifest, root)
        except (KeyError, OSError, StorageIntegrityError, pa.ArrowException):
            raise StorageIntegrityError(
                "runtime canonical dataset is unavailable or invalid"
            ) from None

    def _table_from_manifest(
        self, manifest: SessionManifest, table_name: CanonicalTableName
    ) -> pa.Table:
        artifact = manifest.tables[table_name.value]
        if not artifact.materialized or artifact.path is None:
            return super()._table_from_manifest(manifest, table_name)
        expected = f"tables/{table_name.value}.parquet"
        if artifact.path != expected:
            raise StorageIntegrityError("runtime canonical table path is invalid")
        root = self.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        try:
            with self.cache.lease([self._relative(root / expected)]):
                return super()._table_from_manifest(manifest, table_name)
        except (KeyError, OSError, StorageIntegrityError, pa.ArrowException):
            raise StorageIntegrityError(
                "runtime canonical table is unavailable or invalid"
            ) from None

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        paths = tuple(
            path
            for path in self.cache.manifest.paths(".downforce/normalized", suffix="/active.json")
            if len(path.split("/")) == 4
        )
        if not paths:
            return ()
        with self.cache.lease(paths):
            summaries: list[SessionSummary] = []
            for path in paths:
                # Discovery must use the release index, not physical cache leftovers.
                # Preserve the original reader's full per-dataset verification and
                # small summary mapper/validation below without synthesizing files.
                manifest = self.load_manifest(path.split("/")[2])
                session_data = manifest.session
                provider_data = manifest.provider
                season = session_data.get("season")
                event_name = session_data.get("event_name")
                session_type = session_data.get("session_type")
                provider = provider_data.get("name")
                if not isinstance(season, int) or not all(
                    isinstance(value, str) for value in (event_name, session_type, provider)
                ):
                    raise StorageIntegrityError("session summary metadata is malformed")
                summaries.append(
                    SessionSummary(
                        session_id=manifest.session_id,
                        dataset_id=manifest.dataset_id,
                        season=season,
                        event_name=cast(str, event_name),
                        session_type=cast(str, session_type),
                        provider=cast(str, provider),
                        created_at_utc=manifest.created_at_utc,
                    )
                )
            return tuple(summaries)

    def write_session(
        self,
        normalized: NormalizedSession,
        snapshot_id: str,
        *,
        events: tuple[RaceEvent, ...] | None = None,
    ) -> SessionManifest:
        raise StorageIntegrityError("runtime product storage is read-only")
