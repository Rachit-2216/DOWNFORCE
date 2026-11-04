"""Immutable detailed Parquet revisions with verified, atomic active pointers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.status import (
    filesystem_ancestor,
    guard_operation,
    owned_temporary_directory,
    parquet_peak_bytes,
    revision_peak_bytes,
)
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256

SCHEMA_VERSION = "detailed-columnar-v1"


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


class DetailedStore:
    """Each driver/channel has its own file; time statistics prune Parquet row groups.

    Immutable revision directories are published with an atomic rename. Concurrent
    identical writers converge on the same content identity; pointers only ever refer
    to complete validated revisions. A consumer can pin a revision across requests.
    """

    def __init__(self, project_root: Path, *, detailed_root: Path | None = None) -> None:
        project = project_root.absolute()
        if not project.is_dir():
            raise ValueError("project root must exist")
        root = DetailedPaths(project, override=detailed_root).canonical
        filesystem_ancestor(root)
        self.root = root

    def _path(self, session_id: str, *parts: str, exists: bool = True) -> Path:
        validate_safe_identifier(session_id)
        path = self.root / session_id
        for part in parts:
            path /= part
        return ensure_contained(path, self.root, must_exist=exists)

    def publish(
        self, session_id: str, tables: dict[str, pa.Table], metadata: dict[str, Any]
    ) -> str:
        validate_safe_identifier(session_id)
        if not tables:
            raise ValueError("cannot publish an empty revision")
        for name in tables:
            validate_safe_identifier(name)
        peak = revision_peak_bytes(
            (parquet_peak_bytes(table, row_group_size=16384) for table in tables.values()),
            metadata_bytes=len(_encode(metadata).encode("utf-8")),
            schema_bytes=sum(table.schema.serialize().size for table in tables.values()),
        )
        guard_operation(self.root, next_peak_bytes=peak, operation="canonical revision publication")
        self.root.mkdir(parents=True, exist_ok=True)
        revisions = self._path(session_id, "revisions", exists=False)
        revisions.mkdir(parents=True, exist_ok=True)
        # Staging remains local and never referenced by active.json.
        with owned_temporary_directory(prefix=".staging-", directory=revisions) as temporary:
            staging = Path(temporary)
            descriptors: dict[str, Any] = {}
            for name, source in sorted(tables.items()):
                table = source
                path = staging / f"{name}.parquet"
                pq.write_table(
                    table,
                    path,
                    compression="zstd",
                    compression_level=6,
                    row_group_size=16384,
                    use_dictionary=False,
                    use_compliant_nested_type=False,
                )
                with pq.ParquetFile(path) as reader:
                    check = reader.read()
                if not check.equals(table, check_metadata=True):
                    raise StorageIntegrityError("detailed table failed round-trip validation")
                descriptors[name] = {
                    "sha256": file_sha256(path),
                    "bytes": path.stat().st_size,
                    "rows": table.num_rows,
                    "schema": str(table.schema),
                }
            semantic = {
                "schema_version": SCHEMA_VERSION,
                "session_id": session_id,
                "metadata": metadata,
                "tables": descriptors,
            }
            revision = _digest(semantic)
            manifest = {**semantic, "revision": revision}
            write_text_atomic(staging / "manifest.json", _encode(manifest))
            target = self._path(session_id, "revisions", revision, exists=False)
            if target.exists():
                self.validate(session_id, revision=revision)
            else:
                try:
                    os.rename(staging, target)
                except PermissionError as exc:
                    exc.add_note(
                        f"canonical revision rename: source={staging}; destination={target}"
                    )
                    raise
                except OSError:
                    if not target.exists():
                        raise
                    self.validate(session_id, revision=revision)
            pointer = self._path(session_id, "active.json", exists=False)
            write_text_atomic(pointer, _encode({"revision": revision}))
        return revision

    def manifest(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
        try:
            if revision is None:
                pointer = json.loads(self._path(session_id, "active.json").read_text())
                revision = pointer["revision"]
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise StorageIntegrityError("invalid detailed revision identity")
            path = self._path(session_id, "revisions", revision, "manifest.json")
            manifest: dict[str, Any] = json.loads(path.read_text())
            semantic = {k: v for k, v in manifest.items() if k != "revision"}
            if (
                manifest.get("revision") != revision
                or _digest(semantic) != revision
                or manifest.get("session_id") != session_id
                or manifest.get("schema_version") != SCHEMA_VERSION
            ):
                raise StorageIntegrityError("detailed manifest identity mismatch")
            return manifest
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise StorageIntegrityError("detailed revision unavailable or invalid") from exc

    def _table_path(self, manifest: dict[str, Any], name: str) -> Path:
        validate_safe_identifier(name)
        descriptor = manifest["tables"].get(name)
        if descriptor is None:
            raise KeyError(f"unavailable detailed table: {name}")
        path = self._path(
            manifest["session_id"], "revisions", manifest["revision"], f"{name}.parquet"
        )
        if path.stat().st_size != descriptor["bytes"] or file_sha256(path) != descriptor["sha256"]:
            raise StorageIntegrityError("detailed table checksum mismatch")
        with pq.ParquetFile(path) as parquet:
            if (
                parquet.metadata.num_rows != descriptor["rows"]
                or str(parquet.schema_arrow) != descriptor["schema"]
            ):
                raise StorageIntegrityError("detailed table schema mismatch")
        return path

    def validate(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
        manifest = self.manifest(session_id, revision=revision)
        for name in manifest["tables"]:
            self._table_path(manifest, name)
        return manifest

    def read_table(
        self,
        session_id: str,
        name: str,
        *,
        revision: str | None = None,
        time_range: tuple[float, float] | None = None,
        columns: list[str] | None = None,
    ) -> pa.Table:
        filters = None
        if time_range is not None:
            start, end = time_range
            if not math.isfinite(start) or not math.isfinite(end) or start > end:
                raise ValueError("invalid detailed time range")
            filters = [("session_time_ms", ">=", start), ("session_time_ms", "<=", end)]
        manifest = self.manifest(session_id, revision=revision)
        path = self._table_path(manifest, name)
        return pq.read_table(path, filters=filters, columns=columns)
