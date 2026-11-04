"""Frozen JSON model bundles with original ML unavailable/error semantics."""

from __future__ import annotations

import json
import re

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError
from downforce_core.runtime_blob.cache import RuntimeBlobCache


class BlobArtifactStore(ArtifactStore):
    def __init__(self, cache: RuntimeBlobCache) -> None:
        self.cache = cache
        super().__init__(cache.release_root)

    def load(self) -> dict[str, object]:
        registry = "artifacts/ml/registry.json"
        try:
            with self.cache.lease([registry]) as paths:
                value = json.loads(paths[registry].read_text("utf-8"))
                if not isinstance(value, dict):
                    raise ArtifactUnavailableError("invalid runtime registry")
                relative = value.get("path")
                if (
                    not isinstance(relative, str)
                    or re.fullmatch(r"bundles/ml-bundle-sha256-[0-9a-f]{64}\.json", relative)
                    is None
                ):
                    raise ArtifactUnavailableError("invalid runtime bundle path")
                with self.cache.lease([f"artifacts/ml/{relative}"]):
                    return super().load()
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            RecursionError,
            StorageIntegrityError,
            ArtifactUnavailableError,
        ):
            raise ArtifactUnavailableError(
                "runtime ML artifact is unavailable or invalid"
            ) from None

    def publish(self, payload: dict[str, object]) -> tuple[str, str]:
        raise StorageIntegrityError("runtime product storage is read-only")
