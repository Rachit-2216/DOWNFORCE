"""Targeted read-only detailed data over an authenticated runtime release."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.dataset as ds  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.storage import DetailedStore
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import RuntimeBlobCache

_PREFIX = ".downforce/detailed/canonical"


class BlobDetailedStore(DetailedStore):
    def __init__(self, cache: RuntimeBlobCache) -> None:
        self.cache = cache
        # The cache root exists; the logical release need not. No constructor writes.
        super().__init__(cache.root, detailed_root=cache.release_root / ".downforce" / "detailed")

    def session_ids(self) -> tuple[str, ...]:
        # Sort identities, not object paths: '-' sorts before '/', so full-path
        # ordering reverses e.g. sprint and sprint-shootout before pagination.
        return tuple(
            sorted(
                path.split("/")[3]
                for path in self.cache.manifest.paths(_PREFIX, suffix="/active.json")
                if len(path.split("/")) == 5
            )
        )

    def has_session(self, session_id: str) -> bool:
        validate_safe_identifier(session_id)
        return f"{_PREFIX}/{session_id}/active.json" in self.cache.manifest.files

    @contextmanager
    def lease_session_metadata(
        self, session_id: str, *, revision: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Keep the genuine active pointer (if needed) and manifest live for a caller."""
        validate_safe_identifier(session_id)
        with ExitStack() as stack:
            try:
                if revision is None:
                    pointer = f"{_PREFIX}/{session_id}/active.json"
                    paths = stack.enter_context(self.cache.lease([pointer]))
                    revision = json.loads(paths[pointer].read_text("utf-8"))["revision"]
                if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                    raise StorageIntegrityError("invalid runtime detailed revision")
                path = f"{_PREFIX}/{session_id}/revisions/{revision}/manifest.json"
                paths = stack.enter_context(self.cache.lease([path]))
                if not isinstance(json.loads(paths[path].read_text("utf-8")), dict):
                    raise StorageIntegrityError("invalid runtime detailed manifest")
                manifest = super().manifest(session_id, revision=revision)
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                RecursionError,
                StorageIntegrityError,
            ):
                raise StorageIntegrityError(
                    "runtime detailed metadata is unavailable or invalid"
                ) from None
            yield manifest

    def manifest(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
        with self.lease_session_metadata(session_id, revision=revision) as manifest:
            return manifest

    def _selected_paths(self, manifest: dict[str, Any], names: list[str]) -> list[str]:
        tables = manifest.get("tables")
        if not isinstance(tables, dict):
            raise StorageIntegrityError("runtime detailed table metadata is invalid")
        for name in names:
            validate_safe_identifier(name)
            if tables.get(name) is None:
                raise KeyError(f"unavailable detailed table: {name}")
        return [
            f"{_PREFIX}/{manifest['session_id']}/revisions/{manifest['revision']}/{name}.parquet"
            for name in names
        ]

    def validate(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
        with self.lease_session_metadata(session_id, revision=revision) as manifest:
            try:
                paths = self._selected_paths(manifest, list(manifest["tables"]))
                with self.cache.lease(paths):
                    return super().validate(session_id, revision=manifest["revision"])
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                StorageIntegrityError,
                pa.ArrowException,
            ):
                raise StorageIntegrityError(
                    "runtime detailed revision is unavailable or invalid"
                ) from None

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
        projection_filter = None
        if time_range is not None:
            start, end = time_range
            if not math.isfinite(start) or not math.isfinite(end) or start > end:
                raise ValueError("invalid detailed time range")
            filters = [("session_time_ms", ">=", start), ("session_time_ms", "<=", end)]
            projection_filter = (ds.field("session_time_ms") >= start) & (
                ds.field("session_time_ms") <= end
            )
        validate_safe_identifier(name)
        with self.lease_session_metadata(session_id, revision=revision) as manifest:
            paths = self._selected_paths(manifest, [name])
            try:
                with self.cache.lease(paths):
                    try:
                        path = super()._table_path(manifest, name)
                        dataset = ds.dataset(path, format="parquet")
                    except (
                        KeyError,
                        OSError,
                        TypeError,
                        ValueError,
                        StorageIntegrityError,
                        pa.ArrowException,
                    ):
                        raise StorageIntegrityError(
                            "runtime detailed table is unavailable or invalid"
                        ) from None
                    # Arrow's scanner validates caller projections/filters from metadata
                    # without reading rows. These caller errors must retain Arrow types.
                    dataset.scanner(columns=columns, filter=projection_filter)
                    try:
                        return pq.read_table(path, filters=filters, columns=columns)
                    except (OSError, TypeError, ValueError, pa.ArrowException):
                        raise StorageIntegrityError(
                            "runtime detailed table is unavailable or invalid"
                        ) from None
            except (KeyError, OSError, StorageIntegrityError):
                raise StorageIntegrityError(
                    "runtime detailed table is unavailable or invalid"
                ) from None

    def publish(
        self, session_id: str, tables: dict[str, pa.Table], metadata: dict[str, Any]
    ) -> str:
        raise StorageIntegrityError("runtime product storage is read-only")
