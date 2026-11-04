"""Authenticate a bounded immutable release index without trusting URLs or paths."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from downforce_core.exceptions import StorageIntegrityError

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)
_EXCLUDED = frozenset(
    {
        "raw",
        "study",
        "cache",
        "caches",
        "completion",
        "agent",
        "agents",
        "report",
        "reports",
        "prompt",
        "prompts",
        "test",
        "tests",
        "provider",
        "providers",
        "workflow",
        "workflows",
        "staging",
        "review",
        "reviews",
        "evidence",
        "evidences",
    }
)


def validate_relative_path(path: str) -> str:
    """Reject ambiguous Windows/POSIX spellings, even on a POSIX worker."""
    if not isinstance(path, str) or not path or len(path) > 512:
        raise StorageIntegrityError("runtime path is invalid")
    for part in path.split("/"):
        if (
            not _COMPONENT.fullmatch(part)
            or part in {".", ".."}
            or part.endswith(".")
            or _RESERVED.fullmatch(part)
        ):
            raise StorageIntegrityError("runtime path is unsafe")
    return path


def _product_path(path: str) -> None:
    validate_relative_path(path)
    prefixes = (
        ".downforce/detailed/canonical/",
        ".downforce/archive/sessions/",
        ".downforce/normalized/",
        ".downforce/aliases/",
    )
    exact = {
        ".downforce/archive/catalog/historical-catalog.json",
        "artifacts/ml/registry.json",
        ".downforce/analytics/driver-race.parquet",
        ".downforce/analytics/manifest.json",
        ".downforce/analytics/circuit-identities.json",
    }
    ml_bundle = re.fullmatch(r"artifacts/ml/bundles/ml-bundle-sha256-[0-9a-f]{64}\.json", path)
    if path not in exact and not ml_bundle and not path.startswith(prefixes):
        raise StorageIntegrityError("runtime path is outside product data")
    if not path.endswith((".json", ".parquet")):
        raise StorageIntegrityError("runtime file type is unsupported")
    for component in path.split("/"):
        words = re.split(r"[_.-]", component.casefold())
        if any(word in _EXCLUDED for word in words):
            raise StorageIntegrityError("runtime path names excluded artifacts")


@dataclass(frozen=True, slots=True)
class ManifestLimits:
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_files: int = 100_000
    max_object_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 1024**4

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.max_manifest_bytes,
                self.max_files,
                self.max_object_bytes,
                self.max_total_bytes,
            )
        ):
            raise StorageIntegrityError("manifest limits are invalid")


@dataclass(frozen=True, slots=True)
class BlobFile:
    path: str
    size_bytes: int
    sha256: str


_DEFAULT_LIMITS = ManifestLimits()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StorageIntegrityError("release JSON contains duplicate keys")
        result[key] = value
    return result


def _invalid_number(value: str) -> object:
    raise StorageIntegrityError("release JSON contains nonfinite data")


@dataclass(frozen=True, slots=True, init=False)
class RuntimeManifest:
    sha256: str
    files: Mapping[str, BlobFile]

    def __init__(self) -> None:
        raise TypeError("runtime manifests must be authenticated with from_bytes")

    @classmethod
    def from_bytes(
        cls, body: bytes, expected_sha256: str, *, limits: ManifestLimits = _DEFAULT_LIMITS
    ) -> RuntimeManifest:
        if not isinstance(body, bytes) or len(body) > limits.max_manifest_bytes:
            raise StorageIntegrityError("release body exceeds manifest limit")
        if (
            not isinstance(expected_sha256, str)
            or not _SHA256.fullmatch(expected_sha256)
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise StorageIntegrityError("release checksum verification failed")
        try:
            data: object = json.loads(
                body,
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_number,
                parse_float=_invalid_number,
            )
        except (ValueError, UnicodeError, RecursionError):
            raise StorageIntegrityError("release JSON is malformed") from None
        if not isinstance(data, dict) or set(data) != {"schema_version", "files"}:
            raise StorageIntegrityError("release schema is invalid")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise StorageIntegrityError("release schema version is unsupported")
        entries = data["files"]
        if not isinstance(entries, dict) or not entries or len(entries) > limits.max_files:
            raise StorageIntegrityError("release file count is invalid")
        files: dict[str, BlobFile] = {}
        spellings: dict[str, str] = {}
        total = 0
        for path, value in cast(dict[str, object], entries).items():
            _product_path(path)
            for index in range(1, len(path.split("/")) + 1):
                prefix = "/".join(path.split("/")[:index])
                key = prefix.casefold()
                if key in spellings and spellings[key] != prefix:
                    raise StorageIntegrityError("release paths have a case collision")
                spellings[key] = prefix
            if not isinstance(value, dict) or set(value) != {"size_bytes", "sha256"}:
                raise StorageIntegrityError("release file metadata is invalid")
            size, digest = value["size_bytes"], value["sha256"]
            if type(size) is not int or not 0 <= size <= limits.max_object_bytes:
                raise StorageIntegrityError("release object size exceeds limit")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise StorageIntegrityError("release object checksum is invalid")
            total += size
            if total > limits.max_total_bytes:
                raise StorageIntegrityError("release total bytes exceed limit")
            files[path] = BlobFile(path, size, digest)
        for path in files:
            parts = path.split("/")
            if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
                raise StorageIntegrityError("release file and directory paths collide")
        result = object.__new__(cls)
        object.__setattr__(result, "sha256", expected_sha256)
        object.__setattr__(result, "files", MappingProxyType(files))
        return result

    def entry(self, path: str) -> BlobFile:
        return self.files[path]

    def paths(self, prefix: str = "", *, suffix: str | None = None) -> tuple[str, ...]:
        if prefix:
            validate_relative_path(prefix)
        return tuple(
            sorted(
                path
                for path in self.files
                if (not prefix or path == prefix or path.startswith(prefix + "/"))
                and (suffix is None or path.endswith(suffix))
            )
        )
