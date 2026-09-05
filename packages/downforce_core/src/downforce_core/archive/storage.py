"""Immutable archive storage and atomic catalog pointers."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Lock, RLock
from typing import Any, BinaryIO, cast
from uuid import uuid4

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.archive.contracts import HistoricalCatalog
from downforce_core.archive.schemas import ARCHIVE_SCHEMA_VERSION, ARCHIVE_SCHEMAS, ArchiveTableName
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256

_LOCAL_LOCKS_GUARD = Lock()
_LOCAL_LOCKS: dict[str, RLock] = {}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(_canonical_json(value))
    os.replace(temporary, path)


def _semantic_manifest(value: dict[str, object]) -> dict[str, object]:
    """Return revision-defining metadata without lifecycle timestamps."""

    semantic = deepcopy(value)
    for key in (
        "archive_schema_version",
        "data_revision",
        "metadata_sha256",
        "session_id",
        "source_revision",
        "tables",
    ):
        semantic.pop(key, None)
    quality = semantic.get("quality")
    if isinstance(quality, dict):
        quality.pop("validated_at_utc", None)
    provenance = semantic.get("provenance")
    if isinstance(provenance, list):
        for item in provenance:
            if isinstance(item, dict):
                item.pop("retrieved_at_utc", None)
    return semantic


def _revision_payload(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "archive_schema_version": manifest.get("archive_schema_version"),
        "session_id": manifest.get("session_id"),
        "source_revision": manifest.get("source_revision"),
        "metadata_sha256": manifest.get("metadata_sha256"),
        "tables": manifest.get("tables"),
    }


def _revision_for(payload: dict[str, object]) -> str:
    return "archive-revision-sha256-" + sha256(_canonical_json(payload)).hexdigest()


def _thread_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, RLock())


def _try_lock_file(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(Any, __import__("msvcrt"))

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    fcntl = cast(Any, __import__("fcntl"))

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt = cast(Any, __import__("msvcrt"))

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = cast(Any, __import__("fcntl"))

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class HistoricalArchiveStore:
    """Own broad archive data without mutating the Step 2 canonical replay store."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.root = self.project_root / ".downforce" / "archive"
        self.raw_root = self.root / "raw"
        self.sessions_root = self.root / "sessions"
        self.catalog_root = self.root / "catalog"
        self.sync_root = self.root / "sync"
        self.staging_root = self.root / "staging"

    @property
    def catalog_path(self) -> Path:
        return self.catalog_root / "historical-catalog.json"

    @property
    def quality_report_path(self) -> Path:
        return self.catalog_root / "quality-report.json"

    @property
    def sync_state_path(self) -> Path:
        return self.sync_root / "sync-state.json"

    def raw_dump_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise StorageIntegrityError("archive raw digest is invalid")
        return self.raw_root / "jolpica" / f"jolpica-csv-sha256-{digest}.zip"

    def write_sync_state(self, value: dict[str, object]) -> None:
        _atomic_json(self.sync_state_path, value)

    def load_sync_state(self) -> dict[str, object] | None:
        if not self.sync_state_path.is_file():
            return None
        try:
            payload = json.loads(self.sync_state_path.read_text("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("archive sync state must be an object")
            return cast(dict[str, object], payload)
        except (OSError, TypeError, ValueError) as exc:
            raise StorageIntegrityError("archive sync state is unreadable") from exc

    def save_catalog(self, catalog: HistoricalCatalog) -> None:
        _atomic_json(self.catalog_path, catalog.to_dict())

    def load_catalog(self) -> HistoricalCatalog:
        if not self.catalog_path.is_file():
            raise StorageIntegrityError("historical catalog has not been built")
        try:
            payload = json.loads(self.catalog_path.read_text("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("historical catalog must be an object")
            return HistoricalCatalog.from_dict(cast(dict[str, object], payload))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageIntegrityError("historical catalog is unreadable") from exc

    def save_quality_report(self, value: dict[str, object]) -> None:
        _atomic_json(self.quality_report_path, value)

    def _session_root(self, session_id: str) -> Path:
        if not session_id.startswith("archive-") or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in session_id
        ):
            raise StorageIntegrityError("archive session identifier is invalid")
        return self.sessions_root / session_id

    @contextmanager
    def exclusive_lock(self, name: str, *, timeout_seconds: float = 60.0) -> Iterator[None]:
        """Serialize archive publication across threads and CLI processes."""

        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
            raise StorageIntegrityError("archive lock identifier is invalid")
        lock_path = self.root / "locks" / f"{name}.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_contained(lock_path, self.root, must_exist=False)
        local = _thread_lock(lock_path)
        with local:
            with lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                deadline = time.monotonic() + timeout_seconds
                while not _try_lock_file(handle):
                    if time.monotonic() >= deadline:
                        raise StorageIntegrityError("archive lock acquisition timed out")
                    time.sleep(0.02)
                try:
                    yield
                finally:
                    _unlock_file(handle)

    def _revision_is_valid(
        self,
        destination: Path,
        *,
        revision: str,
        revision_payload: dict[str, object],
    ) -> bool:
        try:
            manifest_path = destination / "manifest.json"
            ensure_contained(manifest_path, self.root, must_exist=True)
            payload = json.loads(manifest_path.read_text("utf-8"))
            if not isinstance(payload, dict):
                return False
            manifest = cast(dict[str, object], payload)
            for key, expected in revision_payload.items():
                if manifest.get(key) != expected:
                    return False
            if manifest.get("data_revision") != revision:
                return False
            if _revision_for(_revision_payload(manifest)) != revision:
                return False
            semantic_digest = sha256(_canonical_json(_semantic_manifest(manifest))).hexdigest()
            if semantic_digest != revision_payload["metadata_sha256"]:
                return False
            table_files = cast(dict[str, dict[str, object]], revision_payload["tables"])
            for table_name in ArchiveTableName:
                table_meta = table_files[table_name.value]
                if table_meta.get("file") != f"{table_name.value}.parquet":
                    return False
                path = destination / str(table_meta["file"])
                ensure_contained(path, self.root, must_exist=True)
                if path.stat().st_size != int(str(table_meta["bytes"])):
                    return False
                if file_sha256(path) != str(table_meta["sha256"]):
                    return False
            return True
        except (KeyError, OSError, TypeError, ValueError, StorageIntegrityError):
            return False

    def revision_is_valid(self, session_id: str, revision: str) -> bool:
        prefix = "archive-revision-sha256-"
        digest = revision.removeprefix(prefix)
        if (
            not revision.startswith(prefix)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return False
        destination = self._session_root(session_id) / "revisions" / revision
        try:
            ensure_contained(destination, self.root, must_exist=True)
            payload = json.loads((destination / "manifest.json").read_text("utf-8"))
            if not isinstance(payload, dict):
                return False
            manifest = cast(dict[str, object], payload)
        except (OSError, TypeError, ValueError, StorageIntegrityError):
            return False
        return self._revision_is_valid(
            destination,
            revision=revision,
            revision_payload=_revision_payload(manifest),
        )

    @staticmethod
    def _replace_revision(staging: Path, destination: Path) -> None:
        quarantine = destination.with_name(f".{destination.name}.{uuid4().hex}.corrupt")
        os.replace(destination, quarantine)
        try:
            os.replace(staging, destination)
        except Exception:
            if not destination.exists() and quarantine.exists():
                os.replace(quarantine, destination)
            raise
        try:
            shutil.rmtree(quarantine)
        except OSError:
            # The valid replacement is already published. A Windows reader or
            # antivirus process may temporarily hold the quarantined files.
            pass

    def write_session(
        self,
        session_id: str,
        tables: dict[ArchiveTableName, pa.Table],
        *,
        source_revision: str,
        manifest: dict[str, object],
    ) -> str:
        with self.exclusive_lock(session_id):
            return self._write_session_locked(
                session_id,
                tables,
                source_revision=source_revision,
                manifest=manifest,
            )

    def _write_session_locked(
        self,
        session_id: str,
        tables: dict[ArchiveTableName, pa.Table],
        *,
        source_revision: str,
        manifest: dict[str, object],
    ) -> str:
        expected_tables = set(ArchiveTableName)
        if set(tables) != expected_tables:
            raise StorageIntegrityError("archive session tables are incomplete")
        staging = self.staging_root / f"session-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_contained(staging, self.root, must_exist=False)
        staging.mkdir(parents=True, exist_ok=False)
        table_files: dict[str, dict[str, object]] = {}
        try:
            for table_name in ArchiveTableName:
                table = tables[table_name]
                if not table.schema.equals(ARCHIVE_SCHEMAS[table_name], check_metadata=False):
                    raise StorageIntegrityError(
                        f"archive table schema is invalid: {table_name.value}"
                    )
                path = staging / f"{table_name.value}.parquet"
                pq.write_table(
                    table.replace_schema_metadata(None),
                    path,
                    compression="zstd",
                    compression_level=9,
                    use_dictionary=True,
                    write_statistics=True,
                    version="2.6",
                )
                table_files[table_name.value] = {
                    "file": path.name,
                    "sha256": file_sha256(path),
                    "rows": cast(int, table.num_rows),
                    "bytes": path.stat().st_size,
                }
            revision_payload: dict[str, object] = {
                "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
                "session_id": session_id,
                "source_revision": source_revision,
                "metadata_sha256": sha256(
                    _canonical_json(_semantic_manifest(manifest))
                ).hexdigest(),
                "tables": table_files,
            }
            revision = _revision_for(revision_payload)
            manifest_payload = {
                **manifest,
                **revision_payload,
                "data_revision": revision,
            }
            (staging / "manifest.json").write_bytes(_canonical_json(manifest_payload))
            session_root = self._session_root(session_id)
            destination = session_root / "revisions" / revision
            destination.parent.mkdir(parents=True, exist_ok=True)
            ensure_contained(destination, self.root, must_exist=False)
            if destination.exists():
                if self._revision_is_valid(
                    destination,
                    revision=revision,
                    revision_payload=revision_payload,
                ):
                    shutil.rmtree(staging)
                else:
                    self._replace_revision(staging, destination)
            else:
                os.replace(staging, destination)
            active_path = session_root / "active.json"
            try:
                current_active = self.active_revision(session_id)
            except StorageIntegrityError:
                current_active = None
            if current_active != revision:
                _atomic_json(active_path, {"data_revision": revision})
            return revision
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def active_revision(self, session_id: str) -> str | None:
        path = self._session_root(session_id) / "active.json"
        if not path.is_file():
            return None
        try:
            ensure_contained(path, self.root, must_exist=True)
            value = json.loads(path.read_text("utf-8"))
            if not isinstance(value, dict):
                raise TypeError("archive active pointer must be an object")
            payload = cast(dict[str, object], value)
        except (OSError, TypeError, ValueError, StorageIntegrityError) as exc:
            raise StorageIntegrityError("archive active pointer is unreadable") from exc
        revision = str(payload.get("data_revision", ""))
        prefix = "archive-revision-sha256-"
        digest = revision.removeprefix(prefix)
        if (
            not revision.startswith(prefix)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise StorageIntegrityError("archive active revision is invalid")
        return revision

    def load_manifest(self, session_id: str) -> dict[str, object]:
        revision = self.active_revision(session_id)
        if revision is None:
            raise StorageIntegrityError("archive session is not materialized")
        path = self._session_root(session_id) / "revisions" / revision / "manifest.json"
        try:
            ensure_contained(path, self.root, must_exist=True)
            payload = json.loads(path.read_text("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("archive session manifest must be an object")
            manifest = cast(dict[str, object], payload)
            if manifest.get("archive_schema_version") != ARCHIVE_SCHEMA_VERSION:
                raise StorageIntegrityError("archive session schema version is stale")
            if manifest.get("session_id") != session_id:
                raise StorageIntegrityError("archive session manifest identity mismatch")
            if manifest.get("data_revision") != revision:
                raise StorageIntegrityError("archive session revision mismatch")
            expected_metadata = str(manifest.get("metadata_sha256", ""))
            actual_metadata = sha256(_canonical_json(_semantic_manifest(manifest))).hexdigest()
            if expected_metadata != actual_metadata:
                raise StorageIntegrityError("archive session metadata digest mismatch")
            if _revision_for(_revision_payload(manifest)) != revision:
                raise StorageIntegrityError("archive session revision digest mismatch")
            return manifest
        except StorageIntegrityError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise StorageIntegrityError("archive session manifest is unreadable") from exc

    def load_table(self, session_id: str, table_name: ArchiveTableName) -> pa.Table:
        manifest = self.load_manifest(session_id)
        revision = str(manifest["data_revision"])
        table_meta = cast(dict[str, dict[str, object]], manifest["tables"])[table_name.value]
        if table_meta.get("file") != f"{table_name.value}.parquet":
            raise StorageIntegrityError(f"archive table path is invalid: {table_name.value}")
        path = self._session_root(session_id) / "revisions" / revision / str(table_meta["file"])
        ensure_contained(path, self.root, must_exist=True)
        if path.stat().st_size != int(str(table_meta["bytes"])):
            raise StorageIntegrityError(f"archive table size mismatch: {table_name.value}")
        if file_sha256(path) != str(table_meta["sha256"]):
            raise StorageIntegrityError(f"archive table digest mismatch: {table_name.value}")
        try:
            table = pq.read_table(path)
        except (OSError, pa.ArrowException) as exc:
            raise StorageIntegrityError(f"archive table is unreadable: {table_name.value}") from exc
        if not table.schema.equals(ARCHIVE_SCHEMAS[table_name], check_metadata=False):
            raise StorageIntegrityError(f"archive table schema mismatch: {table_name.value}")
        if table.num_rows != int(str(table_meta["rows"])):
            raise StorageIntegrityError(f"archive table row count mismatch: {table_name.value}")
        return table

    def storage_report(self) -> dict[str, object]:
        files = [path for path in self.root.rglob("*") if path.is_file()]
        by_suffix: dict[str, int] = {}
        for path in files:
            suffix = path.suffix.casefold() or "no_suffix"
            by_suffix[suffix] = by_suffix.get(suffix, 0) + path.stat().st_size
        session_count = sum(1 for _ in self.sessions_root.glob("archive-*/active.json"))
        return {
            "root": str(self.root),
            "file_count": len(files),
            "session_count": session_count,
            "total_bytes": sum(path.stat().st_size for path in files),
            "bytes_by_suffix": dict(sorted(by_suffix.items())),
            "catalog_bytes": self.catalog_path.stat().st_size if self.catalog_path.is_file() else 0,
        }

    def session_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.parent.name
                for path in self.sessions_root.glob("archive-*/active.json")
                if path.is_file()
            )
        )


__all__ = ["HistoricalArchiveStore"]
