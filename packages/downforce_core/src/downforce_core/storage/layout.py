"""Validated DOWNFORCE data layout and atomic small-file primitives."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError


def _safe_component(value: str, field_name: str) -> str:
    validate_safe_identifier(value, field_name=field_name)
    return value


def ensure_contained(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    """Resolve a storage path and reject symlinks or root escapes."""

    lexical_root = root.absolute()
    cursor = path.absolute()
    while True:
        if cursor.is_symlink():
            raise StorageIntegrityError(f"storage path must not use symlinks: {path.name}")
        if cursor == lexical_root:
            break
        if lexical_root not in cursor.parents:
            raise StorageIntegrityError(f"storage path escapes its root: {path.name}")
        cursor = cursor.parent
    try:
        resolved = path.resolve(strict=must_exist)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise StorageIntegrityError(f"storage path cannot be resolved: {path.name}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise StorageIntegrityError(f"storage path escapes its root: {path.name}")
    return resolved


def write_text_atomic(path: Path, text: str) -> None:
    """Durably replace a small UTF-8 pointer/manifest on the same filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """All paths used by DOWNFORCE, rooted below one project directory."""

    project_root: Path

    def __init__(self, project_root: str | Path) -> None:
        root = Path(project_root).resolve()
        object.__setattr__(self, "project_root", root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    @property
    def data_root(self) -> Path:
        return self.project_root / ".downforce"

    @property
    def staging_root(self) -> Path:
        return self.data_root / ".staging"

    @property
    def normalized_root(self) -> Path:
        return self.data_root / "normalized"

    @property
    def aliases_root(self) -> Path:
        return self.data_root / "aliases"

    def raw_session_root(self, provider: str, session_id: str) -> Path:
        return (
            self.data_root
            / "raw"
            / _safe_component(provider, "provider")
            / _safe_component(session_id, "session_id")
        )

    def raw_snapshot(self, provider: str, session_id: str, snapshot_id: str) -> Path:
        return self.raw_session_root(provider, session_id) / _safe_component(
            snapshot_id, "snapshot_id"
        )

    def normalized_session_root(self, session_id: str) -> Path:
        return self.normalized_root / _safe_component(session_id, "session_id")

    def normalized_dataset(self, session_id: str, dataset_id: str) -> Path:
        return self.normalized_session_root(session_id) / _safe_component(dataset_id, "dataset_id")

    def active_pointer(self, session_id: str) -> Path:
        return self.normalized_session_root(session_id) / "active.json"

    def alias_pointer(self, requested_session_id: str) -> Path:
        return self.aliases_root / f"{_safe_component(requested_session_id, 'session_id')}.json"


__all__ = ["StorageLayout", "ensure_contained", "write_text_atomic"]
