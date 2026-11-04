"""Verified derived snapshots and compact circuit identities without archive rebuilds."""

from __future__ import annotations

import json
import re
from typing import Never

from downforce_core.analytics.circuits import CircuitIdentityResolver
from downforce_core.analytics.storage import AnalyticsDerivedStore, StoredAnalyticsSnapshot
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import RuntimeBlobCache


class BlobAnalyticsDerivedStore(AnalyticsDerivedStore):
    def __init__(self, cache: RuntimeBlobCache) -> None:
        self.cache = cache
        super().__init__(cache.release_root)

    def load(self, source_revision: str) -> StoredAnalyticsSnapshot:
        paths = [".downforce/analytics/manifest.json", ".downforce/analytics/driver-race.parquet"]
        try:
            with self.cache.lease(paths):
                snapshot = super().load(source_revision)
                if snapshot is None:
                    raise StorageIntegrityError(
                        "runtime analytics snapshot is unavailable or invalid"
                    )
                return snapshot
        except (KeyError, OSError, TypeError, ValueError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime analytics snapshot is unavailable or invalid"
            ) from None

    def publish(self, *args: object, **kwargs: object) -> Never:
        raise StorageIntegrityError("runtime product storage is read-only")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class BlobCircuitIdentityResolver(CircuitIdentityResolver):
    """Load a deterministic sorted mapping; never discover or copy raw provider dumps."""

    def __init__(self, cache: RuntimeBlobCache) -> None:
        path = ".downforce/analytics/circuit-identities.json"
        try:
            if cache.manifest.entry(path).size_bytes > 1024 * 1024:
                raise ValueError("mapping exceeds bound")
            with cache.lease([path]) as paths:
                value = json.loads(paths[path].read_text("utf-8"), object_pairs_hook=_strict_object)
            if not isinstance(value, dict) or set(value) != {"schema_version", "identities"}:
                raise ValueError("mapping schema is invalid")
            if type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise ValueError("mapping version is invalid")
            rows = value["identities"]
            if not isinstance(rows, list) or not rows:
                raise ValueError("mapping is empty")
            identities: dict[tuple[int, int], str] = {}
            previous: tuple[int, int] | None = None
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "season",
                    "round_number",
                    "circuit_id",
                }:
                    raise ValueError("mapping entry is invalid")
                season, round_number, circuit_id = (
                    row["season"],
                    row["round_number"],
                    row["circuit_id"],
                )
                if (
                    type(season) is not int
                    or season < 1950
                    or type(round_number) is not int
                    or not 1 <= round_number <= 99
                    or not isinstance(circuit_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", circuit_id) is None
                ):
                    raise ValueError("mapping identity is invalid")
                key = (season, round_number)
                if previous is not None and key <= previous:
                    raise ValueError("mapping must be sorted and unique")
                previous = key
                identities[key] = circuit_id
        except (KeyError, OSError, TypeError, ValueError, RecursionError, StorageIntegrityError):
            raise StorageIntegrityError(
                "runtime circuit identities are unavailable or invalid"
            ) from None
        super().__init__(identities)

    @classmethod
    def from_cache(cls, cache: RuntimeBlobCache) -> BlobCircuitIdentityResolver:
        return cls(cache)

    @classmethod
    def from_store(cls, store: HistoricalArchiveStore) -> Never:
        raise StorageIntegrityError(
            "runtime circuit identities require the compact release mapping"
        )
