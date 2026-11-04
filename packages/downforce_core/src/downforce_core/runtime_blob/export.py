"""Prepare authenticated product releases locally, without copying source payloads.

This is a runtime product index, not an official provider index. Source provenance
is retained byte-for-byte; only the compact circuit mapping is generated.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Never, cast

from downforce_core.analytics.circuits import CircuitIdentityResolver
from downforce_core.analytics.storage import AnalyticsDerivedStore
from downforce_core.archive.schemas import ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactStore
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.manifest import (
    ManifestLimits,
    RuntimeManifest,
    validate_relative_path,
)
from downforce_core.runtime_blob.stores_analytics import BlobCircuitIdentityResolver
from downforce_core.runtime_blob.stores_repository import _ReadOnlyLayout
from downforce_core.storage.repository import DownforceRepository

_CHUNK_BYTES = 64 * 1024
_CIRCUITS = ".downforce/analytics/circuit-identities.json"
_LIMITS = ManifestLimits()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _safe_path(root: Path, path: Path, *, file: bool = False) -> None:
    """Check lexical containment and every existing ancestor before following it."""
    try:
        relative = path.relative_to(root)
        for ancestor in reversed(root.parents):
            ancestor_info = ancestor.lstat()
            if (
                stat.S_ISLNK(ancestor_info.st_mode)
                or getattr(ancestor_info, "st_file_attributes", 0) & 0x400
            ):
                raise StorageIntegrityError("release source or output ancestor is unsafe")
        current = root
        for component in ("", *relative.parts):
            if component:
                current /= component
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise StorageIntegrityError("release source or output path is unsafe")
        if not path.resolve().is_relative_to(root.resolve()):
            raise StorageIntegrityError("release source or output path is unsafe")
        if file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise StorageIntegrityError("release source or output file is unsafe")
    except (OSError, ValueError):
        raise StorageIntegrityError("release source or output path is unsafe or missing") from None


def _children(root: Path, directory: Path) -> tuple[Path, ...]:
    if not directory.exists():
        return ()
    _safe_path(root, directory)
    return tuple(sorted(directory.iterdir()))


def _object(path: Path) -> dict[str, object]:
    try:
        value: object = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return cast(dict[str, object], value)
    except (OSError, ValueError, RecursionError):
        raise StorageIntegrityError("release source metadata is invalid") from None


class _SourceRepository(DownforceRepository):
    def __init__(self, root: Path) -> None:
        self.layout = _ReadOnlyLayout(root)

    def write_session(self, *args: object, **kwargs: object) -> Never:
        raise StorageIntegrityError("release source repository is read-only")


@dataclass(frozen=True, slots=True)
class RuntimeRelease:
    project_root: Path
    body: bytes
    sha256: str
    manifest: RuntimeManifest
    source_files: Mapping[str, Path]
    generated_files: Mapping[str, bytes]

    def reader(self) -> ReleaseObjectReader:
        """Explicit offline injection: this reader never contacts a provider."""
        return ReleaseObjectReader(self)

    def write_metadata(self) -> Path:
        """Write only known small generated metadata in a content-addressed child."""
        base = self.project_root / ".downforce/deployment/releases"
        output = base / self.sha256
        known = {
            "release.json": self.body,
            "circuit-identities.json": self.generated_files[_CIRCUITS],
        }
        # Validate all existing targets before any mutation, including unknown entries.
        current = self.project_root
        for part in output.relative_to(self.project_root).parts:
            current /= part
            if current.exists() or current.is_symlink():
                _safe_path(self.project_root, current)
                if not current.is_dir():
                    raise StorageIntegrityError("release output directory is unavailable")
        if output.exists():
            if {path.name for path in output.iterdir()} != set(known):
                raise StorageIntegrityError("release output contains unknown or incomplete files")
            for name, body in known.items():
                path = output / name
                _safe_path(self.project_root, path, file=True)
                if path.read_bytes() != body:
                    raise StorageIntegrityError("release output differs from known generated bytes")
            return output
        output.mkdir(parents=True, exist_ok=False)
        for name, body in known.items():
            with (output / name).open("xb") as handle:
                handle.write(body)
        return output


@dataclass(frozen=True, slots=True)
class ReleaseObjectReader:
    release: RuntimeRelease

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        entry = self.release.manifest.entry(relative_path)
        if type(max_bytes) is not int or entry.size_bytes > max_bytes:
            raise StorageIntegrityError("release read exceeds object bound")
        digest = hashlib.sha256()
        count = 0
        if relative_path in self.release.generated_files:
            generated = self.release.generated_files[relative_path]
            for offset in range(0, len(generated), _CHUNK_BYTES):
                chunk = generated[offset : offset + _CHUNK_BYTES]
                count += len(chunk)
                digest.update(chunk)
                yield chunk
        else:
            path = self.release.source_files[relative_path]
            _safe_path(self.release.project_root, path, file=True)
            with path.open("rb") as handle:
                while chunk := handle.read(min(_CHUNK_BYTES, max_bytes + 1)):
                    count += len(chunk)
                    if count > entry.size_bytes or count > max_bytes:
                        raise StorageIntegrityError("release source changed after indexing")
                    digest.update(chunk)
                    yield chunk
        if count != entry.size_bytes or digest.hexdigest() != entry.sha256:
            raise StorageIntegrityError("release source changed after indexing")


def prepare_runtime_release(project_root: Path, *, include_detailed: bool = True) -> RuntimeRelease:
    """Validate original readers, hash bounded files and retain read-only source references."""
    root = project_root.absolute()
    _safe_path(root, root)
    sources: dict[str, Path] = {}
    entries: dict[str, dict[str, int | str]] = {}

    def add(path: Path) -> None:
        _safe_path(root, path, file=True)
        relative = validate_relative_path(path.relative_to(root).as_posix())
        size = path.stat().st_size
        if size > _LIMITS.max_object_bytes:
            raise StorageIntegrityError("release source exceeds object bound")
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                count += len(chunk)
                if count > _LIMITS.max_object_bytes:
                    raise StorageIntegrityError("release source exceeds object bound")
                digest.update(chunk)
        if count != size:
            raise StorageIntegrityError("release source changed during indexing")
        sources[relative] = path
        entries[relative] = {"size_bytes": size, "sha256": digest.hexdigest()}

    repository = _SourceRepository(root)
    included: set[str] = set()
    for session in _children(root, root / ".downforce/normalized"):
        pointer = session / "active.json"
        if not pointer.exists():
            continue
        add(pointer)
        canonical, dataset = repository.active_dataset_identity(session.name)
        if canonical != session.name:
            raise StorageIntegrityError("release canonical pointer identity conflicts")
        dataset_root = repository.layout.normalized_dataset(canonical, dataset)
        add(dataset_root / "manifest.json")
        metadata = repository._load_dataset_manifest(canonical, dataset)
        for name, table in metadata.tables.items():
            if table.materialized:
                if table.path != f"tables/{name}.parquet":
                    raise StorageIntegrityError("release canonical table path conflicts")
                add(dataset_root / "tables" / f"{name}.parquet")
        repository.load_manifest(canonical)
        included.add(canonical)
    for alias in _children(root, root / ".downforce/aliases"):
        if alias.suffix != ".json":
            continue
        add(alias)
        target = repository.resolve_session_id(alias.stem)
        if target not in included:
            raise StorageIntegrityError("release alias target is not included")

    if include_detailed:
        detailed = DetailedStore(root, detailed_root=root / ".downforce/detailed")
        for session in _children(root, detailed.root):
            revisions = tuple(
                path
                for path in _children(root, session / "revisions")
                if re.fullmatch(r"[0-9a-f]{64}", path.name)
            )
            if not revisions and not (session / "active.json").exists():
                continue
            add(session / "active.json")
            active = detailed.manifest(session.name)["revision"]
            if active not in {path.name for path in revisions}:
                raise StorageIntegrityError("release detailed active revision is missing")
            for revision_root in revisions:
                add(revision_root / "manifest.json")
                metadata_detailed = detailed.manifest(session.name, revision=revision_root.name)
                for name in metadata_detailed["tables"]:
                    validate_relative_path(name)
                    add(revision_root / f"{name}.parquet")
                detailed.validate(session.name, revision=revision_root.name)

    archive = HistoricalArchiveStore(root)
    add(archive.catalog_path)
    catalog = archive.load_catalog()
    archive_sessions: dict[str, str] = {}
    for event in catalog.events:
        for session_metadata in event.sessions:
            if session_metadata.data_revision is None:
                continue
            session_id, revision = session_metadata.session_id, session_metadata.data_revision
            if session_id in archive_sessions:
                raise StorageIntegrityError("release archive catalog identity conflicts")
            archive_sessions[session_id] = revision
            session_root = archive.sessions_root / session_id
            add(session_root / "active.json")
            if archive.active_revision(session_id) != revision:
                raise StorageIntegrityError(
                    "release archive active revision conflicts with catalog"
                )
            revision_root = session_root / "revisions" / revision
            add(revision_root / "manifest.json")
            for archive_table in ArchiveTableName:
                add(revision_root / f"{archive_table.value}.parquet")
            archive.load_manifest(session_id)
            if not archive.revision_is_valid(session_id, revision):
                raise StorageIntegrityError("release archive revision is invalid")
            for archive_table in ArchiveTableName:
                archive.load_table(session_id, archive_table)
    for session in _children(root, archive.sessions_root):
        if (session / "active.json").exists() and session.name not in archive_sessions:
            raise StorageIntegrityError("release archive active session is absent from catalog")

    analytics = AnalyticsDerivedStore(root)
    add(analytics.manifest_path)
    add(analytics.root / "driver-race.parquet")
    snapshot = analytics.load(catalog.source_revision)
    if snapshot is None:
        raise StorageIntegrityError("release analytics snapshot is absent, stale or invalid")
    dumps = tuple(sorted((archive.raw_root / "jolpica").glob("jolpica-csv-sha256-*.zip")))
    if dumps:
        _safe_path(root, dumps[-1], file=True)
    resolver = CircuitIdentityResolver.from_store(archive)
    if not resolver.identities or snapshot.provider_circuit_identities != len(resolver.identities):
        raise StorageIntegrityError("release circuit identities conflict with analytics")
    compact = _json_bytes(
        {
            "schema_version": 1,
            "identities": [
                {"season": season, "round_number": round_number, "circuit_id": identity}
                for (season, round_number), identity in sorted(resolver.identities.items())
            ],
        }
    )
    generated = {_CIRCUITS: compact}
    entries[_CIRCUITS] = {"size_bytes": len(compact), "sha256": hashlib.sha256(compact).hexdigest()}

    models = ArtifactStore(root)
    add(models.registry_path)
    registry = _object(models.registry_path)
    bundle_id = registry.get("active_bundle_id")
    relative_bundle = registry.get("path")
    if (
        not isinstance(bundle_id, str)
        or not re.fullmatch(r"ml-bundle-sha256-[0-9a-f]{64}", bundle_id)
        or relative_bundle != f"bundles/{bundle_id}.json"
        or registry.get("sha256") != bundle_id.removeprefix("ml-bundle-sha256-")
    ):
        raise StorageIntegrityError("release model registry identity is invalid")
    add(models.root / relative_bundle)
    models.load()

    body = _json_bytes({"schema_version": 1, "files": entries})
    digest = hashlib.sha256(body).hexdigest()
    manifest = RuntimeManifest.from_bytes(body, digest)
    release = RuntimeRelease(
        root, body, digest, manifest, MappingProxyType(sources), MappingProxyType(generated)
    )
    with TemporaryDirectory(prefix="downforce-release-validation-") as temporary:
        cache = RuntimeBlobCache(Path(temporary) / "objects", manifest, release.reader())
        if BlobCircuitIdentityResolver.from_cache(cache).identities != resolver.identities:
            raise StorageIntegrityError("release circuit identities do not preserve the source")
    return release
