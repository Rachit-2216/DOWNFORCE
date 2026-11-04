"""Legacy repository retains full canonical validation in the bounded release cache."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest
from downforce_core.exceptions import (
    SchemaVersionError,
    SessionNotFoundError,
    StorageIntegrityError,
)
from downforce_core.providers import DatasetAvailability, DatasetName
from downforce_core.storage.repository import DownforceRepository
from downforce_core.storage.schemas import CanonicalTableName
from test_replay_engine import _session
from test_runtime_blob_stores_detailed import runtime_cache


def repository_fixture(tmp_path: Path):
    local = DownforceRepository(tmp_path / "source")
    original = _session()
    session = replace(
        original,
        completeness={
            **original.completeness,
            DatasetName.TRACK_POSITIONS: DatasetAvailability.EMPTY,
        },
    )
    manifest = local.write_session(session, "fixture")
    return local, session, manifest


def test_repository_bridge_exists() -> None:
    module = importlib.import_module("downforce_core.runtime_blob.stores_repository")
    assert issubclass(module.BlobDownforceRepository, DownforceRepository)


def test_repository_metadata_alias_and_full_legacy_validation_equal_local(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    local, session, manifest = repository_fixture(tmp_path)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobDownforceRepository(cache)
    assert remote.layout.project_root == cache.release_root
    assert not cache.release_root.exists()
    requested = str(session.requested_session.session_id)
    assert remote.active_dataset_identity(requested) == local.active_dataset_identity(requested)
    assert len(reader.calls) == 2
    assert all(not path.endswith(".parquet") for path in reader.calls)
    assert remote.load_manifest(requested) == manifest
    materialized = {name for name, artifact in manifest.tables.items() if artifact.materialized}
    assert {Path(path).stem for path in reader.calls if path.endswith(".parquet")} == materialized
    for table_name in CanonicalTableName:
        assert remote.load_table(requested, table_name).equals(
            local.load_table(requested, table_name)
        )
    assert remote.list_sessions() == local.list_sessions()
    actual = remote.load_session(requested, include_track_positions=False)
    expected = local.load_session(requested, include_track_positions=False)
    assert actual.metadata == expected.metadata and actual.laps == expected.laps
    assert actual.track_positions.table.equals(expected.track_positions.table)


def test_repository_omitting_track_positions_does_not_omit_validation(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    local, session, manifest = repository_fixture(tmp_path)
    table = local.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
    (table / "tables/track_positions.parquet").write_bytes(b"corrupt")
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError):
        BlobDownforceRepository(cache).load_session(
            str(session.metadata.session_id), include_track_positions=False
        )
    assert any(path.endswith("/track_positions.parquet") for path in reader.calls)


def test_repository_missing_pointer_and_alias_semantics(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    _, session, _ = repository_fixture(tmp_path)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobDownforceRepository(cache)
    assert remote.resolve_session_id("unknown-race") == "unknown-race"
    assert not remote.session_exists("unknown-race")
    with pytest.raises(SessionNotFoundError):
        remote.active_dataset_identity("unknown-race")
    assert reader.calls == []
    with pytest.raises(StorageIntegrityError, match="read-only"):
        remote.write_session(session, "fixture")
    assert not cache.release_root.exists()


def test_repository_missing_materialized_entry_fails_closed(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    _, session, manifest = repository_fixture(tmp_path)
    missing = (
        f".downforce/normalized/{manifest.session_id}/{manifest.dataset_id}/tables/laps.parquet"
    )
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime", exclude=(missing,))
    with pytest.raises(StorageIntegrityError):
        BlobDownforceRepository(cache).load_manifest(str(session.metadata.session_id))
    assert not (cache.release_root / missing).exists()


@pytest.mark.parametrize("part", ["alias", "pointer", "manifest"])
@pytest.mark.parametrize(
    "payload", [b"[" * 10000 + b"0" + b"]" * 10000, b"\xff"], ids=["deep-json", "invalid-utf8"]
)
def test_repository_corrupt_json_is_safe_integrity_failure(
    tmp_path: Path, part: str, payload: bytes
) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    local, session, manifest = repository_fixture(tmp_path)
    requested = str(session.requested_session.session_id)
    path = {
        "alias": local.layout.alias_pointer(requested),
        "pointer": local.layout.active_pointer(manifest.session_id),
        "manifest": local.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        / "manifest.json",
    }[part]
    path.write_bytes(payload)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError) as caught:
        BlobDownforceRepository(cache).load_manifest(requested)
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "field",
    [
        "canonical_schema_version",
        "normalization_version",
        "storage_format_version",
        "timeline_version",
        "replay_version",
    ],
)
def test_repository_schema_version_error_precedes_absent_payload_and_all_fetches(
    tmp_path: Path, field: str
) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    local, _, manifest = repository_fixture(tmp_path)
    updates = {field: "stale"}
    if field in {"timeline_version", "replay_version"}:
        updates.update(timeline_version="stale", replay_version="stale")
    stale = replace(manifest, **updates)
    missing = (
        f".downforce/normalized/{manifest.session_id}/{manifest.dataset_id}/tables/laps.parquet"
    )
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime", exclude=(missing,))
    root = local.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
    with pytest.raises(SchemaVersionError) as expected:
        local._verify_manifest(stale, root)
    remote = BlobDownforceRepository(cache)
    with pytest.raises(type(expected.value), match=str(expected.value)):
        remote._verify_manifest(
            stale, remote.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        )
    assert reader.calls == []
    assert not cache.release_root.exists()


@pytest.mark.parametrize("has_indexed_pointer", [True, False])
def test_repository_listing_ignores_and_preserves_unindexed_cache_leftovers(
    tmp_path: Path, has_indexed_pointer: bool
) -> None:
    from downforce_core.runtime_blob.stores_repository import BlobDownforceRepository

    local, _, manifest = repository_fixture(tmp_path)
    pointer = f".downforce/normalized/{manifest.session_id}/active.json"
    cache, reader = runtime_cache(
        tmp_path / "source",
        tmp_path / "runtime",
        exclude=() if has_indexed_pointer else (pointer,),
    )
    orphan = cache.release_root / ".downforce/normalized/orphan-race/active.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b'{"session_id":"orphan-race","dataset_id":"unknown"}')
    original = orphan.read_bytes()
    expected = local.list_sessions() if has_indexed_pointer else ()
    assert BlobDownforceRepository(cache).list_sessions() == expected
    assert orphan.read_bytes() == original
    assert all("orphan-race" not in path for path in reader.calls)
    if not has_indexed_pointer:
        assert reader.calls == []
