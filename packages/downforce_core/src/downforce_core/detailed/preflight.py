"""Measured, conservative storage guard for detailed-corpus synchronization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from math import ceil
from typing import Any


def _nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


@dataclass(frozen=True)
class StorageSample:
    season: int
    session_type: str
    raw_bytes: int
    canonical_bytes: Mapping[str, int]

    def __post_init__(self) -> None:
        _nonnegative_integer(self.raw_bytes, "raw bytes")
        for name, size in self.canonical_bytes.items():
            _nonnegative_integer(size, name)

    @property
    def total_bytes(self) -> int:
        return self.raw_bytes + sum(self.canonical_bytes.values())


@dataclass(frozen=True)
class StorageProjection:
    projected_bytes: int
    free_bytes: int
    reserve_bytes: int
    largest_source: str
    missing_years: list[int]
    safe_to_sync: bool
    method: str = "maximum measured session bytes x candidates x 1.25; no cache-credit"


def measurement_sample(measured: Mapping[str, Any]) -> StorageSample:
    """Absent optional markers don't invalidate a storage measurement of real traces."""
    if measured.get("completion_confirmed") is not True:
        raise ValueError("sample completion is not confirmed")
    failures = measured.get("failures", [])
    if any(f["channel"] != "corners" for f in failures):
        raise ValueError("incomplete storage measurement")
    sizes = measured["canonical_sample_bytes"]
    if not sizes.get("laps") or not sizes.get("car_data"):
        raise ValueError("incomplete lap/car storage measurement")
    return StorageSample(
        measured["session"]["season"],
        measured["session"]["session_type"],
        measured["raw_bytes"],
        sizes,
    )


def project_storage(
    samples: Sequence[StorageSample],
    *,
    candidate_sessions: int,
    free_bytes: int,
    reserve_bytes: int = 20 * 1024**3,
    required_years: Set[int] = frozenset(),
) -> StorageProjection:
    """Upper sample envelope, 25% uncertainty allowance, plus a separate disk reserve.

    This is a projection, not a guarantee. Runtime ingestion must also guard free space.
    Unknown/cancelled past candidates are included until positively reconciled.
    """
    if not samples or max(s.total_bytes for s in samples) == 0:
        raise ValueError("nonempty measured storage samples are required")
    for value, label in (
        (candidate_sessions, "candidate sessions"),
        (free_bytes, "free bytes"),
        (reserve_bytes, "reserve bytes"),
    ):
        _nonnegative_integer(value, label)
    missing = sorted(set(required_years) - {s.season for s in samples})
    projected = ceil(max(s.total_bytes for s in samples) * candidate_sessions * 1.25)
    channel_sizes: dict[str, int] = {}
    for sample in samples:
        channel_sizes["raw_cache"] = channel_sizes.get("raw_cache", 0) + sample.raw_bytes
        for channel, size in sample.canonical_bytes.items():
            channel_sizes[channel] = channel_sizes.get(channel, 0) + size
    largest = max(channel_sizes, key=lambda name: channel_sizes[name])
    return StorageProjection(
        projected,
        free_bytes,
        reserve_bytes,
        largest,
        missing,
        not missing and projected + reserve_bytes <= free_bytes,
    )
