"""Authenticated product-only filesystem bundles, with no network fallback.

Logical paths remain in the release index. Only deduplicated content-addressed
objects and the two release metadata files are physically materialized.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.export import RuntimeRelease, _safe_path
from downforce_core.runtime_blob.manifest import (
    ManifestLimits,
    RuntimeManifest,
    validate_relative_path,
)

_CHUNK_BYTES = 64 * 1024
_PIN = re.compile(rb"[0-9a-f]{64}\n\Z")
_LIMITS = ManifestLimits()


def _root(path: Path) -> Path:
    # Preserve lexical spellings until link checks have completed. Resolving first
    # would hide junctions, symlinks and parent traversal from the safety guard.
    if ".." in path.parts:
        raise StorageIntegrityError("bundle root has unsafe lexical ancestry")
    return path.absolute()


def _directory(root: Path, path: Path) -> None:
    _safe_path(root, path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise StorageIntegrityError("bundle directory is not a regular directory")


@contextmanager
def _open_file(root: Path, path: Path, *, create: bool = False) -> Iterator[BinaryIO]:
    """Check both the lexical pathname and the actual opened regular single-link file."""
    try:
        if create:
            _directory(root, path.parent)
        else:
            _safe_path(root, path, file=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageIntegrityError("bundle opened file is unsafe")
            _safe_path(root, path, file=True)
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise StorageIntegrityError("bundle file changed while opening")
            handle = os.fdopen(descriptor, "wb" if create else "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            yield handle
    except OSError:
        raise StorageIntegrityError("bundle file is unsafe, missing or unavailable") from None


def _read_metadata(root: Path, name: str, limit: int) -> bytes:
    with _open_file(root, root / name) as handle:
        if os.fstat(handle.fileno()).st_size > limit:
            raise StorageIntegrityError("bundle metadata exceeds limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := handle.read(min(_CHUNK_BYTES, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise StorageIntegrityError("bundle metadata exceeds limit")
            chunks.append(chunk)
        return b"".join(chunks)


def _verified_file(root: Path, path: Path, size: int, digest: str) -> Iterator[bytes]:
    checksum = hashlib.sha256()
    total = 0
    with _open_file(root, path) as handle:
        if os.fstat(handle.fileno()).st_size != size:
            raise StorageIntegrityError("bundle object size verification failed")
        while chunk := handle.read(_CHUNK_BYTES):
            total += len(chunk)
            if total > size:
                raise StorageIntegrityError("bundle object exceeds indexed size")
            checksum.update(chunk)
            yield chunk
    if total != size or checksum.hexdigest() != digest:
        raise StorageIntegrityError("bundle object checksum or size verification failed")


class BundledReleaseReader:
    """Local immutable ObjectReader; no HTTP, environment credentials or providers."""

    def __init__(self, root: Path, expected_sha256: str | None = None) -> None:
        self.root = _root(root)
        _directory(self.root, self.root)
        pin = _read_metadata(self.root, "release.sha256", 65)
        if not _PIN.fullmatch(pin):
            raise StorageIntegrityError("bundle release pin is malformed")
        pinned_sha256 = pin[:-1].decode("ascii")
        if expected_sha256 is not None and expected_sha256 != pinned_sha256:
            raise StorageIntegrityError("bundle release pin conflicts with expected checksum")
        self.release_body = _read_metadata(self.root, "release.json", _LIMITS.max_manifest_bytes)
        self.manifest = RuntimeManifest.from_bytes(self.release_body, pinned_sha256)
        self.validate_inventory()

    def validate_inventory(self) -> None:
        """Check exact filenames, shape and links without eagerly reading payloads."""
        try:
            _directory(self.root, self.root)
            if {path.name for path in self.root.iterdir()} != {
                "release.json",
                "release.sha256",
                "objects",
            }:
                raise StorageIntegrityError("bundle inventory contains unknown or incomplete files")
            for name in ("release.json", "release.sha256"):
                _safe_path(self.root, self.root / name, file=True)
            objects = self.root / "objects"
            _directory(self.root, objects)
            sizes: dict[str, int] = {}
            for entry in self.manifest.files.values():
                name = entry.sha256 + ".bin"
                if name in sizes and sizes[name] != entry.size_bytes:
                    raise StorageIntegrityError("bundle index has conflicting object sizes")
                sizes[name] = entry.size_bytes
            if {path.name for path in objects.iterdir()} != set(sizes):
                raise StorageIntegrityError("bundle objects contain unknown or incomplete files")
            for name, size in sizes.items():
                path = objects / name
                _safe_path(self.root, path, file=True)
                if path.lstat().st_size != size:
                    raise StorageIntegrityError("bundle indexed object size is invalid")
        except OSError:
            raise StorageIntegrityError("bundle inventory is unavailable") from None

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        if type(max_bytes) is not int or max_bytes < 0:
            raise StorageIntegrityError("bundle read bound is invalid")
        validate_relative_path(relative_path)
        if relative_path == "release.json":
            size, digest = len(self.release_body), self.manifest.sha256
            path = self.root / "release.json"
        else:
            if relative_path not in self.manifest.files:
                raise StorageIntegrityError("bundle path is not indexed")
            entry = self.manifest.entry(relative_path)
            size, digest = entry.size_bytes, entry.sha256
            path = self.root / "objects" / (digest + ".bin")
        if size > max_bytes:
            raise StorageIntegrityError("bundle read exceeds object bound")
        yield from _verified_file(self.root, path, size, digest)


def _source_stream(release: RuntimeRelease, path: str, output: BinaryIO | None) -> None:
    entry = release.manifest.entry(path)
    digest = hashlib.sha256()
    total = 0
    iterator = release.reader().iter_bytes(path, max_bytes=entry.size_bytes)
    try:
        for chunk in iterator:
            if not isinstance(chunk, bytes) or len(chunk) > _CHUNK_BYTES:
                raise StorageIntegrityError("bundle source stream exceeds chunk limit")
            total += len(chunk)
            if total > entry.size_bytes:
                raise StorageIntegrityError("bundle source stream exceeds indexed size")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    if total != entry.size_bytes or digest.hexdigest() != entry.sha256:
        raise StorageIntegrityError("bundle source stream checksum or size verification failed")


def write_bundle(release: RuntimeRelease, output: Path) -> Path:
    """Copy only authenticated indexed streams into a new, fail-closed bundle.

    Existing output is read-only: only complete identical known bundles may be
    reused. Unknown, partial or corrupt output is never repaired or removed.
    The release pin is written last so interrupted publication cannot look valid.
    """
    manifest = RuntimeManifest.from_bytes(release.body, release.sha256)
    if manifest.files != release.manifest.files or manifest.sha256 != release.manifest.sha256:
        raise StorageIntegrityError("bundle source manifest conflicts with authenticated bytes")
    root = _root(output)
    try:
        for ancestor in (*reversed(root.parents), root):
            try:
                ancestor.lstat()
            except FileNotFoundError:
                continue
            _directory(ancestor, ancestor)
        if root.exists():
            existing = BundledReleaseReader(root, expected_sha256=release.sha256)
            if existing.release_body != release.body:
                raise StorageIntegrityError("bundle output differs from known release bytes")
            checked: set[str] = set()
            for name, entry in manifest.files.items():
                if entry.sha256 not in checked:
                    for _ in existing.iter_bytes(name, max_bytes=entry.size_bytes):
                        pass
                    checked.add(entry.sha256)
            for name in manifest.files:
                _source_stream(release, name, None)
            return root
        root.mkdir(parents=True, exist_ok=False)
        _directory(root, root)
        (root / "objects").mkdir(exist_ok=False)
        copied: set[str] = set()
        for name, entry in manifest.files.items():
            if entry.sha256 in copied:
                _source_stream(release, name, None)
            else:
                with _open_file(
                    root, root / "objects" / (entry.sha256 + ".bin"), create=True
                ) as obj:
                    _source_stream(release, name, obj)
                copied.add(entry.sha256)
        with _open_file(root, root / "release.json", create=True) as metadata:
            metadata.write(release.body)
        with _open_file(root, root / "release.sha256", create=True) as pin:
            pin.write((release.sha256 + "\n").encode("ascii"))
        return root
    except OSError:
        raise StorageIntegrityError("bundle output is unsafe or unavailable") from None
