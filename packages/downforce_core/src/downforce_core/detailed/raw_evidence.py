"""Lossless immutable source archives with bounded, validated restoration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from downforce_core.detailed.status import (
    archive_peak_bytes,
    copy_peak_bytes,
    guard_operation,
    owned_temporary_directory,
)
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256

RAW_VERSION = "raw-evidence-zip-v1"
MAX_RAW_BYTES = 8 * 1024**3
MAX_FILES = 20_000


def _relative(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or str(path) != name
        or any(
            part.endswith((".", " "))
            or any(ord(c) < 32 for c in part)
            or any(c in '<>"|?*' for c in part)
            or part.split(".")[0].casefold()
            in {
                "con",
                "prn",
                "aux",
                "nul",
                *(f"com{n}" for n in range(1, 10)),
                *(f"lpt{n}" for n in range(1, 10)),
            }
            for part in path.parts
        )
    ):
        raise StorageIntegrityError("unsafe raw evidence member")
    return name


def source_manifest(source: Path) -> dict[str, Any]:
    source = source.absolute()
    if not source.is_dir() or source.is_symlink() or source.is_junction():
        raise StorageIntegrityError("invalid source evidence root")
    files = []
    directories = []
    for path in sorted(source.rglob("*")):
        ensure_contained(path, source)
        if path.is_junction():
            raise StorageIntegrityError("junction in source evidence")
        stat = path.stat()
        record = {
            "path": _relative(path.relative_to(source).as_posix()),
            "mtime_ns": stat.st_mtime_ns,
            "mode": stat.st_mode & 0o777,
        }
        if path.is_file():
            files.append({**record, "bytes": stat.st_size, "sha256": file_sha256(path)})
        elif path.is_dir():
            directories.append(record)
        else:
            raise StorageIntegrityError("unsupported source evidence entry")
    return {"files": files, "directories": directories}


class RawEvidenceStore:
    def __init__(self, root: Path) -> None:
        root = root.absolute()
        cursor = root
        while True:
            if cursor.is_symlink() or cursor.is_junction():
                raise StorageIntegrityError("raw store cannot use linked roots")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        self.root = root

    def _path(self, identity: str) -> Path:
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise StorageIntegrityError("invalid raw bundle identity")
        return ensure_contained(self.root / f"{identity}.zip", self.root)

    def pack(
        self,
        source: Path,
        *,
        session_id: str,
        provider: str,
        provider_version: str,
        level: int = 6,
    ) -> str:
        validate_safe_identifier(session_id)
        validate_safe_identifier(provider)
        if not 0 <= level <= 9:
            raise ValueError("invalid compression level")
        snapshot = source_manifest(source)
        if (
            len(snapshot["files"]) > MAX_FILES
            or sum(f["bytes"] for f in snapshot["files"]) > MAX_RAW_BYTES
        ):
            raise StorageIntegrityError("source exceeds evidence pack limits")
        manifest = {
            "schema_version": RAW_VERSION,
            "session_id": session_id,
            "provider": provider,
            "provider_version": provider_version,
            **snapshot,
        }
        guard_operation(
            self.root,
            next_peak_bytes=archive_peak_bytes(
                snapshot, metadata_bytes=len(json.dumps(manifest, sort_keys=True).encode("utf-8"))
            ),
            operation="raw evidence pack",
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with owned_temporary_directory(prefix=".packing-", directory=self.root) as temporary:
            path = Path(temporary) / "bundle.zip"
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
            ) as bundle:
                for record in snapshot["files"]:
                    name = record["path"]
                    info = zipfile.ZipInfo("source/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info._compresslevel = level  # type: ignore[attr-defined]
                    with bundle.open(info, "w", force_zip64=True) as output:
                        with (source / name).open("rb") as original:
                            shutil.copyfileobj(original, output, length=1024**2)
                header = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
                header.compress_type = zipfile.ZIP_DEFLATED
                header._compresslevel = level  # type: ignore[attr-defined]
                bundle.writestr(header, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
            self._validate_path(path)
            if source_manifest(source) != snapshot:
                raise StorageIntegrityError("source changed during archival")
            identity = file_sha256(path)
            final = self.root / f"{identity}.zip"
            if final.exists():
                self.validate(identity)
            else:
                os.rename(path, final)
            return identity

    @staticmethod
    def _validate_path(path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as bundle:
                names = bundle.namelist()
                if len(names) != len(set(names)) or len(names) > MAX_FILES + 1:
                    raise StorageIntegrityError("duplicate or excessive evidence members")
                header = bundle.getinfo("manifest.json")
                if header.file_size > 8 * 1024**2:
                    raise StorageIntegrityError("oversized evidence manifest")
                manifest: dict[str, Any] = json.loads(bundle.read(header))
                if manifest.get("schema_version") != RAW_VERSION:
                    raise StorageIntegrityError("unsupported raw evidence schema")
                validate_safe_identifier(manifest["session_id"])
                validate_safe_identifier(manifest["provider"])
                expected = {"manifest.json"}
                total = 0
                for record in manifest["files"]:
                    name = "source/" + _relative(record["path"])
                    if name in expected:
                        raise StorageIntegrityError("duplicate evidence manifest path")
                    expected.add(name)
                    info = bundle.getinfo(name)
                    total += info.file_size
                    if total > MAX_RAW_BYTES or info.file_size != record["bytes"]:
                        raise StorageIntegrityError("invalid raw evidence size")
                    digest = hashlib.sha256()
                    with bundle.open(info) as handle:
                        while chunk := handle.read(1024**2):
                            digest.update(chunk)
                    if digest.hexdigest() != record["sha256"]:
                        raise StorageIntegrityError("raw evidence file checksum mismatch")
                if set(names) != expected:
                    raise StorageIntegrityError("unmanifested raw evidence members")
                for directory in manifest["directories"]:
                    _relative(directory["path"])
                return manifest
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            raise StorageIntegrityError("raw evidence is corrupt or incompatible") from exc

    def validate(self, identity: str) -> dict[str, Any]:
        path = self._path(identity)
        if file_sha256(path) != identity:
            raise StorageIntegrityError("raw bundle checksum mismatch")
        return self._validate_path(path)

    def restore(self, identity: str, destination: Path) -> dict[str, Any]:
        manifest = self.validate(identity)
        destination = destination.absolute()
        if destination.exists():
            raise FileExistsError("restore destination already exists")
        # A new sibling directory allows atomic publication without merging user files.
        parent = destination.parent
        guard_operation(
            destination,
            next_peak_bytes=copy_peak_bytes(manifest),
            operation="raw evidence restoration",
        )
        parent.mkdir(parents=True, exist_ok=True)
        ensure_contained(destination, parent, must_exist=False)
        with owned_temporary_directory(prefix=".restoring-", directory=parent) as temporary:
            staging = Path(temporary)
            with zipfile.ZipFile(self._path(identity)) as bundle:
                for directory in manifest["directories"]:
                    (staging / directory["path"]).mkdir(parents=True, exist_ok=True)
                for record in manifest["files"]:
                    output = staging / record["path"]
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        bundle.open("source/" + record["path"]) as source,
                        output.open("xb") as handle,
                    ):
                        shutil.copyfileobj(source, handle, length=1024**2)
                    os.chmod(output, record["mode"])
                    os.utime(output, ns=(record["mtime_ns"], record["mtime_ns"]))
            for directory in sorted(manifest["directories"], key=lambda d: d["path"], reverse=True):
                output = staging / directory["path"]
                os.chmod(output, directory["mode"])
                os.utime(output, ns=(directory["mtime_ns"], directory["mtime_ns"]))
            expected = {key: manifest[key] for key in ("files", "directories")}
            if source_manifest(staging) != expected:
                raise StorageIntegrityError("restored evidence differs from original")
            os.rename(staging, destination)
        return manifest
