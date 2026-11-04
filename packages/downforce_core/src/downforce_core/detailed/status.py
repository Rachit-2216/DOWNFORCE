"""Detailed filesystem capacity, read-only status, and owned-lifetime utilities."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.exceptions import StorageIntegrityError

HEADROOM_BYTES = 50 * 1024**3
FILE_OVERHEAD_BYTES = 4096


class CapacityStop(ValueError):
    """Invalid estimates or insufficient preferred headroom stop the entire worker."""


@contextmanager
def owned_temporary_directory(*, prefix: str, directory: Path) -> Iterator[Path]:
    """Clean this owned tree once without replacing an active primary failure.

    Cleanup-only errors still stop the caller. Cleanup cancellation is never
    suppressed; ordinary secondary cleanup errors remain notes on the primary.
    """
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=directory)
    primary: BaseException | None = None
    try:
        yield Path(temporary.name)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            temporary.cleanup()
        except Exception as cleanup_error:
            if primary is None:
                raise
            primary.add_note(
                f"owned temporary cleanup failed: path={temporary.name}; "
                f"secondary_error={cleanup_error!r}; "
                f"winerror={getattr(cleanup_error, 'winerror', None)}; "
                f"filename={getattr(cleanup_error, 'filename', None)}"
            )


def _bytes(value: int) -> int:
    if type(value) is not int or value < 0:
        raise CapacityStop("capacity estimate requires nonnegative integer byte counts")
    return value


def filesystem_ancestor(path: Path) -> Path:
    """Find an existing directory, checking every component without creating it."""
    cursor = path.absolute()
    existing = None
    while True:
        if cursor.is_symlink() or cursor.is_junction():
            raise StorageIntegrityError("filesystem operation must not use linked paths")
        if existing is None and cursor.is_dir():
            existing = cursor
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if existing is None:
        raise CapacityStop("filesystem operation has no existing filesystem ancestor")
    return existing


def guard_operation(
    path: Path,
    *,
    next_peak_bytes: int,
    operation: str,
    free_bytes: Callable[[Path], int] | None = None,
) -> None:
    """Read current capacity before writes/load; no automatic 20–50 GiB fallback."""
    _bytes(next_peak_bytes)
    existing = filesystem_ancestor(path)
    available = free_bytes(existing) if free_bytes is not None else shutil.disk_usage(existing).free
    try:
        guard_free_space(
            free_bytes=available, next_peak_bytes=next_peak_bytes, reserve_bytes=HEADROOM_BYTES
        )
    except ValueError as exc:
        raise CapacityStop(
            f"{operation} requires 50 GiB headroom after conservative next-operation peak; "
            "less headroom requires an explicit user choice"
        ) from exc


def copy_peak_bytes(snapshot: dict[str, Any]) -> int:
    files, directories = snapshot["files"], snapshot["directories"]
    if not isinstance(files, list) or not isinstance(directories, list):
        raise CapacityStop("invalid source snapshot for capacity estimate")
    return sum(_bytes(record["bytes"]) for record in files) + FILE_OVERHEAD_BYTES * (
        len(files) + len(directories) + 1
    )


def archive_peak_bytes(snapshot: dict[str, Any], *, metadata_bytes: int) -> int:
    """Conservative DEFLATE growth + ZIP64/names/manifest and filesystem overhead.

    12.5% payload growth is deliberately above normal DEFLATE worst-case growth.
    This is an operational estimate, not a guarantee for arbitrary serializers.
    """
    payload = sum(_bytes(record["bytes"]) for record in snapshot["files"])
    names = sum(len(record["path"].encode("utf-8")) for record in snapshot["files"])
    return (
        copy_peak_bytes(snapshot)
        + (payload + 7) // 8
        + 8 * names
        + 2 * _bytes(metadata_bytes)
        + 1024**2
    )


def _array_estimate(array: Any) -> tuple[int, int, int]:
    """Expanded payload, value slots, leaf count; dictionaries are not size proxies."""
    if pa.types.is_dictionary(array.type):
        return _array_estimate(array.dictionary_decode())
    if isinstance(array.type, pa.ExtensionType):
        return _array_estimate(array.storage)
    children = []
    if pa.types.is_struct(array.type):
        children = [array.field(index) for index in range(array.type.num_fields)]
    elif pa.types.is_map(array.type):
        children = [array.keys, array.items]
    elif (
        pa.types.is_list(array.type)
        or pa.types.is_large_list(array.type)
        or pa.types.is_fixed_size_list(array.type)
    ):
        children = [array.flatten()]
    elif pa.types.is_union(array.type):
        raise CapacityStop("unsupported union capacity estimate")
    if children:
        stats = [_array_estimate(child) for child in children]
        return (
            array.nbytes + sum(x[0] for x in stats),
            len(array) + sum(x[1] for x in stats),
            sum(x[2] for x in stats),
        )
    return array.nbytes, len(array), 1


def parquet_peak_bytes(table: Any, *, source_bytes: int = 0, row_group_size: int = 8192) -> int:
    """Conservative Arrow-derived encoding/codec/statistics/footer budget.

    Eight expanded payloads and 128 bytes per value cover encoding/codec growth
    and statistics; 64 KiB per leaf/group and 8 schema copies cover metadata.
    Known source bytes remain a floor. nbytes alone is never an upper-bound claim.
    """
    _bytes(source_bytes)
    if type(row_group_size) is not int or row_group_size <= 0:
        raise CapacityStop("invalid Parquet row-group estimate")
    payload = slots = leaves = 0
    for column in table.columns:
        column_leaves = 1
        for chunk in column.chunks:
            data, values, chunk_leaves = _array_estimate(chunk)
            payload += data
            slots += values
            column_leaves = max(column_leaves, chunk_leaves)
        leaves += column_leaves
    groups = max(1, (table.num_rows + row_group_size - 1) // row_group_size)
    estimate = (
        8 * payload
        + 128 * slots
        + 8 * table.schema.serialize().size
        + 65536 * max(1, leaves) * (groups + 1)
        + 1024**2
    )
    return max(_bytes(estimate), source_bytes + FILE_OVERHEAD_BYTES)


def revision_peak_bytes(
    table_peaks: Iterable[int], *, metadata_bytes: int, schema_bytes: int
) -> int:
    peaks = [_bytes(value) for value in table_peaks]
    return (
        sum(peaks)
        + 4 * _bytes(metadata_bytes)
        + 8 * _bytes(schema_bytes)
        + 65536 * (len(peaks) + 2)
    )


def provider_peak_bytes(snapshot: dict[str, Any], *, native_peak_bytes: int) -> int:
    """Offline cache load budget: cache rewrites plus known native serialization."""
    return max(2 * 1024**3, 4 * copy_peak_bytes(snapshot) + _bytes(native_peak_bytes))


def tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_symlink() or root.is_junction():
        raise StorageIntegrityError("linked data path in storage measurement")
    if root.is_file():
        return root.stat().st_size
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise StorageIntegrityError("linked data path in storage measurement")
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except FileNotFoundError:
                continue  # a concurrently retired temporary file is not persistent usage
    return total


def guard_free_space(*, free_bytes: int, next_peak_bytes: int, reserve_bytes: int) -> None:
    for value in (free_bytes, next_peak_bytes, reserve_bytes):
        if type(value) is not int or value < 0:
            raise ValueError("space guard requires nonnegative integer byte counts")
    if free_bytes - next_peak_bytes < reserve_bytes:
        raise ValueError("insufficient free space for next operation plus reserve")


def _filesystem(existing: Path) -> str:
    if os.name != "nt":
        return "unknown (not reported by disk_usage)"
    import ctypes
    from ctypes import wintypes

    volume = ctypes.create_unicode_buffer(1024)
    filesystem = ctypes.create_unicode_buffer(256)
    kernel = importlib.import_module("ctypes").windll.kernel32
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    volume_path.restype = wintypes.BOOL
    if not volume_path(str(existing), volume, len(volume)):
        return "unknown (volume path unavailable)"
    information = kernel.GetVolumeInformationW
    information.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    information.restype = wintypes.BOOL
    if not information(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
        return "unknown (filesystem information unavailable)"
    return filesystem.value


def storage_status(paths: DetailedPaths) -> dict[str, Any]:
    existing = filesystem_ancestor(paths.root)
    usage = shutil.disk_usage(existing)
    used_bytes = tree_bytes(paths.root)  # Reject linked child directories before walking caches.
    preflight_caches = sum(
        tree_bytes(directory) for directory in (paths.root / "preflight").glob("*/*/raw-cache")
    )
    discovery_cache = tree_bytes(paths.root / "discovery-cache")
    reconciliation_cache = tree_bytes(paths.root / "reconciliation-cache")
    projection_path = paths.root / "stratified-projection.json"
    projection = None
    current = None
    error = None
    if projection_path.exists():
        try:
            projection = json.loads(projection_path.read_text())
            # Imports remain local to avoid cycles; these boundaries only read
            # existing artifacts and never invoke publication or provider loading.
            from downforce_core.detailed.proofs import validated_study_measurements
            from downforce_core.detailed.study_io import project_measured_study

            samples = validated_study_measurements(paths)
            current = project_measured_study(paths.root, samples, free_bytes=usage.free)
            if not isinstance(projection, dict) or json.dumps(
                projection.get("measurements"), sort_keys=True, allow_nan=False
            ) != json.dumps(samples, sort_keys=True, allow_nan=False):
                current = None
                error = "historical projection lacks exact current artifact bindings"
        except (OSError, ValueError, KeyError, TypeError, StorageIntegrityError) as exc:
            current = None
            error = str(exc)
    return {
        "configured_root": str(paths.root),
        "filesystem_anchor": paths.root.anchor,
        "filesystem": _filesystem(existing),
        "free_bytes": usage.free,
        "volume_total_bytes": usage.total,
        "used_detailed_bytes": used_bytes,
        "persistent_raw_bytes": tree_bytes(paths.raw),
        "canonical_bytes": tree_bytes(paths.canonical),
        "cache_bytes": tree_bytes(paths.cache)
        + preflight_caches
        + discovery_cache
        + reconciliation_cache,
        "preflight_provider_cache_bytes": preflight_caches,
        "discovery_cache_bytes": discovery_cache,
        "reconciliation_cache_bytes": reconciliation_cache,
        "preflight_bytes": tree_bytes(paths.root / "preflight"),
        "study_bytes": tree_bytes(paths.root / "study"),
        "temporary_bytes": tree_bytes(paths.staging),
        "components_are_additive": False,
        "component_note": "Preflight/study are whole subtrees; cache components overlap preflight",
        "projection": projection,
        "headroom_bytes": 50 * 1024**3,
        "reserve_bytes": 20 * 1024**3,
        "projection_current": current is not None,
        "current_projection": current,
        "current_safety": "verified" if current is not None else "stale-or-unverified",
        "current_safety_error": error,
        "safe_to_sync": bool(current and current.get("safe_to_sync")),
    }
