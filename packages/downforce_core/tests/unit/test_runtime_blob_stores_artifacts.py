"""Frozen ML registry/bundle reads retain unavailable semantics and never publish."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError
from test_runtime_blob_stores_detailed import runtime_cache


def test_artifact_bridge_exists() -> None:
    module = importlib.import_module("downforce_core.runtime_blob.stores_artifacts")
    assert issubclass(module.BlobArtifactStore, ArtifactStore)


def test_artifact_bridge_equal_local_only_referenced_bundle_and_no_writes(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_artifacts import BlobArtifactStore

    local = ArtifactStore(tmp_path / "source")
    local.publish({"dataset_digest": "old"})
    bundle_id, _ = local.publish({"dataset_digest": "frozen"})
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobArtifactStore(cache)
    assert not cache.release_root.exists()
    assert remote.load() == local.load()
    assert reader.calls == ["artifacts/ml/registry.json", f"artifacts/ml/bundles/{bundle_id}.json"]
    assert remote.load() == local.load()
    assert len(reader.calls) == 2
    with pytest.raises(StorageIntegrityError, match="read-only"):
        remote.publish({"dataset_digest": "new"})


@pytest.mark.parametrize("failure", ["missing", "checksum", "version", "path", "unavailable"])
def test_artifacts_unavailable_preserve_safe_original_error_type(
    tmp_path: Path, failure: str
) -> None:
    from downforce_core.runtime_blob.stores_artifacts import BlobArtifactStore

    local = ArtifactStore(tmp_path / "source")
    bundle_id, _ = local.publish({"dataset_digest": "frozen"})
    registry = json.loads(local.registry_path.read_text("utf-8"))
    if failure == "checksum":
        registry["sha256"] = "a" * 64
    if failure == "version":
        registry["ml_schema_version"] = "stale"
    if failure == "path":
        registry["path"] = "../../private-provider-credential.json"
    local.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    bundle_path = f"artifacts/ml/bundles/{bundle_id}.json"
    exclude = (bundle_path,) if failure == "missing" else ()
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime", exclude=exclude)
    if failure == "unavailable":
        local.registry_path.unlink()
    with pytest.raises(ArtifactUnavailableError) as caught:
        BlobArtifactStore(cache).load()
    assert "credential" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert all("credential" not in path for path in reader.calls)


@pytest.mark.parametrize("part", ["registry", "bundle"])
def test_artifacts_deep_remote_json_is_safe_unavailable_failure(tmp_path: Path, part: str) -> None:
    from downforce_core.runtime_blob.stores_artifacts import BlobArtifactStore

    local = ArtifactStore(tmp_path / "source")
    bundle_id, _ = local.publish({"dataset_digest": "frozen"})
    payload = b"[" * 10000 + b"0" + b"]" * 10000
    if part == "registry":
        local.registry_path.write_bytes(payload)
    else:
        (local.root / "bundles" / f"{bundle_id}.json").write_bytes(payload)
        registry = json.loads(local.registry_path.read_text("utf-8"))
        registry["sha256"] = hashlib.sha256(payload).hexdigest()
        local.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(ArtifactUnavailableError) as caught:
        BlobArtifactStore(cache).load()
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in str(caught.value)
