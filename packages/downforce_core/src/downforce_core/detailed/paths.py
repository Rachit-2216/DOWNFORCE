"""One explicitly configurable detailed-data root, independent of archive and ML."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from downforce_core.exceptions import StorageIntegrityError


class DetailedPaths:
    def __init__(
        self,
        project_root: Path,
        *,
        override: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        project = project_root.absolute()
        environment = os.environ if environ is None else environ
        configured = environment.get("DOWNFORCE_DETAILED_DATA_ROOT")
        root = (
            override
            if override is not None
            else (
                Path(configured) if configured is not None else project / ".downforce" / "detailed"
            )
        )
        if configured == "" and override is None:
            raise ValueError("configured detailed root is empty")
        if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
            raise ValueError("detailed root must be an absolute dedicated data directory")
        root = root.absolute()
        if root == project:
            raise ValueError("project root cannot be the detailed data root")
        if root.is_relative_to(project):
            relative = root.relative_to(project)
            if relative.parts[0].casefold() in (
                "apps",
                "packages",
                ".git",
                ".agents",
                ".codex",
                "artifacts",
                "docs",
                "scripts",
                "assets",
                "references",
                "data",
                "node_modules",
            ):
                raise ValueError("detailed root cannot target repository sources")
            if relative.parts[0].casefold() == ".downforce" and (
                len(relative.parts) < 2 or relative.parts[1].casefold() != "detailed"
            ):
                raise ValueError("detailed root cannot target another data product")
        cursor = root
        while True:
            if cursor.is_symlink() or cursor.is_junction():
                raise StorageIntegrityError("detailed root must not contain linked directories")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        self.root = root.resolve()

    @property
    def canonical(self) -> Path:
        return self.root / "canonical"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def staging(self) -> Path:
        return self.root / "staging"
