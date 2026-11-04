"""Historical archive bridges preserve table validation and indexed product metrics."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from downforce_core.archive.schemas import ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import StorageIntegrityError
from test_analytics import _fixture_store
from test_runtime_blob_cache import windows_short_path
from test_runtime_blob_stores_detailed import runtime_cache


def test_archive_bridge_exists() -> None:
    module = importlib.import_module("downforce_core.runtime_blob.stores_archive")
    assert issubclass(module.BlobHistoricalArchiveStore, HistoricalArchiveStore)


def test_archive_selected_table_catalog_and_metadata_equal_local(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    assert not cache.release_root.exists()
    assert remote.session_ids() == local.session_ids()
    assert remote.load_sync_state() is None
    assert reader.calls == []
    session = local.session_ids()[0]
    assert remote.load_table(session, ArchiveTableName.LAPS).equals(
        local.load_table(session, ArchiveTableName.LAPS)
    )
    assert len(reader.calls) == 3
    assert sum(path.endswith(".parquet") for path in reader.calls) == 1
    assert all(session in path for path in reader.calls)
    assert remote.load_manifest(session) == local.load_manifest(session)
    assert remote.load_catalog() == local.load_catalog()
    revision = local.active_revision(session)
    assert revision is not None and remote.revision_is_valid(session, revision)
    assert sum(path.endswith(".parquet") for path in reader.calls) == 3
    assert all(session in path for path in reader.calls if "/sessions/" in path)
    with remote.lease_session_metadata(session) as manifest:
        assert manifest == local.load_manifest(session)


def test_archive_storage_report_is_release_product_not_hydrated_subset(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    report = remote.storage_report()
    paths = cache.manifest.paths(".downforce/archive")
    assert report["file_count"] == len(paths)
    assert report["session_count"] == len(local.session_ids())
    assert report["total_bytes"] == sum(cache.manifest.entry(path).size_bytes for path in paths)
    assert report["catalog_bytes"] == local.catalog_path.stat().st_size
    assert reader.calls == []
    remote.load_table(local.session_ids()[0], ArchiveTableName.RESULTS)
    assert remote.storage_report() == report


def test_archive_other_table_corruption_is_targeted_then_full_revision_invalid(
    tmp_path: Path,
) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    session = local.session_ids()[0]
    revision = local.active_revision(session)
    assert revision is not None
    root = local.sessions_root / session / "revisions" / revision
    (root / "pit_stops.parquet").write_bytes(b"corrupt")
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    assert remote.load_table(session, ArchiveTableName.LAPS).num_rows > 0
    assert not remote.revision_is_valid(session, revision)
    with pytest.raises(StorageIntegrityError):
        remote.load_table(session, ArchiveTableName.PIT_STOPS)
    assert not remote.revision_is_valid(session, "not-a-revision")
    assert remote.active_revision("archive-unknown") is None


@pytest.mark.parametrize(
    "method",
    [
        "write_session",
        "_write_session_locked",
        "write_sync_state",
        "save_catalog",
        "save_quality_report",
        "exclusive_lock",
    ],
)
def test_archive_writers_are_disabled(tmp_path: Path, method: str) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    _fixture_store(tmp_path / "source")
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    with pytest.raises(StorageIntegrityError, match="read-only"):
        if method == "exclusive_lock":
            with remote.exclusive_lock("test"):
                pytest.fail("writer lock opened")
        else:
            getattr(remote, method)(None)
    assert reader.calls == []
    assert not cache.release_root.exists()


@pytest.mark.parametrize("part", ["catalog", "pointer", "manifest", "revision-validation"])
def test_archive_deep_remote_json_is_safe_integrity_failure(tmp_path: Path, part: str) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    session = local.session_ids()[0]
    revision = local.active_revision(session)
    assert revision is not None
    path = (
        local.catalog_path
        if part == "catalog"
        else (
            local.sessions_root / session / "active.json"
            if part == "pointer"
            else local.sessions_root / session / "revisions" / revision / "manifest.json"
        )
    )
    path.write_bytes(b"[" * 10000 + b"0" + b"]" * 10000)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    if part == "revision-validation":
        assert not remote.revision_is_valid(session, revision)
    else:
        with pytest.raises(StorageIntegrityError) as caught:
            if part == "catalog":
                remote.load_catalog()
            elif part == "pointer":
                remote.active_revision(session)
            else:
                remote.load_manifest(session)
        assert caught.value.__cause__ is None
        assert str(tmp_path) not in str(caught.value)


def test_archive_bridge_accepts_actual_windows_short_temp_path(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    short_root = windows_short_path(tmp_path) / "runtime"
    cache, _ = runtime_cache(tmp_path / "source", short_root)
    remote = BlobHistoricalArchiveStore(cache)
    assert remote.load_catalog() == local.load_catalog()
    session = local.session_ids()[0]
    assert remote.load_manifest(session) == local.load_manifest(session)


def test_archive_malformed_authentic_catalog_is_safe_integrity_failure(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    catalog = json.loads(local.catalog_path.read_text("utf-8"))
    catalog["seasons"][0]["events"][0]["sessions"][0]["capabilities"] = []
    local.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError) as caught:
        BlobHistoricalArchiveStore(cache).load_catalog()
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("operation", ["load-table", "revision-validation"])
def test_archive_hash_consistent_malformed_table_descriptor_is_safe_failure(
    tmp_path: Path, operation: str
) -> None:
    from downforce_core.runtime_blob.stores_archive import BlobHistoricalArchiveStore

    local = _fixture_store(tmp_path / "source")
    session = local.session_ids()[0]
    original_revision = local.active_revision(session)
    assert original_revision is not None
    root = local.sessions_root / session / "revisions" / original_revision
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["tables"]["laps"] = []
    revision_payload = {
        key: manifest[key]
        for key in (
            "archive_schema_version",
            "session_id",
            "source_revision",
            "metadata_sha256",
            "tables",
        )
    }
    revision = (
        "archive-revision-sha256-"
        + hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    manifest["data_revision"] = revision
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    root.rename(root.with_name(revision))
    (local.sessions_root / session / "active.json").write_text(
        json.dumps({"data_revision": revision}),
        encoding="utf-8",
    )
    # Authentication and original metadata validation both succeed: descriptor
    # shape must still be rejected at the table/complete-revision boundary.
    assert local.load_manifest(session) == manifest
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobHistoricalArchiveStore(cache)
    if operation == "revision-validation":
        assert not remote.revision_is_valid(session, revision)
    else:
        with pytest.raises(StorageIntegrityError) as caught:
            remote.load_table(session, ArchiveTableName.LAPS)
        assert caught.value.__cause__ is None
        assert str(tmp_path) not in str(caught.value)
