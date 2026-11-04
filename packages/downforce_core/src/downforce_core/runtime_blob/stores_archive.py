"""Read-only historical archive tables and truthful release-index product accounting."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Never

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.archive.contracts import HistoricalCatalog
from downforce_core.archive.schemas import ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import RuntimeBlobCache


class BlobHistoricalArchiveStore(HistoricalArchiveStore):
    def __init__(self, cache: RuntimeBlobCache) -> None:
        self.cache = cache
        super().__init__(cache.release_root)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.cache.release_root).as_posix()

    def load_catalog(self) -> HistoricalCatalog:
        try:
            with self.cache.lease([self._relative(self.catalog_path)]):
                return super().load_catalog()
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            AttributeError,
            RecursionError,
            StorageIntegrityError,
        ):
            raise StorageIntegrityError(
                "runtime historical catalog is unavailable or invalid"
            ) from None

    def load_sync_state(self) -> dict[str, object] | None:
        path = self._relative(self.sync_state_path)
        if path not in self.cache.manifest.files:
            return None
        try:
            with self.cache.lease([path]):
                return super().load_sync_state()
        except (KeyError, OSError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime archive sync state is unavailable or invalid"
            ) from None

    def active_revision(self, session_id: str) -> str | None:
        path = self._relative(self._session_root(session_id) / "active.json")
        if path not in self.cache.manifest.files:
            return None
        try:
            with self.cache.lease([path]):
                return super().active_revision(session_id)
        except (KeyError, OSError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime archive pointer is unavailable or invalid"
            ) from None

    @contextmanager
    def lease_session_metadata(self, session_id: str) -> Iterator[dict[str, object]]:
        root = self._session_root(session_id)
        pointer = self._relative(root / "active.json")
        with ExitStack() as stack:
            try:
                stack.enter_context(self.cache.lease([pointer]))
                revision = super().active_revision(session_id)
                if revision is None:
                    raise StorageIntegrityError("runtime archive session is not materialized")
                path = self._relative(root / "revisions" / revision / "manifest.json")
                stack.enter_context(self.cache.lease([path]))
                manifest = super().load_manifest(session_id)
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                RecursionError,
                StorageIntegrityError,
            ):
                raise StorageIntegrityError(
                    "runtime archive metadata is unavailable or invalid"
                ) from None
            yield manifest

    def load_manifest(self, session_id: str) -> dict[str, object]:
        with self.lease_session_metadata(session_id) as manifest:
            return manifest

    def load_table(self, session_id: str, table_name: ArchiveTableName) -> pa.Table:
        with self.lease_session_metadata(session_id) as manifest:
            revision = str(manifest["data_revision"])
            path = self._relative(
                self._session_root(session_id)
                / "revisions"
                / revision
                / f"{table_name.value}.parquet"
            )
            try:
                with self.cache.lease([path]):
                    return super().load_table(session_id, table_name)
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                AttributeError,
                StorageIntegrityError,
                pa.ArrowException,
            ):
                raise StorageIntegrityError(
                    "runtime archive table is unavailable or invalid"
                ) from None

    def revision_is_valid(self, session_id: str, revision: str) -> bool:
        prefix = "archive-revision-sha256-"
        digest = revision.removeprefix(prefix)
        if (
            not revision.startswith(prefix)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return False
        root = self._session_root(session_id) / "revisions" / revision
        paths = [self._relative(root / "manifest.json")]
        paths.extend(self._relative(root / f"{name.value}.parquet") for name in ArchiveTableName)
        try:
            with self.cache.lease(paths):
                return super().revision_is_valid(session_id, revision)
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            AttributeError,
            RecursionError,
            StorageIntegrityError,
        ):
            return False

    def session_ids(self) -> tuple[str, ...]:
        return tuple(
            path.split("/")[3]
            for path in self.cache.manifest.paths(
                ".downforce/archive/sessions", suffix="/active.json"
            )
            if len(path.split("/")) == 5 and path.split("/")[3].startswith("archive-")
        )

    def storage_report(self) -> dict[str, object]:
        paths = self.cache.manifest.paths(".downforce/archive")
        by_suffix: dict[str, int] = {}
        for path in paths:
            suffix = Path(path).suffix.casefold() or "no_suffix"
            by_suffix[suffix] = (
                by_suffix.get(suffix, 0) + self.cache.manifest.entry(path).size_bytes
            )
        catalog = self.cache.manifest.files.get(self._relative(self.catalog_path))
        return {
            "root": str(self.root),
            "file_count": len(paths),
            "session_count": len(self.session_ids()),
            "total_bytes": sum(by_suffix.values()),
            "bytes_by_suffix": dict(sorted(by_suffix.items())),
            "catalog_bytes": catalog.size_bytes if catalog is not None else 0,
        }

    def write_session(self, *args: object, **kwargs: object) -> Never:
        raise StorageIntegrityError("runtime product storage is read-only")

    def _write_session_locked(self, *args: object, **kwargs: object) -> Never:
        raise StorageIntegrityError("runtime product storage is read-only")

    def write_sync_state(self, value: dict[str, object]) -> None:
        raise StorageIntegrityError("runtime product storage is read-only")

    def save_catalog(self, catalog: HistoricalCatalog) -> None:
        raise StorageIntegrityError("runtime product storage is read-only")

    def save_quality_report(self, value: dict[str, object]) -> None:
        raise StorageIntegrityError("runtime product storage is read-only")

    @contextmanager
    def exclusive_lock(self, name: str, *, timeout_seconds: float = 60.0) -> Iterator[None]:
        raise StorageIntegrityError("runtime product storage is read-only")
        yield
