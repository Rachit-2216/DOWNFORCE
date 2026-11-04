"""Private Windows read leases; never a readiness cache or an advisory lock.

CreateFileW GENERIC_READ + FILE_SHARE_READ excludes existing and future writers
and delete/rename handles until CloseHandle. Directory GENERIC_READ uses share
READ|WRITE (not DELETE), preserving child publication but pinning each container.
Attributes and child membership remain separately checked at every caller gate.
This covers normal filesystem access, not privileged raw-volume/kernel writes.
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from downforce_core.detailed.provider_guard import _unlinked_path


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _Information(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("written", _FileTime),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _FileId(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]


def _fingerprint(info: _Information, directory: bool, *, mutable: bool = False) -> tuple[int, ...]:
    identity = (int(info.volume), int(info.index_high), int(info.index_low))
    if directory:
        attributes = int(info.attributes)
        return (*identity, attributes & 0x410 if mutable else attributes)
    return (
        *identity,
        int(info.attributes),
        int(info.links),
        int(info.size_high),
        int(info.size_low),
        int(info.created.high),
        int(info.created.low),
        int(info.written.high),
        int(info.written.low),
    )


def _path_identity(path: Path, *, directory: bool = False) -> tuple[int, ...]:
    info = path.stat(follow_symlinks=False)
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("leased authority path is a reparse point")
    identity = (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    return (*identity, getattr(info, "st_file_attributes", 0)) if directory else identity


class _Windows:
    def __init__(self) -> None:
        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create: Any = kernel.CreateFileW
        self.create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.create.restype = wintypes.HANDLE
        self.information: Any = kernel.GetFileInformationByHandle
        self.information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Information)]
        self.information.restype = wintypes.BOOL
        self.file_id: Any = kernel.GetFileInformationByHandleEx
        self.file_id.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self.file_id.restype = wintypes.BOOL
        self.close: Any = kernel.CloseHandle
        self.close.argtypes = [wintypes.HANDLE]
        self.close.restype = wintypes.BOOL

    def open(self, path: Path, *, directory: bool) -> int:
        _unlinked_path(path)
        text = str(path)
        if not text.startswith("\\\\?\\"):
            text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
        handle: Any = self.create(
            text,
            0x80000000,  # GENERIC_READ; attributes-only access does not pin rename.
            3 if directory else 1,  # SHARE_READ|WRITE directories; SHARE_READ files.
            None,  # Non-inheritable; no security descriptor/privilege changes.
            3,  # OPEN_EXISTING, never create or truncate.
            0x00200000 | (0x02000000 if directory else 0),
            None,  # OPEN_REPARSE_POINT and directory BACKUP_SEMANTICS only.
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def info(self, handle: int) -> _Information:
        value = _Information()
        if not self.information(handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def identity(self, handle: int) -> tuple[int, int]:
        value = _FileId()
        if not self.file_id(handle, 18, ctypes.byref(value), ctypes.sizeof(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value.volume), int.from_bytes(bytes(value.identifier), "little")


@dataclass(frozen=True)
class _Lease:
    handle: int
    path: Path
    expected: tuple[int, ...]
    fingerprint: tuple[int, ...]
    sha256: str | None = None


class _LeaseSet:
    """One operation's complete, fail-closed ownership of kernel read handles."""

    def __init__(
        self,
        root: Path,
        files: Mapping[str, tuple[tuple[int, ...], str]],
        directories: Mapping[str, tuple[int, ...]],
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows read leases are unavailable on this platform")
        self.root = _unlinked_path(root)
        self.api = _Windows()
        self.files: dict[str, _Lease] = {}
        self.directories: dict[str, _Lease] = {}
        self._owned: list[int] = []
        self._identities: set[tuple[int, int]] = set()
        self.closed = False
        try:
            for relative in sorted(directories, key=lambda name: len(Path(name).parts)):
                self.bind_directory(relative, directories[relative])
            for relative, (expected, digest) in files.items():
                self.files[relative] = self._acquire(relative, expected, False, digest)
        except BaseException as exc:
            self.close(exc)
            raise

    def _path(self, relative: str) -> Path:
        token = Path(relative)
        path = (self.root / token).absolute()
        if ".." in token.parts or not path.is_relative_to(self.root):
            raise ValueError("input lease escaped dedicated root")
        return path

    def _acquire(
        self, relative: str, expected: tuple[int, ...], directory: bool, digest: str | None = None
    ) -> _Lease:
        if self.closed:
            raise ValueError("input lease ownership is closed")
        path = self._path(relative)
        handle = self.api.open(path, directory=directory)
        self._owned.append(handle)  # Own immediately, including every verification failure.
        info = self.api.info(handle)
        identity = self.api.identity(handle)
        if identity != expected[:2] or identity in self._identities:
            raise ValueError("input lease handle or path alias differs from captured input")
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise ValueError("input lease must identify a regular unlinked input")
        if not directory and (info.links != 1 or not stat.S_ISREG(expected[2])):
            raise ValueError("input lease must identify a regular unlinked file")
        if _path_identity(path, directory=directory)[: len(expected)] != expected:
            raise ValueError("input lease path changed during acquisition")
        self._identities.add(identity)
        return _Lease(
            handle,
            path,
            expected,
            _fingerprint(info, directory, mutable=directory and len(expected) == 3),
            digest,
        )

    def bind_directory(self, relative: str, expected: tuple[int, ...]) -> None:
        if relative in self.directories:
            raise ValueError("input directory lease is already bound")
        self.directories[relative] = self._acquire(relative, expected, True)

    def complete(self, files: set[str], directories: set[str]) -> None:
        if self.closed or files != set(self.files) or directories != set(self.directories):
            raise ValueError("complete held input lease ownership is required")

    def file(self, relative: str, expected: tuple[int, ...], digest: str) -> None:
        lease = self.files.get(relative)
        if (
            self.closed
            or lease is None
            or lease.expected != expected
            or lease.sha256 != digest
            or _fingerprint(self.api.info(lease.handle), False) != lease.fingerprint
            or _path_identity(lease.path) != expected
        ):
            raise ValueError(f"held authority file identity changed: {relative}")

    def directory(self, relative: str) -> None:
        lease = self.directories.get(relative)
        if (
            self.closed
            or lease is None
            or _fingerprint(self.api.info(lease.handle), True, mutable=len(lease.expected) == 3)
            != lease.fingerprint
            or _path_identity(lease.path, directory=True)[: len(lease.expected)] != lease.expected
        ):
            raise ValueError(f"held authority directory identity changed: {relative}")

    def close(self, primary: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        failures: list[BaseException] = []
        for handle in reversed(self._owned):
            try:
                if not self.api.close(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException as exc:
                failures.append(exc)
        self._owned.clear()
        if failures:
            failure = next(
                (exc for exc in failures if isinstance(exc, (KeyboardInterrupt, SystemExit))),
                failures[0],
            )
            for extra in failures:
                if extra is not failure:
                    failure.add_note(f"additional input lease release failed: {extra!r}")
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                if primary is not None:
                    failure.add_note(f"original operation failure: {primary!r}")
                raise failure from primary
            if primary is not None:
                primary.add_note(f"input lease release failed: {failure!r}")
            else:
                raise failure
