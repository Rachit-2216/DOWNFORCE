"""Owned-cache integrity, bounded storage and lease safety without network IO."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import threading
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
import requests
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.manifest import RuntimeManifest
from downforce_core.runtime_blob.transport import BlobTransportError

FIRST = ".downforce/normalized/race/active.json"
SECOND = ".downforce/normalized/other/active.json"


class Reader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[str] = []

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        self.calls.append(relative_path)
        body = self.objects[relative_path]
        for offset in range(0, len(body), 2):
            yield body[offset : offset + 2]


def manifest(objects: dict[str, bytes]) -> RuntimeManifest:
    body = json.dumps(
        {
            "schema_version": 1,
            "files": {
                path: {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in objects.items()
            },
        }
    ).encode()
    return RuntimeManifest.from_bytes(body, hashlib.sha256(body).hexdigest())


def cache(
    tmp_path: Path,
    objects: dict[str, bytes],
    *,
    max_disk_bytes: int = 512 * 1024 * 1024,
    chunk_bytes: int = 64 * 1024,
) -> RuntimeBlobCache:
    return RuntimeBlobCache(
        tmp_path / "runtime",
        manifest(objects),
        Reader(objects),
        max_disk_bytes=max_disk_bytes,
        chunk_bytes=chunk_bytes,
    )


def test_cache_hydrates_verified_project_paths_and_idempotent_hits(tmp_path: Path) -> None:
    objects = {FIRST: b"abc", SECOND: b"def"}
    reader = Reader(objects)
    store = RuntimeBlobCache(tmp_path / "runtime", manifest(objects), reader)
    for _ in range(2):
        with store.lease([FIRST, FIRST]) as paths:
            assert paths[FIRST] == store.release_root / FIRST
            assert paths[FIRST].read_bytes() == b"abc"
    assert reader.calls == [FIRST]
    with store.lease_prefix(".downforce/normalized", suffix=".json") as paths:
        assert tuple(paths) == (SECOND, FIRST)


@pytest.mark.parametrize("body", [b"bad", b"ab", b"abcd"])
def test_cache_rejects_wrong_hash_truncated_and_oversized_stream(
    tmp_path: Path, body: bytes
) -> None:
    store = RuntimeBlobCache(tmp_path / "runtime", manifest({FIRST: b"abc"}), Reader({FIRST: body}))
    with pytest.raises(StorageIntegrityError):
        with store.lease([FIRST]):
            pass
    assert not (store.release_root / FIRST).exists()
    assert not list(store.root.glob("*.part"))


def test_cache_repairs_only_corrupt_owned_regular_file(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    with store.lease([FIRST]) as paths:
        target = paths[FIRST]
    target.write_bytes(b"BAD")
    with store.lease([FIRST]) as paths:
        assert paths[FIRST].read_bytes() == b"abc"


def test_cache_rejects_corrupted_live_outer_lease(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    with store.lease([FIRST]) as paths:
        paths[FIRST].write_bytes(b"BAD")
        with pytest.raises(StorageIntegrityError, match="leased"):
            with store.lease([FIRST]):
                pass
        assert paths[FIRST].read_bytes() == b"BAD"


def test_nested_same_and_disjoint_leases_keep_outer_paths_live(tmp_path: Path) -> None:
    objects = {FIRST: b"abc", SECOND: b"def"}
    store = cache(tmp_path, objects)
    # A second instance must use the same process-wide reentrant lock/lease state.
    second = RuntimeBlobCache(store.root, store.manifest, Reader(objects))
    with store.lease([FIRST]) as outer:
        with second.lease([FIRST, SECOND]) as inner:
            assert inner[FIRST] == outer[FIRST]
            assert outer[FIRST].read_bytes() == b"abc"
        assert outer[FIRST].exists()


def test_cache_capacity_keeps_live_outer_path_and_fails_nested_oversize(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"a" * 100, SECOND: b"b" * 100}, max_disk_bytes=180)
    with store.lease([FIRST]) as paths:
        with pytest.raises(StorageIntegrityError, match="capacity"):
            with store.lease([SECOND]):
                pass
        assert paths[FIRST].read_bytes() == b"a" * 100
    with store.lease([SECOND]) as paths:
        assert paths[SECOND].read_bytes() == b"b" * 100
    assert not (store.release_root / FIRST).exists()


def test_cache_counts_unknown_files_without_deleting_them(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"}, max_disk_bytes=100)
    unknown = store.root / "unknown.bin"
    unknown.write_bytes(b"x" * 100)
    with pytest.raises(StorageIntegrityError, match="capacity"):
        with store.lease([FIRST]):
            pass
    assert unknown.read_bytes() == b"x" * 100


def test_cache_rejects_populated_unowned_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    original = root / "original.json"
    original.write_bytes(b"do not alter")
    with pytest.raises(StorageIntegrityError, match="owned"):
        RuntimeBlobCache(root, manifest({FIRST: b"abc"}), Reader({FIRST: b"abc"}))
    assert original.read_bytes() == b"do not alter"


def test_cache_rejects_hardlinked_owned_file_without_deleting_original(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    with store.lease([FIRST]) as paths:
        target = paths[FIRST]
    original = tmp_path / "original.json"
    target.rename(original)
    os.link(original, target)
    with pytest.raises(StorageIntegrityError, match="linked"):
        with store.lease([FIRST]):
            pass
    assert original.read_bytes() == b"abc"
    assert target.exists()


def test_cache_rejects_symlink_or_junction_in_root(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    outside = tmp_path / "original"
    outside.mkdir()
    link = store.root / "linked-dir"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable to this Windows user")
    with pytest.raises(StorageIntegrityError, match="linked"):
        with store.lease([FIRST]):
            pass
    assert list(outside.iterdir()) == []


def test_concurrent_threads_cannot_evict_a_live_lease(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"a" * 100, SECOND: b"b" * 100}, max_disk_bytes=180)
    started, acquired = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def competing_reader() -> None:
        started.set()
        try:
            with store.lease([SECOND]):
                acquired.set()
        except BaseException as exc:
            errors.append(exc)

    with store.lease([FIRST]) as paths:
        worker = threading.Thread(target=competing_reader)
        worker.start()
        assert started.wait(2)
        assert not acquired.wait(0.05)
        assert paths[FIRST].exists()
    worker.join(3)
    assert not worker.is_alive()
    assert acquired.is_set()
    assert not errors


def _competing_process(root: str, result: multiprocessing.Queue[str]) -> None:
    try:
        store = RuntimeBlobCache(
            Path(root), manifest({FIRST: b"abc"}), Reader({FIRST: b"abc"}), lock_timeout=0.1
        )
        with store.lease([FIRST]):
            result.put("acquired")
    except StorageIntegrityError:
        result.put("locked")


def test_concurrent_process_cannot_enter_live_lease(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    with store.lease([FIRST]) as paths:
        worker = context.Process(target=_competing_process, args=(str(store.root), result))
        worker.start()
        assert result.get(timeout=10) == "locked"
        assert paths[FIRST].exists()
        worker.join(10)
    assert worker.exitcode == 0
    result.close()


def test_cache_rejects_missing_entry_before_fetch(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    with pytest.raises(KeyError):
        with store.lease([SECOND]):
            pass


def test_cache_rejects_large_injected_chunks(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"}, chunk_bytes=1)
    with pytest.raises(StorageIntegrityError, match="chunk"):
        with store.lease([FIRST]):
            pass


def test_cache_stream_failure_is_safe_and_cleans_partial_file(tmp_path: Path) -> None:
    class FailingReader:
        def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
            yield b"a"
            raise StorageIntegrityError("credential-secret at sensitive-url")

    store = RuntimeBlobCache(tmp_path / "runtime", manifest({FIRST: b"abc"}), FailingReader())
    with pytest.raises(StorageIntegrityError) as captured:
        with store.lease([FIRST]):
            pass
    assert "credential-secret" not in str(captured.value)
    assert captured.value.__context__ is None
    assert not list(store.root.glob("*.part"))


def test_cache_normalizes_lexical_root_spellings_for_reentrant_instances(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    alias = store.root / ".." / "runtime"
    with store.lease([FIRST]):
        second = RuntimeBlobCache(alias, store.manifest, Reader({FIRST: b"abc"}), lock_timeout=0.1)
        with second.lease([FIRST]) as paths:
            assert paths[FIRST] == store.release_root / FIRST


def test_cache_file_count_limit_counts_unknown_files(tmp_path: Path) -> None:
    store = RuntimeBlobCache(
        tmp_path / "runtime", manifest({FIRST: b"abc"}), Reader({FIRST: b"abc"}), max_cache_files=3
    )
    unknown = store.root / "unknown.json"
    unknown.write_bytes(b"x")
    with pytest.raises(StorageIntegrityError, match="capacity"):
        with store.lease([FIRST]):
            pass
    assert unknown.exists()


def test_cache_does_not_adopt_tampered_ownership_marker(tmp_path: Path) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    marker = store.root / ".runtime-cache-owner"
    marker.write_bytes(b"fake")
    with pytest.raises(StorageIntegrityError, match="ownership"):
        with store.lease([FIRST]):
            pass


def test_cache_rejects_windows_junction_without_touching_target(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction-specific check")
    import subprocess

    store = cache(tmp_path, {FIRST: b"abc"})
    outside = tmp_path / "original"
    outside.mkdir()
    link = store.root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
    )
    try:
        with pytest.raises(StorageIntegrityError, match="linked"):
            with store.lease([FIRST]):
                pass
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(link)
    assert outside.is_dir()


def test_concurrent_initializers_wait_for_marker_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import downforce_core.runtime_blob.cache as cache_module

    created, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []
    original_open = cache_module._open
    root = tmp_path / "runtime"

    def paused_open(path: Path, flags: int) -> int:
        descriptor = original_open(path, flags)
        if path.name == ".runtime-cache-owner" and flags & os.O_CREAT:
            created.set()
            assert release.wait(5)
        return descriptor

    monkeypatch.setattr(cache_module, "_open", paused_open)

    def initialize(*, second: bool) -> None:
        try:
            RuntimeBlobCache(root, manifest({FIRST: b"abc"}), Reader({FIRST: b"abc"}))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if second:
                finished.set()

    first = threading.Thread(target=initialize, kwargs={"second": False})
    second = threading.Thread(target=initialize, kwargs={"second": True})
    first.start()
    assert created.wait(3)
    second.start()
    try:
        finished_early = finished.wait(0.1)
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert not finished_early
    assert not first.is_alive() and not second.is_alive()
    assert not errors


@pytest.mark.parametrize("diagnostic", [requests.Timeout, requests.ConnectionError])
def test_cache_preserves_transport_diagnostic_type_without_injected_secret(
    tmp_path: Path,
    diagnostic: type[Exception],
) -> None:
    class FailingReader:
        def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
            raise BlobTransportError("secret-token at sensitive-url", exception_type=diagnostic)
            yield b""

    store = RuntimeBlobCache(tmp_path / "runtime", manifest({FIRST: b"abc"}), FailingReader())
    with pytest.raises(BlobTransportError) as captured:
        with store.lease([FIRST]):
            pass
    assert captured.value.exception_type is diagnostic
    assert "secret-token" not in str(captured.value)
    assert captured.value.__context__ is None


def test_cache_never_deletes_preexisting_unknown_temporary_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    identifier = uuid.UUID("00000000-0000-0000-0000-000000000001")
    unknown = store.root / (identifier.hex + ".part")
    unknown.write_bytes(b"unknown bytes must survive")
    monkeypatch.setattr(uuid, "uuid4", lambda: identifier)
    with pytest.raises(BlobTransportError):
        with store.lease([FIRST]):
            pass
    assert unknown.read_bytes() == b"unknown bytes must survive"


def test_windows_byte_lock_beyond_eof_allows_locked_initialization(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows CRT byte-range lock behavior")
    import downforce_core.runtime_blob.cache as cache_module

    lock = tmp_path / "empty-lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    locked = False
    try:
        assert os.fstat(descriptor).st_size == 0
        cache_module._os_lock(descriptor)
        locked = True
        assert os.write(descriptor, b"0") == 1
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.read(descriptor, 1) == b"0"
    finally:
        if locked:
            cache_module._os_lock(descriptor, unlock=True)
        os.close(descriptor)


def test_bootstrap_byte_write_occurs_only_after_os_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import downforce_core.runtime_blob.cache as cache_module

    acquired = False
    original_lock, original_write = cache_module._os_lock, os.write

    def checked_lock(descriptor: int, *, unlock: bool = False) -> None:
        nonlocal acquired
        original_lock(descriptor, unlock=unlock)
        acquired = not unlock

    def checked_write(descriptor: int, body: bytes) -> int:
        assert acquired, "bootstrap byte was written before process lock"
        return original_write(descriptor, body)

    monkeypatch.setattr(cache_module, "_os_lock", checked_lock)
    monkeypatch.setattr(os, "write", checked_write)
    cache(tmp_path, {FIRST: b"abc"})


@pytest.mark.parametrize("failure_at", ["write", "open_stat", "locked_stat"])
def test_bootstrap_failures_close_opened_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    descriptors: list[int] = []
    original_open, original_stat = os.open, os.fstat
    stat_calls = 0

    def tracked_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if Path(path).name == ".runtime-cache-lock":
            descriptors.append(descriptor)
        return descriptor

    def failing_stat(descriptor: int) -> os.stat_result:
        nonlocal stat_calls
        if descriptor in descriptors:
            stat_calls += 1
            if (
                failure_at == "open_stat"
                and stat_calls == 1
                or failure_at == "locked_stat"
                and stat_calls == 2
            ):
                raise OSError("injected stat failure")
        return original_stat(descriptor)

    def failing_write(descriptor: int, body: bytes) -> int:
        raise PermissionError("injected competing byte-lock owner")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fstat", failing_stat)
    if failure_at == "write":
        monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        cache(tmp_path, {FIRST: b"abc"})
    assert len(descriptors) == 1
    try:
        with pytest.raises(OSError):
            original_stat(descriptors[0])
    finally:
        # Clean up leaked test descriptors on the RED implementation only.
        try:
            os.close(descriptors[0])
        except OSError:
            pass


def test_first_lease_rejects_parent_directory_overflow_before_materializing(tmp_path: Path) -> None:
    deep = ".downforce/normalized/" + "a/" * 48 + "active.json"
    objects = {deep: b"abc"}
    reader = Reader(objects)
    store = RuntimeBlobCache(tmp_path / "runtime", manifest(objects), reader, max_cache_files=3)
    with pytest.raises(StorageIntegrityError, match="directory"):
        with store.lease([deep]):
            pass
    assert not store.release_root.exists()
    assert reader.calls == []


def test_accumulated_parent_directories_fail_before_evicting_valid_cache_hit(
    tmp_path: Path,
) -> None:
    objects = {f".downforce/normalized/s{index}/a/b/active.json": b"abc" for index in range(15)}
    reader = Reader(objects)
    store = RuntimeBlobCache(tmp_path / "runtime", manifest(objects), reader, max_cache_files=3)
    for index in range(14):
        with store.lease([f".downforce/normalized/s{index}/a/b/active.json"]):
            pass
    previous = store.release_root / ".downforce/normalized/s13/a/b/active.json"
    assert previous.read_bytes() == b"abc"
    calls_before = tuple(reader.calls)
    with pytest.raises(StorageIntegrityError, match="directory"):
        with store.lease([".downforce/normalized/s14/a/b/active.json"]):
            pass
    assert previous.read_bytes() == b"abc"
    assert not (store.release_root / ".downforce/normalized/s14").exists()
    assert tuple(reader.calls) == calls_before
    with store.lease([".downforce/normalized/s13/a/b/active.json"]) as paths:
        assert paths[".downforce/normalized/s13/a/b/active.json"] == previous


def test_directory_reservation_counts_shared_parent_union_once(tmp_path: Path) -> None:
    shared = ".downforce/normalized/" + "a/" * 58
    objects = {shared + "first.json": b"abc", shared + "second.json": b"def"}
    store = RuntimeBlobCache(
        tmp_path / "runtime", manifest(objects), Reader(objects), max_cache_files=4
    )
    with store.lease(objects) as paths:
        assert len(paths) == 2
        assert all(path.read_bytes() == objects[name] for name, path in paths.items())


def test_nested_directory_reservation_preserves_outer_lease_on_overflow(tmp_path: Path) -> None:
    deep = ".downforce/normalized/" + "a/" * 48 + "active.json"
    objects = {FIRST: b"abc", deep: b"def"}
    store = RuntimeBlobCache(
        tmp_path / "runtime", manifest(objects), Reader(objects), max_cache_files=4
    )
    # Existing unknown empty directories are counted, not removed to make room.
    unknown = store.root / "unknown"
    for index in range(10):
        (unknown / str(index)).mkdir(parents=True)
    with store.lease([FIRST]) as outer:
        with pytest.raises(StorageIntegrityError, match="directory"):
            with store.lease([deep]):
                pass
        assert outer[FIRST].read_bytes() == b"abc"
    assert all((unknown / str(index)).is_dir() for index in range(10))


def windows_short_path(path: Path) -> Path:
    """Return an actual Windows 8.3 spelling, never a simulated alias."""
    if os.name != "nt":
        pytest.skip("Windows short-path identity regression")
    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short_path(str(path), buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("test Windows short-path lookup failed")
    short = Path(buffer.value)
    if short == path.resolve():
        pytest.skip("test filesystem does not provide a distinct 8.3 path")
    assert short.resolve() == path.resolve()
    return short


def test_windows_short_and_long_cache_aliases_share_canonical_state(tmp_path: Path) -> None:
    short_root = windows_short_path(tmp_path) / "runtime"
    long_root = tmp_path.resolve() / "runtime"
    objects = {FIRST: b"abc", SECOND: b"def"}
    index = manifest(objects)
    short_cache = RuntimeBlobCache(short_root, index, Reader(objects))
    long_cache = RuntimeBlobCache(long_root, index, Reader(objects))
    assert short_cache.root == long_cache.root
    assert short_cache.release_root == long_cache.release_root
    assert short_cache._state is long_cache._state
    with short_cache.lease([FIRST]) as outer:
        with long_cache.lease([SECOND]) as inner:
            assert outer[FIRST].read_bytes() == b"abc"
            assert inner[SECOND].read_bytes() == b"def"
        assert outer[FIRST].exists()


def test_each_lease_takes_one_fresh_complete_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = cache(tmp_path, {FIRST: b"abc", SECOND: b"def"})
    inventories: list[dict[Path, int]] = []
    original = RuntimeBlobCache._inventory

    def tracked(self: RuntimeBlobCache, *, reserve_parents: Iterable[Path] = ()) -> dict[Path, int]:
        result = original(self, reserve_parents=reserve_parents)
        inventories.append(result.copy())
        return result

    monkeypatch.setattr(RuntimeBlobCache, "_inventory", tracked)
    with store.lease([FIRST]):
        unknown = store.root / "unknown.bin"
        unknown.write_bytes(b"outside bookkeeping")
        with store.lease([SECOND]):
            pass
    with store.lease([FIRST]):
        pass
    assert len(inventories) == 3
    assert unknown not in inventories[0]
    assert inventories[1][unknown] == inventories[2][unknown] == 19


def test_fitting_lease_does_not_construct_unrelated_eviction_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = {f".downforce/normalized/s{index}/active.json": b"abc" for index in range(120)}
    store = cache(tmp_path, objects)
    with store.lease(objects):
        pass
    enumeration_calls = 0
    original = RuntimeManifest.paths
    original_stat = Path.stat
    selected = next(iter(objects))
    unrelated = {store.release_root / name for name in objects if name != selected}
    unrelated_stat_calls = 0

    def tracked(
        self: RuntimeManifest, prefix: str = "", *, suffix: str | None = None
    ) -> tuple[str, ...]:
        nonlocal enumeration_calls
        enumeration_calls += 1
        return original(self, prefix, suffix=suffix)

    def tracked_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal unrelated_stat_calls
        if follow_symlinks and self in unrelated:
            unrelated_stat_calls += 1
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(RuntimeManifest, "paths", tracked)
    monkeypatch.setattr(Path, "stat", tracked_stat)
    with store.lease([selected]) as paths:
        assert next(iter(paths.values())).read_bytes() == b"abc"
    assert enumeration_calls == 0
    assert unrelated_stat_calls == 0


def test_inventory_ancestor_validation_work_is_linear_in_physical_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = cache(tmp_path, {FIRST: b"abc"})
    deep = store.root.joinpath(*[f"level{index}" for index in range(18)])
    deep.mkdir(parents=True)
    for index in range(80):
        (deep / f"unknown{index}.bin").write_bytes(b"x")
    lstat_calls = 0
    original = Path.lstat

    def tracked(self: Path) -> os.stat_result:
        nonlocal lstat_calls
        lstat_calls += 1
        return original(self)

    monkeypatch.setattr(Path, "lstat", tracked)
    files = store._inventory()
    assert len(files) == 82
    physical_entries = 82 + 19  # Files, directory descendants, and root.
    assert lstat_calls <= 2 * physical_entries + len(store.root.parents)


def test_corrupt_replacement_at_full_budget_updates_inventory_accounting(tmp_path: Path) -> None:
    objects = {FIRST: b"abc", SECOND: b"def"}
    reader = Reader(objects)
    store = RuntimeBlobCache(
        tmp_path / "runtime", manifest(objects), reader, max_disk_bytes=34, max_cache_files=4
    )
    with store.lease(objects) as paths:
        target = paths[FIRST]
        preserved = paths[SECOND]
    target.write_bytes(b"BAD")
    with store.lease([FIRST]) as paths:
        assert paths[FIRST].read_bytes() == b"abc"
        assert preserved.read_bytes() == b"def"
    assert reader.calls == [FIRST, SECOND, FIRST]


@pytest.mark.parametrize("budget", ["bytes", "files", "directories"])
@pytest.mark.parametrize("nested", [False, True])
def test_next_inventory_detects_changed_unknown_budget_before_download(
    tmp_path: Path, budget: str, nested: bool
) -> None:
    objects = {FIRST: b"abc", SECOND: b"def"}
    reader = Reader(objects)
    store = RuntimeBlobCache(
        tmp_path / "runtime",
        manifest(objects),
        reader,
        max_disk_bytes=100,
        max_cache_files=4,
    )

    def introduce_and_reject() -> None:
        if budget == "bytes":
            (store.root / "unknown.bin").write_bytes(b"x" * 100)
        elif budget == "files":
            (store.root / "unknown.bin").write_bytes(b"x")
            (store.root / "another-unknown.bin").write_bytes(b"x")
        else:
            for index in range(64):
                (store.root / f"unknown{index}").mkdir()
        calls_before = reader.calls.copy()
        with pytest.raises(StorageIntegrityError, match="capacity|directory|file count"):
            with store.lease([SECOND] if nested else [FIRST]):
                pass
        assert reader.calls == calls_before
        assert (store.release_root / FIRST).read_bytes() == b"abc"

    with store.lease([FIRST]):
        if nested:
            introduce_and_reject()
    if not nested:
        introduce_and_reject()


def test_nested_inventory_rejects_new_unknown_hardlink_without_touching_original(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path, {FIRST: b"abc", SECOND: b"def"})
    original = tmp_path / "original.bin"
    original.write_bytes(b"untouched")
    with store.lease([FIRST]) as outer:
        os.link(original, store.root / "unknown.bin")
        with pytest.raises(StorageIntegrityError, match="linked"):
            with store.lease([SECOND]):
                pass
        assert outer[FIRST].read_bytes() == b"abc"
    assert original.read_bytes() == b"untouched"


def test_inventory_traversal_denial_fails_before_any_cache_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_path = ".downforce/normalized/new/deep/active.json"
    objects = {FIRST: b"abc", SECOND: b"def", new_path: b"ghi"}
    reader = Reader(objects)
    store = RuntimeBlobCache(tmp_path / "runtime", manifest(objects), reader, max_disk_bytes=34)
    with store.lease([FIRST, SECOND]) as paths:
        corrupt = paths[FIRST]
        eviction_candidate = paths[SECOND]
    corrupt.write_bytes(b"BAD")
    denied = store.root / "denied-subtree"
    denied.mkdir()
    (denied / "unknown.bin").write_bytes(b"preserve unknown bytes")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in store.root.rglob("*")
        if path.is_file()
    }
    directories_before = {path for path in store.root.rglob("*") if path.is_dir()}
    calls_before = reader.calls.copy()
    scanned_denied = False
    original_scandir = os.scandir

    def denied_scandir(path: str | Path) -> Iterator[os.DirEntry[str]]:
        nonlocal scanned_denied
        if Path(path) == denied:
            scanned_denied = True
            raise PermissionError("secret-token at sensitive-local-path")
        return original_scandir(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", denied_scandir)
        with pytest.raises(StorageIntegrityError, match="inventory traversal failed") as captured:
            with store.lease([FIRST, new_path]):
                pytest.fail("lease entered despite incomplete inventory")
    assert scanned_denied
    assert "secret-token" not in str(captured.value)
    assert "sensitive-local-path" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__
    assert reader.calls == calls_before
    assert corrupt.read_bytes() == b"BAD"
    assert eviction_candidate.read_bytes() == b"def"
    assert not (store.release_root / new_path).parent.exists()
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in store.root.rglob("*")
        if path.is_file()
    } == before
    assert {path for path in store.root.rglob("*") if path.is_dir()} == directories_before
