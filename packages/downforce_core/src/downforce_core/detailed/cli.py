"""Storage status, frozen sampling plans, projections, and safe source restoration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.proofs import validated_study_measurements
from downforce_core.detailed.raw_evidence import RawEvidenceStore
from downforce_core.detailed.status import storage_status
from downforce_core.detailed.study_io import project_measured_study, publish_sample_plan
from downforce_core.storage.layout import ensure_contained, write_text_atomic


def project_study(paths: DetailedPaths) -> dict[str, Any]:
    if not (paths.root / "reconciled-inventory.json").exists():
        raise ValueError("reconciled inventory is unavailable; no schedule-only fallback")
    measurements = validated_study_measurements(paths)
    result = project_measured_study(
        paths.root, measurements, free_bytes=shutil.disk_usage(paths.root).free
    )
    write_text_atomic(
        paths.root / "stratified-projection.json", json.dumps(result, indent=2, allow_nan=False)
    )
    return result


def storage_command(
    paths: DetailedPaths,
    action: str,
    *,
    bundle: str | None = None,
    destination: Path | None = None,
    seed: int = 2216,
) -> dict[str, Any]:
    if action == "status":
        return storage_status(paths)
    if action == "plan":
        return publish_sample_plan(paths.root, seed=seed)
    if action == "project":
        return project_study(paths)
    if action == "restore":
        if bundle is None:
            raise ValueError("restore requires --bundle")
        # Restores are isolated under detailed staging and never merge user directories.
        paths.staging.mkdir(parents=True, exist_ok=True)
        target = destination or paths.staging / f"restored-{bundle}"
        target = ensure_contained(target, paths.staging, must_exist=False)
        store = RawEvidenceStore(paths.raw)
        manifest = store.restore(bundle, target)
        return {
            "restored_to": str(target),
            "bundle_sha256": bundle,
            "session_id": manifest["session_id"],
            "verified": True,
        }
    raise ValueError("unknown detailed storage command")
