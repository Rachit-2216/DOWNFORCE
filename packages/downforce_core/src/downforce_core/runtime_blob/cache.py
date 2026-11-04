"""Bounded project-shaped materialization with thread/process-safe path leases.

The root lock is held throughout a lease. Nested leases are reentrant across cache
instances in one thread; competing threads/processes wait with a bounded timeout.
Only indexed files in this release can be evicted. Unknown/stale files count.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import stat
import threading
import time
import uuid
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.manifest import BlobFile, RuntimeManifest
from downforce_core.runtime_blob.transport import BlobTransportError, ObjectReader

_MARKER = b"downforce-runtime-cache-v1\n"
_STATES_GUARD = threading.Lock()


@dataclass
class _LockState:
    mutex: object = field(default_factory=threading.RLock)
    depth: int = 0
    descriptor: int | None = None
    leased: dict[Path, int] = field(default_factory=dict)


_STATES: dict[tuple[int, str], _LockState] = {}


def _safe(path: Path) -> None:
    """Check lexical ancestors, Windows reparse points, and regular-file links."""
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StorageIntegrityError("runtime cache path is linked or reparsed")
        if not stat.S_ISDIR(info.st_mode):
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageIntegrityError("runtime cache file is linked or nonregular")


def _open(path: Path, flags: int) -> int:
    _safe(path)
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageIntegrityError("runtime cache opened file is linked or nonregular")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _os_lock(descriptor: int, *, unlock: bool = False) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(descriptor, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class RuntimeBlobCache:
    def __init__(
        self,
        root: Path,
        manifest: RuntimeManifest,
        reader: ObjectReader,
        *,
        max_disk_bytes: int = 512 * 1024 * 1024,
        chunk_bytes: int = 64 * 1024,
        max_cache_files: int = 100_002,
        lock_timeout: float = 10.0,
    ) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (max_disk_bytes, chunk_bytes, max_cache_files)
        ):
            raise StorageIntegrityError("runtime cache limits are invalid")
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise StorageIntegrityError("runtime cache lock timeout is invalid")
        _safe(root.absolute())
        # Check lexical links before resolving Windows 8.3 aliases. All spellings
        # of one physical root must share paths and the reentrant lock state.
        self.root = root.resolve(strict=False)
        _safe(self.root)
        self.manifest = manifest
        self.reader = reader
        self.max_disk_bytes = max_disk_bytes
        self.chunk_bytes = chunk_bytes
        self.max_cache_files = max_cache_files
        self.lock_timeout = lock_timeout
        self.release_root = self.root / "releases" / manifest.sha256
        with _STATES_GUARD:
            self._state = _STATES.setdefault(
                (os.getpid(), os.path.normcase(str(self.root))), _LockState()
            )
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked(initialize=True):
            self._inventory()

    def _preflight_bootstrap(self) -> None:
        """Only an owned root or our exact bootstrap lock may precede initialization."""
        marker = self.root / ".runtime-cache-owner"
        if not marker.exists():
            entries = tuple(self.root.iterdir())
            lock = self.root / ".runtime-cache-lock"
            if any(path != lock for path in entries):
                raise StorageIntegrityError("runtime cache root is populated and not owned")
            if lock.exists():
                _safe(lock)
                if lock.stat().st_size > 1:
                    raise StorageIntegrityError("runtime cache bootstrap lock is not owned")

    def _initialize_marker(self) -> None:
        # Called only with BOTH the process-wide mutex and filesystem lock held.
        self._preflight_bootstrap()
        marker = self.root / ".runtime-cache-owner"
        if not marker.exists():
            descriptor = self._state.descriptor
            if descriptor is None:
                raise StorageIntegrityError("runtime cache bootstrap lock is unavailable")
            # Read through the locked handle: Windows denies byte-range reads via a
            # second descriptor, even to our own process. Never inspect content
            # before the OS lock, when another initializer may own that byte.
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, 2) != b"0":
                raise StorageIntegrityError("runtime cache bootstrap lock is not owned")
            with os.fdopen(_open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as handle:
                handle.write(_MARKER)
                handle.flush()
                os.fsync(handle.fileno())

    def _check_marker(self) -> None:
        marker = self.root / ".runtime-cache-owner"
        _safe(marker)
        if marker.stat().st_size != len(_MARKER) or marker.read_bytes() != _MARKER:
            raise StorageIntegrityError("runtime cache ownership marker is invalid")

    @contextmanager
    def _locked(self, *, initialize: bool = False) -> Iterator[None]:
        # RLock's public type is a factory; keep this narrow cast at the synchronization boundary.
        from typing import cast

        with _STATES_GUARD:
            self._state = _STATES.setdefault(
                (os.getpid(), os.path.normcase(str(self.root))), _LockState()
            )
        mutex = cast(threading._RLock, self._state.mutex)
        deadline = time.monotonic() + self.lock_timeout
        if not mutex.acquire(timeout=self.lock_timeout):
            raise StorageIntegrityError("runtime cache lease lock timed out")
        try:
            if self._state.depth == 0:
                if initialize:
                    self._preflight_bootstrap()
                descriptor = _open(self.root / ".runtime-cache-lock", os.O_RDWR | os.O_CREAT)
                locked = False
                transferred = False
                try:
                    while True:
                        try:
                            _os_lock(descriptor)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise StorageIntegrityError(
                                    "runtime cache process lock timed out"
                                ) from None
                            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                    # Windows CRT supports byte locks beyond EOF. Initialize the
                    # bootstrap byte ONLY after ownership of that byte is acquired.
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"0")
                    self._state.descriptor = descriptor
                    transferred = True
                finally:
                    if not transferred:
                        try:
                            if locked:
                                _os_lock(descriptor, unlock=True)
                        finally:
                            os.close(descriptor)
            self._state.depth += 1
            try:
                _safe(self.root)
                if initialize:
                    self._initialize_marker()
                self._check_marker()
                yield
            finally:
                self._state.depth -= 1
                if self._state.depth == 0 and self._state.descriptor is not None:
                    descriptor = self._state.descriptor
                    self._state.descriptor = None
                    try:
                        _os_lock(descriptor, unlock=True)
                    finally:
                        os.close(descriptor)
        finally:
            mutex.release()

    def _inventory(self, *, reserve_parents: Iterable[Path] = ()) -> dict[Path, int]:
        files: dict[Path, int] = {}
        directories_seen: set[Path] = set()
        # Validate lexical root ancestors once. Each tree parent and immediate
        # entry is then checked without repeating every already-checked ancestor.
        # This inventory is transaction-local, never reused by another lease.
        _safe(self.root)

        def traversal_failed(error: OSError) -> None:
            raise StorageIntegrityError("runtime cache inventory traversal failed") from None

        for directory, directories, filenames in os.walk(
            self.root, followlinks=False, onerror=traversal_failed
        ):
            parent = Path(directory)
            parent_info = parent.lstat()
            if stat.S_ISLNK(parent_info.st_mode) or (
                getattr(parent_info, "st_file_attributes", 0) & 0x400
            ):
                raise StorageIntegrityError("runtime cache path is linked or reparsed")
            if not stat.S_ISDIR(parent_info.st_mode):
                raise StorageIntegrityError("runtime cache tree parent is not a directory")
            directories_seen.add(parent)
            if len(directories_seen) > self.max_cache_files * 16:
                raise StorageIntegrityError("runtime cache directory count exceeds limit")
            for name in (*directories, *filenames):
                path = parent / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise StorageIntegrityError("runtime cache path is linked or reparsed")
                if not stat.S_ISDIR(info.st_mode):
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise StorageIntegrityError("runtime cache file is linked or nonregular")
                    files[path] = info.st_size
                    if len(files) > self.max_cache_files:
                        raise StorageIntegrityError("runtime cache file count exceeds limit")
        # Reserve a UNION of existing and requested parents before any filesystem
        # mutation. Shared ancestors count once, including across nested leases.
        for parent in reserve_parents:
            if not parent.is_relative_to(self.root):
                raise StorageIntegrityError("runtime cache directory reservation escapes root")
            cursor = parent
            while cursor != self.root:
                directories_seen.add(cursor)
                if len(directories_seen) > self.max_cache_files * 16:
                    raise StorageIntegrityError("runtime cache directory capacity is insufficient")
                cursor = cursor.parent
        return files

    def _verified(self, path: Path, entry: BlobFile) -> bool:
        _safe(path)
        if not path.exists():
            return False
        if not path.is_file():
            raise StorageIntegrityError("runtime cache object is not a regular file")
        with os.fdopen(_open(path, os.O_RDONLY), "rb") as handle:
            if os.fstat(handle.fileno()).st_size != entry.size_bytes:
                return False
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(self.chunk_bytes), b""):
                digest.update(chunk)
        return digest.hexdigest() == entry.sha256

    def _reserve(
        self, needed: int, protected: set[Path], *, new_files: int, files: dict[Path, int]
    ) -> None:
        total = sum(files.values())
        if total + needed <= self.max_disk_bytes and len(files) + new_files <= self.max_cache_files:
            return
        indexed = {self.release_root / path for path in self.manifest.files}
        candidates = [
            path
            for path in files
            if path in indexed and path not in protected and path not in self._state.leased
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime_ns)
        while total + needed > self.max_disk_bytes or len(files) + new_files > self.max_cache_files:
            if not candidates:
                raise StorageIntegrityError("runtime cache capacity cannot satisfy live lease")
            target = candidates.pop(0)
            _safe(target)
            total -= files.pop(target)
            target.unlink()

    def _chunks(self, entry: BlobFile) -> Generator[bytes, None, None]:
        iterator: Iterator[bytes] | None = None
        failure: BlobTransportError | None = None
        try:
            iterator = self.reader.iter_bytes(entry.path, max_bytes=entry.size_bytes)
            yield from iterator
        except Exception as exc:
            failure = BlobTransportError(
                "runtime cache object reader failed",
                exception_type=exc.exception_type
                if isinstance(exc, BlobTransportError)
                else type(exc),
            )
        finally:
            if iterator is not None:
                close = getattr(iterator, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as exc:
                        if failure is None:
                            failure = BlobTransportError(
                                "runtime cache object reader close failed",
                                exception_type=(
                                    exc.exception_type
                                    if isinstance(exc, BlobTransportError)
                                    else type(exc)
                                ),
                            )
        if failure is not None:
            raise failure from None

    def _materialize(self, entry: BlobFile) -> Path:
        target = self.release_root / entry.path
        _safe(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root / (uuid.uuid4().hex + ".part")
        created = False
        try:
            digest = hashlib.sha256()
            size = 0
            descriptor = _open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                iterator = self._chunks(entry)
                try:
                    for chunk in iterator:
                        if not isinstance(chunk, bytes) or len(chunk) > self.chunk_bytes:
                            raise StorageIntegrityError("runtime cache stream chunk exceeds limit")
                        size += len(chunk)
                        if size > entry.size_bytes:
                            raise StorageIntegrityError("runtime cache object exceeds byte limit")
                        digest.update(chunk)
                        handle.write(chunk)
                finally:
                    iterator.close()
                if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                    raise StorageIntegrityError("runtime cache object checksum or byte size failed")
                handle.flush()
                os.fsync(handle.fileno())
            _safe(target)
            _safe(temporary)
            os.replace(temporary, target)
            return target
        finally:
            if created and temporary.exists():
                _safe(temporary)
                temporary.unlink()

    @contextmanager
    def lease(self, paths: Iterable[str]) -> Iterator[Mapping[str, Path]]:
        selected = tuple(dict.fromkeys(paths))
        entries = [self.manifest.entry(path) for path in selected]
        with self._locked():
            # An impossible directory allocation must fail before corrupt-file
            # recovery, known-file eviction, mkdir, download, or publication.
            files = self._inventory(
                reserve_parents=((self.release_root / entry.path).parent for entry in entries)
            )
            missing: list[BlobFile] = []
            protected = {self.release_root / entry.path for entry in entries}
            for entry in entries:
                target = self.release_root / entry.path
                if not self._verified(target, entry):
                    if target in self._state.leased:
                        raise StorageIntegrityError("runtime cache leased object lost integrity")
                    if target.exists():
                        _safe(target)
                        target.unlink()
                        files.pop(target)
                    missing.append(entry)
            # Atomic replace is same-filesystem, so staged bytes replace rather than copy.
            # Reserve the entire request before starting any streamed download.
            self._reserve(
                sum(entry.size_bytes for entry in missing),
                protected,
                new_files=len(missing),
                files=files,
            )
            failure: BlobTransportError | None = None
            try:
                for entry in missing:
                    self._materialize(entry)
            except BlobTransportError as exc:
                failure = exc
            except StorageIntegrityError:
                raise
            except Exception as exc:
                failure = BlobTransportError(
                    "runtime cache materialization failed", exception_type=type(exc)
                )
            if failure is not None:
                raise failure from None
            result = {entry.path: self.release_root / entry.path for entry in entries}
            for target in result.values():
                self._state.leased[target] = self._state.leased.get(target, 0) + 1
            try:
                yield MappingProxyType(result)
            finally:
                for target in result.values():
                    self._state.leased[target] -= 1
                    if self._state.leased[target] == 0:
                        del self._state.leased[target]

    @contextmanager
    def lease_prefix(
        self, prefix: str, *, suffix: str | None = None
    ) -> Iterator[Mapping[str, Path]]:
        with self.lease(self.manifest.paths(prefix, suffix=suffix)) as paths:
            yield paths
