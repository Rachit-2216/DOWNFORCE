"""Freeze preflight sources and benchmark reversible compression on isolated copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import threading
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import fastf1  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from downforce_core.detailed.measurement import measure_candidate
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.status import (
    archive_peak_bytes,
    copy_peak_bytes,
    guard_operation,
    owned_temporary_directory,
    parquet_peak_bytes,
    provider_peak_bytes,
)
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256

TOOL_VERSION = "detailed-storage-study-v2"


def peak_process_memory() -> int | None:
    if __import__("os").name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = wintypes.HANDLE
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    if not query(process(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PeakWorkingSetSize)


def directory_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


class WorkingPeak:
    def __init__(self, working: Path, bundles: Path) -> None:
        self.working = working
        self.bundles = bundles
        self.baseline = directory_bytes(bundles)
        self.peak = 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.thread.start()

    def observe(self) -> None:
        while not self.done.wait(0.02):
            self.peak = max(
                self.peak,
                directory_bytes(self.working)
                + max(0, directory_bytes(self.bundles) - self.baseline),
            )

    def stop(self) -> int:
        self.done.set()
        self.thread.join()
        return self.peak


def save(path: Path, value: Any) -> None:
    text = json.dumps(value, indent=2, allow_nan=False) + "\n"
    guard_operation(
        path,
        next_peak_bytes=2 * len(text.encode("utf-8")) + 4096,
        operation="storage benchmark report",
    )
    write_text_atomic(path, text)


def checked_source(root: Path, snapshot: dict[str, Any]) -> Path:
    source: Path = ensure_contained(root / snapshot["cache_path"], root)
    if source_manifest(source) != snapshot["source_manifest"]:
        raise ValueError("authoritative benchmark source changed")
    return source


def native_peak(root: Path, snapshot: dict[str, Any]) -> int:
    directory = checked_source(root, snapshot).parent
    peaks = 0
    for name, descriptor in snapshot["measurement"]["tables"].items():
        path = directory / f"{name}.parquet"
        if file_sha256(path) != descriptor["sha256"] or path.stat().st_size != descriptor["bytes"]:
            raise ValueError("authoritative native benchmark table changed")
        with pq.ParquetFile(path) as reader:
            table = reader.read()
        peaks += parquet_peak_bytes(table, source_bytes=path.stat().st_size)
    return peaks


def allocated_bytes(path: Path) -> int | None:
    if __import__("os").name != "nt":
        stat = path.stat()
        return getattr(stat, "st_blocks", 0) * 512 or None
    import ctypes
    from ctypes import wintypes

    high = wintypes.DWORD()
    function = ctypes.windll.kernel32.GetCompressedFileSizeW
    function.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.DWORD
    low = function(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        return None
    return int((high.value << 32) | low)


def freeze(root: Path) -> list[dict[str, Any]]:
    snapshots = []
    for path in sorted((root / "preflight").glob("*/measurement.json")):
        measurement = json.loads(path.read_text())
        attempts = sorted(path.parent.glob("*/measurement.json"))
        matching = [p for p in attempts if json.loads(p.read_text()) == measurement]
        if len(matching) != 1:
            raise ValueError(f"ambiguous measured sample: {path.parent.name}")
        directory = matching[0].parent
        cache = directory / "raw-cache"
        source = source_manifest(cache)
        identity = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        classes: Counter[str] = Counter()
        for record in source["files"]:
            category = (
                "provider-derived-cache"
                if record["path"].endswith(".ff1pkl")
                else "unknown-preserve"
            )
            if "sqlite" in record["path"]:
                category = "source-http-evidence"
            classes[category] += record["bytes"]
        record = {
            "session_id": measurement["session"]["session_id"],
            "source_identity": identity,
            "source_manifest": source,
            "cache_path": cache.relative_to(root).as_posix(),
            "measurement": measurement,
            "classification_bytes": dict(classes),
            "logical_bytes": sum(f["bytes"] for f in source["files"]),
            "allocated_bytes": sum(
                allocated_bytes(p) or p.stat().st_size for p in cache.rglob("*") if p.is_file()
            ),
            "measurement_timestamp": directory.name,
            "frozen_at": datetime.now(UTC).isoformat(),
            "tool_version": TOOL_VERSION,
        }
        destination = root / "study" / "frozen" / f"{path.parent.name}.json"
        if destination.exists():
            original = json.loads(destination.read_text())
            if (
                original["source_identity"] != identity
                or original["measurement"] != measurement
                or original["source_manifest"] != source
                or original["cache_path"] != record["cache_path"]
                or type(original["logical_bytes"]) is not int
                or original["logical_bytes"] != record["logical_bytes"]
            ):
                raise ValueError("authoritative sample changed since freeze")
            record = original
        else:
            save(destination, record)
        snapshots.append(record)
    counts: Counter[str] = Counter()
    sizes: dict[str, int] = {}
    for snapshot in snapshots:
        for file in snapshot["source_manifest"]["files"]:
            counts[file["sha256"]] += 1
            sizes[file["sha256"]] = file["bytes"]
    duplicate_bytes = sum((count - 1) * sizes[key] for key, count in counts.items())
    save(
        root / "study" / "deduplication.json",
        {
            "files": sum(counts.values()),
            "unique_blobs": len(counts),
            "expanded_bytes": sum(s["logical_bytes"] for s in snapshots),
            "duplicate_bytes": duplicate_bytes,
            "method": "exact whole-file SHA256 identity; no semantic equivalence",
        },
    )
    print(f"Frozen {len(snapshots)} samples; exact duplicates {duplicate_bytes} bytes", flush=True)
    return snapshots


def benchmark(root: Path, *, levels: tuple[int, ...] = (1, 6, 9), metrics: bool = False) -> None:
    snapshots = freeze(root)
    for snapshot in snapshots:
        identity = snapshot["session_id"]
        for level in levels:
            suffix = "-metrics-v2" if metrics else ""
            report_path = (
                root / "study" / "compression" / f"{identity}-deflate-{level}{suffix}.json"
            )
            if report_path.exists():
                continue
            source = checked_source(root, snapshot)
            peak = copy_peak_bytes(snapshot["source_manifest"]) + archive_peak_bytes(
                snapshot["source_manifest"],
                metadata_bytes=len(json.dumps(snapshot).encode("utf-8")),
            )
            guard_operation(
                root / "study", next_peak_bytes=peak, operation="benchmark copy and pack"
            )
            with owned_temporary_directory(
                prefix="compression-", directory=root / "study"
            ) as temporary:
                working = Path(temporary)
                copy = working / "copied-source"
                shutil.copytree(source, copy, copy_function=shutil.copy2)
                store = RawEvidenceStore(root / "study" / "raw-bundles")
                monitor = WorkingPeak(working, store.root)
                primary: BaseException | None = None
                try:
                    start = perf_counter()
                    packed = store.pack(
                        copy,
                        session_id=identity,
                        provider="fastf1",
                        provider_version=snapshot["measurement"]["fastf1_version"],
                        level=level,
                    )
                    pack_seconds = perf_counter() - start
                    # Only this temporary copied tree is removed; source remains authoritative.
                    if not copy.resolve().is_relative_to(working.resolve()):
                        raise ValueError("benchmark cleanup target escaped its working directory")
                    shutil.rmtree(copy)
                    start = perf_counter()
                    restored = working / "restored"
                    store.restore(packed, restored)
                    restore_seconds = perf_counter() - start
                    exact = source_manifest(restored) == snapshot["source_manifest"]
                    if not exact:
                        raise ValueError("raw archive round-trip failed")
                except BaseException as exc:
                    primary = exc
                    raise
                finally:
                    try:
                        observed_peak = monitor.stop()
                    except Exception as stop_error:
                        if primary is None:
                            raise
                        primary.add_note(
                            f"benchmark working-peak monitor stop failed: path={working}; "
                            f"secondary_error={stop_error!r}; "
                            f"winerror={getattr(stop_error, 'winerror', None)}; "
                            f"filename={getattr(stop_error, 'filename', None)}"
                        )
                compressed_bytes = (store.root / f"{packed}.zip").stat().st_size
                report = {
                    "session_id": identity,
                    "level": level,
                    "codec": "ZIP-DEFLATE",
                    "original_bytes": snapshot["logical_bytes"],
                    "compressed_bytes": compressed_bytes,
                    "ratio": compressed_bytes / snapshot["logical_bytes"],
                    "pack_seconds_including_validation": pack_seconds,
                    "restore_seconds_including_validation": restore_seconds,
                    "peak_temporary_bytes_upper_bound": snapshot["logical_bytes"]
                    + compressed_bytes,
                    "round_trip_exact": exact,
                    "observed_peak_temporary_bytes": observed_peak,
                    "sampling_interval_ms": 20,
                    "process_peak_rss_cumulative_bytes": peak_process_memory(),
                    "bundle_sha256": packed,
                    "tool_version": TOOL_VERSION,
                }
                save(report_path, report)
                print(
                    f"{identity} deflate-{level}: {report['ratio']:.3f}, "
                    f"pack {pack_seconds:.2f}s, restore {restore_seconds:.2f}s",
                    flush=True,
                )


def parquet(root: Path) -> None:
    snapshots = freeze(root)
    # Every observed value, row, column and Arrow dtype is compared after each rewrite.
    for snapshot in snapshots:
        directory = (root / snapshot["cache_path"]).parent
        identity = snapshot["session_id"]
        for level, groups, dictionary in [
            (1, 8192, True),
            (6, 16384, True),
            (9, 32768, True),
            (6, 16384, False),
        ]:
            tag = f"zstd-{level}-rg-{groups}-dict-{int(dictionary)}"
            path = root / "study" / "parquet" / f"{identity}-{tag}.json"
            if path.exists():
                continue
            before = after = rows = 0
            start = perf_counter()
            checked_source(root, snapshot)
            guard_operation(
                root / "study",
                next_peak_bytes=native_peak(root, snapshot),
                operation="Parquet rewrite staging",
            )
            with owned_temporary_directory(
                prefix="parquet-", directory=root / "study"
            ) as temporary:
                output = Path(temporary) / "copy.parquet"
                for original in sorted(directory.rglob("*.parquet")):
                    with pq.ParquetFile(original) as reader:
                        table = reader.read()
                    guard_operation(
                        output,
                        next_peak_bytes=parquet_peak_bytes(
                            table, source_bytes=original.stat().st_size, row_group_size=groups
                        ),
                        operation="benchmark Parquet rewrite",
                    )
                    pq.write_table(
                        table,
                        output,
                        compression="zstd",
                        compression_level=level,
                        row_group_size=groups,
                        use_dictionary=dictionary,
                    )
                    with pq.ParquetFile(output) as reader:
                        restored = reader.read()
                    if not restored.equals(table, check_metadata=True):
                        raise ValueError("Parquet rewrite changed values/schema/metadata")
                    before += original.stat().st_size
                    after += output.stat().st_size
                    rows += table.num_rows
            report = {
                "session_id": identity,
                "config": tag,
                "before_bytes": before,
                "after_bytes": after,
                "rows": rows,
                "semantic_identity": True,
                "seconds_including_validation": perf_counter() - start,
            }
            save(path, report)
            print(f"{identity} {tag}: {before} -> {after}", flush=True)


def candidates(root: Path) -> None:
    for snapshot in freeze(root):
        identity = snapshot["session_id"]
        report = root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json"
        if not report.exists():
            raise ValueError("run the current metrics benchmark before candidate measurement")
        result = measure_candidate(
            Path.cwd(), root, snapshot["measurement"], json.loads(report.read_text())
        )
        print(
            f"{identity}: canonical {result['canonical_bytes']}, "
            f"metadata {result['metadata_bytes']}",
            flush=True,
        )


def provider_restore(root: Path) -> None:
    """Prove that restored provider-style caches can load without network access."""
    for snapshot in freeze(root):
        identity = snapshot["session_id"]
        destination = root / "study" / "provider-restore" / f"{identity}.json"
        if destination.exists():
            continue
        compression = json.loads(
            (root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json").read_text()
        )
        store = RawEvidenceStore(root / "study" / "raw-bundles")
        measured = snapshot["measurement"]
        checked_source(root, snapshot)
        manifest = store.validate(compression["bundle_sha256"])
        if {key: manifest[key] for key in ("files", "directories")} != snapshot["source_manifest"]:
            raise ValueError("offline restoration bundle differs from authoritative source")
        peak = provider_peak_bytes(
            snapshot["source_manifest"], native_peak_bytes=native_peak(root, snapshot)
        )
        guard_operation(
            root / "study", next_peak_bytes=peak, operation="offline provider restoration"
        )
        with ExitStack() as stack:
            temporary = stack.enter_context(
                owned_temporary_directory(prefix="provider-restore-", directory=root / "study")
            )
            restored = Path(temporary) / "cache"
            store.restore(compression["bundle_sha256"], restored)
            guard_operation(restored, next_peak_bytes=peak, operation="offline provider cache load")
            fastf1.Cache.enable_cache(str(restored))
            # Close this copied cache's SQLite connections before Windows removes the tree.
            if fastf1.Cache._requests_session_cached is not None:
                stack.callback(fastf1.Cache._requests_session_cached.close)
            fastf1.Cache.offline_mode(True)
            entry = measured["session"]
            start = perf_counter()
            session = fastf1.get_session(
                entry["season"], entry["round_number"], entry["fastf1_slot"]
            )
            session.load(laps=True, telemetry=True, weather=True, messages=True)
            source_directory = (root / snapshot["cache_path"]).parent
            checked = []
            for name in measured["tables"]:
                if name == "corners":
                    continue  # optional third-party corner metadata is a separate source
                if "/" in name:
                    group, driver = name.split("/", 1)
                    native = getattr(session, group)[driver]
                else:
                    native = getattr(session, name)
                frame = pd.DataFrame(native).copy()
                frame.insert(0, "source_index", frame.index)
                table = pa.Table.from_pandas(frame, preserve_index=False)
                with pq.ParquetFile(source_directory / f"{name}.parquet") as reader:
                    original = reader.read()
                if not original.equals(table, check_metadata=True):
                    raise ValueError(f"offline restored provider table differs: {identity}/{name}")
                checked.append(name)
            report = {
                "session_id": identity,
                "offline": True,
                "tables_checked": checked,
                "native_tables_exact": True,
                "load_seconds": perf_counter() - start,
                "bundle_sha256": compression["bundle_sha256"],
            }
            save(destination, report)
            print(
                f"Offline provider restore {identity}: {len(checked)} exact native tables",
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("freeze", "compress", "parquet", "metrics", "candidates", "restore-provider"),
    )
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = DetailedPaths(Path.cwd(), override=args.root).root
    if args.action == "metrics":
        benchmark(root, levels=(6,), metrics=True)
    elif args.action == "freeze":
        freeze(root)
    elif args.action == "compress":
        benchmark(root)
    elif args.action == "candidates":
        candidates(root)
    elif args.action == "restore-provider":
        provider_restore(root)
    else:
        parquet(root)
