"""Hash-verified, JSON-only model artifact registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from downforce_core.ml.contracts import ML_SCHEMA_VERSION, MODEL_BUNDLE_VERSION
from downforce_core.ml.model import RidgeModel


class ArtifactUnavailableError(RuntimeError):
    """Raised when a trusted, compatible local model bundle is not available."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ridge_to_dict(model: RidgeModel) -> dict[str, object]:
    return cast(dict[str, object], asdict(model))


def ridge_from_dict(value: object) -> RidgeModel:
    if not isinstance(value, dict):
        raise ArtifactUnavailableError("ridge artifact is malformed")
    try:
        return RidgeModel(
            means=tuple(
                float(cast(str | int | float, item)) for item in cast(list[object], value["means"])
            ),
            scales=tuple(
                float(cast(str | int | float, item)) for item in cast(list[object], value["scales"])
            ),
            coefficients=tuple(
                float(cast(str | int | float, item))
                for item in cast(list[object], value["coefficients"])
            ),
            intercept=float(cast(str | int | float, value["intercept"])),
            regularization=float(cast(str | int | float, value["regularization"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("ridge artifact is malformed") from exc


class ArtifactStore:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve() / "artifacts" / "ml"
        self.registry_path = self.root / "registry.json"

    def publish(self, payload: dict[str, object]) -> tuple[str, str]:
        envelope = {
            "model_bundle_version": MODEL_BUNDLE_VERSION,
            "ml_schema_version": ML_SCHEMA_VERSION,
            **payload,
        }
        encoded = canonical_json(envelope).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        bundle_id = f"ml-bundle-sha256-{digest}"
        bundle_path = self.root / "bundles" / f"{bundle_id}.json"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_bytes(encoded + b"\n")
        registry = {
            "model_bundle_version": MODEL_BUNDLE_VERSION,
            "ml_schema_version": ML_SCHEMA_VERSION,
            "active_bundle_id": bundle_id,
            "sha256": digest,
            "path": f"bundles/{bundle_id}.json",
            "published_at_utc": datetime.now(UTC).isoformat(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(canonical_json(registry) + "\n", encoding="utf-8")
        return bundle_id, digest

    def load(self) -> dict[str, object]:
        try:
            registry_value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactUnavailableError("ML artifact registry is unavailable") from exc
        if not isinstance(registry_value, dict):
            raise ArtifactUnavailableError("ML artifact registry is malformed")
        if (
            registry_value.get("model_bundle_version") != MODEL_BUNDLE_VERSION
            or registry_value.get("ml_schema_version") != ML_SCHEMA_VERSION
        ):
            raise ArtifactUnavailableError("ML artifact registry version is incompatible")
        relative = registry_value.get("path")
        expected = registry_value.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ArtifactUnavailableError("ML artifact registry identity is malformed")
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ArtifactUnavailableError("ML artifact path escapes the registry")
        try:
            encoded = path.read_bytes().rstrip(b"\n")
        except OSError as exc:
            raise ArtifactUnavailableError("ML artifact bundle is unavailable") from exc
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise ArtifactUnavailableError("ML artifact checksum failed")
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ArtifactUnavailableError("ML artifact bundle is malformed") from exc
        if not isinstance(payload, dict):
            raise ArtifactUnavailableError("ML artifact bundle is malformed")
        if (
            payload.get("model_bundle_version") != MODEL_BUNDLE_VERSION
            or payload.get("ml_schema_version") != ML_SCHEMA_VERSION
        ):
            raise ArtifactUnavailableError("ML artifact bundle version is incompatible")
        return cast(dict[str, object], payload)


__all__ = [
    "ArtifactStore",
    "ArtifactUnavailableError",
    "canonical_json",
    "ridge_from_dict",
    "ridge_to_dict",
]
