"""Derived analytics fail closed and compact circuit identities never load raw evidence."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from downforce_core.analytics.engine import AnalyticsEngine
from downforce_core.analytics.storage import AnalyticsDerivedStore
from downforce_core.exceptions import StorageIntegrityError
from test_analytics import _fixture_store
from test_runtime_blob_stores_detailed import runtime_cache


def analytics_fixture(tmp_path: Path):
    archive = _fixture_store(tmp_path / "source")
    AnalyticsEngine(archive).rebuild_manifest()
    return AnalyticsDerivedStore(tmp_path / "source"), archive.load_catalog().source_revision


def test_analytics_bridge_exists() -> None:
    module = importlib.import_module("downforce_core.runtime_blob.stores_analytics")
    assert issubclass(module.BlobAnalyticsDerivedStore, AnalyticsDerivedStore)


def test_analytics_snapshot_equal_local_and_only_two_files_fetched(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_analytics import BlobAnalyticsDerivedStore

    local, revision = analytics_fixture(tmp_path)
    cache, reader = runtime_cache(
        tmp_path / "source",
        tmp_path / "runtime",
        exclude=(".downforce/analytics/coverage-report.json",),
    )
    remote = BlobAnalyticsDerivedStore(cache)
    assert not cache.release_root.exists()
    assert remote.load(revision) == local.load(revision)
    assert reader.calls == [
        ".downforce/analytics/manifest.json",
        ".downforce/analytics/driver-race.parquet",
    ]
    assert remote.load(revision) == local.load(revision)
    assert len(reader.calls) == 2
    with pytest.raises(StorageIntegrityError, match="read-only"):
        remote.publish(
            (),
            source_revision=revision,
            snapshot_digest="x",
            built_at_utc="x",
            provider_circuit_identities=0,
        )


@pytest.mark.parametrize("failure", ["stale", "missing", "version", "checksum", "rows"])
def test_analytics_invalid_snapshot_raises_instead_of_rebuild_signal(
    tmp_path: Path, failure: str
) -> None:
    from downforce_core.runtime_blob.stores_analytics import BlobAnalyticsDerivedStore

    local, revision = analytics_fixture(tmp_path)
    manifest = json.loads(local.manifest_path.read_text("utf-8"))
    if failure == "version":
        manifest["analytics_version"] = "stale"
    if failure == "checksum":
        manifest["sha256"] = "a" * 64
    if failure == "rows":
        manifest["observation_rows"] += 1
    local.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    exclude = (".downforce/analytics/coverage-report.json",)
    if failure == "missing":
        exclude += (".downforce/analytics/driver-race.parquet",)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime", exclude=exclude)
    with pytest.raises(StorageIntegrityError, match="analytics"):
        BlobAnalyticsDerivedStore(cache).load("stale" if failure == "stale" else revision)
    assert all("/archive/" not in path for path in reader.calls)


def circuit_source(tmp_path: Path, payload: object) -> Path:
    source = tmp_path / "source"
    path = source / ".downforce/analytics/circuit-identities.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return source


def test_circuit_resolver_preserves_all_1172_original_style_identities(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_analytics import BlobCircuitIdentityResolver

    rows = [
        {
            "season": 1950 + index // 20,
            "round_number": 1 + index % 20,
            "circuit_id": f"circuit-{index}_provider",
        }
        for index in range(1172)
    ]
    source = circuit_source(tmp_path, {"schema_version": 1, "identities": rows})
    cache, reader = runtime_cache(source, tmp_path / "runtime")
    resolver = BlobCircuitIdentityResolver.from_cache(cache)
    assert resolver.provider_identity_count() == 1172
    for row in rows:
        assert resolver.resolve(row["season"], row["round_number"], "Ignored") == row["circuit_id"]
    assert resolver.resolve(2026, 98, "Unmapped Circuit") == "unmapped-circuit"
    assert reader.calls == [".downforce/analytics/circuit-identities.json"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"season": 1949, "round_number": 1, "circuit_id": "albert-park"}],
        [{"season": True, "round_number": 1, "circuit_id": "albert-park"}],
        [{"season": 2024, "round_number": 100, "circuit_id": "albert-park"}],
        [{"season": 2024, "round_number": 1, "circuit_id": "../unsafe"}],
        [{"season": 2024, "round_number": 1, "circuit_id": "x", "extra": 1}],
        [
            {"season": 2024, "round_number": 2, "circuit_id": "x"},
            {"season": 2024, "round_number": 1, "circuit_id": "y"},
        ],
        [{"season": 2024, "round_number": 1, "circuit_id": "x"}] * 2,
    ],
)
def test_circuit_resolver_invalid_mapping_never_falls_back_to_empty(tmp_path: Path, rows) -> None:
    from downforce_core.runtime_blob.stores_analytics import BlobCircuitIdentityResolver

    source = circuit_source(tmp_path, {"schema_version": 1, "identities": rows})
    cache, _ = runtime_cache(source, tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError, match="circuit"):
        BlobCircuitIdentityResolver.from_cache(cache)


@pytest.mark.parametrize("part", ["derived", "circuit"])
def test_analytics_deep_remote_json_is_safe_integrity_failure(tmp_path: Path, part: str) -> None:
    from downforce_core.runtime_blob.stores_analytics import (
        BlobAnalyticsDerivedStore,
        BlobCircuitIdentityResolver,
    )

    local, revision = analytics_fixture(tmp_path)
    path = local.manifest_path if part == "derived" else local.root / "circuit-identities.json"
    path.write_bytes(b"[" * 10000 + b"0" + b"]" * 10000)
    cache, _ = runtime_cache(
        tmp_path / "source",
        tmp_path / "runtime",
        exclude=(".downforce/analytics/coverage-report.json",),
    )
    with pytest.raises(StorageIntegrityError) as caught:
        if part == "derived":
            BlobAnalyticsDerivedStore(cache).load(revision)
        else:
            BlobCircuitIdentityResolver.from_cache(cache)
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in str(caught.value)
