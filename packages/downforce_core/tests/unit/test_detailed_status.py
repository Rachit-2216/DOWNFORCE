import tempfile
from pathlib import Path

import pytest
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.status import guard_free_space, storage_status


@pytest.mark.parametrize("target_kind", ["file", "missing-child", "file-parent"])
def test_capacity_anchor_is_nearest_existing_unlinked_directory(tmp_path, target_kind):
    from downforce_core.detailed import status

    directory = tmp_path / "anchor"
    directory.mkdir()
    artifact = directory / "artifact.json"
    artifact.write_text("{}")
    target = {
        "file": artifact,
        "missing-child": directory / "not-created" / "future.json",
        "file-parent": artifact / "not-created" / "future.json",
    }[target_kind]
    assert status.filesystem_ancestor(target) == directory
    # Always supply a directory; a long synthetic gzip target previously failed
    # this host's file-target volume query with WinError267.
    status.guard_operation(target, next_peak_bytes=0, operation="existing artifact")
    assert not (directory / "not-created").exists()


def test_guard_queries_actual_windows_volume_for_an_existing_file(tmp_path):
    from downforce_core.detailed.status import guard_operation

    artifact = tmp_path / "existing.json"
    artifact.write_text("{}")
    guard_operation(artifact, next_peak_bytes=0, operation="existing file volume query")


def test_status_is_read_only_and_reports_components(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, environ={})
    paths.raw.mkdir(parents=True)
    (paths.raw / "source.zip").write_bytes(b"raw")
    paths.canonical.mkdir()
    (paths.canonical / "table.parquet").write_bytes(b"canonical")
    result = storage_status(paths)
    assert result["persistent_raw_bytes"] == 3
    assert result["canonical_bytes"] == 9
    assert result["used_detailed_bytes"] == 12
    assert result["safe_to_sync"] is False
    assert not paths.cache.exists()


def test_missing_data_root_status_does_not_create_it(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, environ={})
    assert storage_status(paths)["used_detailed_bytes"] == 0
    assert not paths.root.exists()


def test_next_batch_guard_includes_temporary_peak_and_reserve() -> None:
    guard_free_space(free_bytes=100, next_peak_bytes=50, reserve_bytes=50)
    with pytest.raises(ValueError, match="space"):
        guard_free_space(free_bytes=99, next_peak_bytes=50, reserve_bytes=50)
    with pytest.raises(ValueError):
        guard_free_space(free_bytes=100, next_peak_bytes=-1, reserve_bytes=50)


def test_status_counts_provider_caches_and_reports_nonadditive_components(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, environ={})
    for directory, body in (
        (paths.cache, b"cache"),
        (paths.root / "discovery-cache", b"discovery"),
        (paths.root / "reconciliation-cache", b"status"),
        (paths.root / "preflight" / "2018-01-race" / "attempt" / "raw-cache", b"source"),
    ):
        directory.mkdir(parents=True)
        (directory / "proof.bin").write_bytes(body)
    result = storage_status(paths)
    assert result["cache_bytes"] == 26
    assert result["preflight_provider_cache_bytes"] == 6
    assert result["components_are_additive"] is False
    assert result["filesystem"]


def test_operation_guard_checks_nearest_existing_ancestor_without_creating_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from downforce_core.detailed import status

    queried = []

    def disk(path):
        queried.append(path)
        return SimpleNamespace(free=50 * 1024**3 + 100)

    monkeypatch.setattr(status.shutil, "disk_usage", disk)
    target = tmp_path / "missing" / "data"
    status.guard_operation(target, next_peak_bytes=100, operation="fixture")
    assert queried == [tmp_path]
    assert not target.parent.exists()
    with pytest.raises(status.CapacityStop, match="headroom"):
        status.guard_operation(target, next_peak_bytes=101, operation="fixture")
    assert not target.parent.exists()


@pytest.mark.parametrize("peak", [-1, True, 1.5, None, "100"])
def test_operation_guard_invalid_estimate_fails_before_filesystem_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peak
) -> None:
    from downforce_core.detailed import status

    monkeypatch.setattr(status.shutil, "disk_usage", lambda *_: pytest.fail("invalid estimate"))
    with pytest.raises(status.CapacityStop):
        status.guard_operation(tmp_path / "missing", next_peak_bytes=peak, operation="fixture")


def test_operation_guard_rejects_linked_ancestor_before_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed import status
    from downforce_core.exceptions import StorageIntegrityError

    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == tmp_path or original(path))
    monkeypatch.setattr(status.shutil, "disk_usage", lambda *_: pytest.fail("linked ancestor"))
    with pytest.raises(StorageIntegrityError):
        status.guard_operation(tmp_path / "missing", next_peak_bytes=1, operation="fixture")


def test_parquet_estimate_accounts_for_dictionary_expansion_and_metadata() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from downforce_core.detailed import status

    values = pa.array(["long-value" * 1000] * 100).dictionary_encode()
    table = pa.table({"labels": values}).replace_schema_metadata({b"large": b"x" * 10000})
    bound = status.parquet_peak_bytes(table, row_group_size=8)
    for dictionary in (True, False):
        output = pa.BufferOutputStream()
        pq.write_table(
            table,
            output,
            compression="zstd",
            use_dictionary=dictionary,
            row_group_size=8,
            use_compliant_nested_type=False,
        )
        assert bound > output.getvalue().size
    assert bound > table.nbytes


def test_cached_projection_is_historical_without_current_bound_artifacts(tmp_path: Path) -> None:
    import json

    paths = DetailedPaths(tmp_path, environ={})
    paths.root.mkdir(parents=True)
    historical = {"safe_to_sync": True, "stress_with_margin_bytes": 1, "peak_working_bytes": 1}
    (paths.root / "stratified-projection.json").write_text(json.dumps(historical))
    result = storage_status(paths)
    assert result["projection"] == historical
    assert result["safe_to_sync"] is False
    assert result["projection_current"] is False
    assert result["current_safety"] == "stale-or-unverified"


def test_source_archive_copy_and_provider_bounds_include_file_overhead() -> None:
    from downforce_core.detailed import status

    snapshot = {"files": [{"path": "cache.bin", "bytes": 100}], "directories": []}
    assert status.copy_peak_bytes(snapshot) > 100
    assert status.archive_peak_bytes(snapshot, metadata_bytes=100) > 200
    assert status.provider_peak_bytes(snapshot, native_peak_bytes=1000) > 1100
    for value in (True, -1, "100"):
        with pytest.raises(status.CapacityStop):
            status.copy_peak_bytes(
                {"files": [{"path": "cache", "bytes": value}], "directories": []}
            )


def test_revision_peak_includes_table_and_manifest_overhead() -> None:
    import pyarrow as pa
    from downforce_core.detailed import status

    table = pa.table({"x": [1, 2]})
    peak = status.parquet_peak_bytes(table)
    assert status.revision_peak_bytes([peak], metadata_bytes=100, schema_bytes=100) > peak


@pytest.mark.parametrize("field", ["temporary_peak_bytes", "round_trip_exact"])
def test_current_projection_rejects_equal_valued_json_type_aliases(
    tmp_path: Path, field: str
) -> None:
    import json

    from downforce_core.detailed.cli import project_study
    from test_detailed_proofs import study

    paths, _ = study(tmp_path)
    project_study(paths)
    assert storage_status(paths)["projection_current"] is True
    projection_path = paths.root / "stratified-projection.json"
    projection = json.loads(projection_path.read_text())
    value = projection["measurements"][0][field]
    projection["measurements"][0][field] = float(value) if field == "temporary_peak_bytes" else 1
    projection_path.write_text(json.dumps(projection))
    result = storage_status(paths)
    assert result["projection"] == projection
    assert result["projection_current"] is False
    assert result["safe_to_sync"] is False
    assert result["current_safety"] == "stale-or-unverified"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_current_projection_rejects_nonfinite_historical_measurement(
    tmp_path: Path, value: float
) -> None:
    import json

    from downforce_core.detailed.cli import project_study
    from test_detailed_proofs import study

    paths, _ = study(tmp_path)
    project_study(paths)
    projection_path = paths.root / "stratified-projection.json"
    projection = json.loads(projection_path.read_text())
    projection["measurements"][0]["temporary_peak_bytes"] = value
    projection_path.write_text(json.dumps(projection))
    result = storage_status(paths)
    assert result["projection_current"] is False
    assert result["current_projection"] is None
    assert result["safe_to_sync"] is False
    assert result["current_safety"] == "stale-or-unverified"
    assert result["current_safety_error"]


def test_owned_temporary_directory_success_cleans_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed import status

    assert hasattr(status, "owned_temporary_directory")
    original_cleanup = tempfile.TemporaryDirectory._rmtree
    cleanups = []

    def cleanup(path, *args, **kwargs):
        cleanups.append(Path(path))
        original_cleanup(path, *args, **kwargs)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(cleanup))
    with status.owned_temporary_directory(prefix="owned-", directory=tmp_path) as working:
        assert working.is_dir()
        (working / "data").write_bytes(b"owned temporary only")
    assert cleanups == [working]
    assert not working.exists()


@pytest.mark.parametrize("primary_kind", ["none", "permission", "capacity", "interrupt"])
def test_owned_temporary_cleanup_preserves_active_primary_or_cleanup_only_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary_kind: str
) -> None:
    from downforce_core.detailed import status

    assert hasattr(status, "owned_temporary_directory")
    cleanup_error = OSError(5, "cleanup failed", "cleanup.tmp")
    cleanup_error.winerror = 5
    primary = {
        "none": None,
        "permission": PermissionError(13, "primary denied", "primary.tmp"),
        "capacity": status.CapacityStop("primary capacity stop"),
        "interrupt": KeyboardInterrupt("primary cancellation"),
    }[primary_kind]
    cleanups = []

    def cleanup(path, *args, **kwargs):
        cleanups.append(Path(path))
        raise cleanup_error

    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(cleanup))
    expected = primary if primary is not None else cleanup_error
    with pytest.raises(type(expected)) as caught:
        with status.owned_temporary_directory(prefix="owned-", directory=tmp_path) as working:
            if primary is not None:
                raise primary
    assert caught.value is expected
    assert cleanups == [working]
    if primary is not None:
        assert any(
            "cleanup" in note and "winerror=5" in note and "cleanup.tmp" in note
            for note in getattr(primary, "__notes__", [])
        )


def test_owned_temporary_cleanup_cancellation_is_not_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed import status

    assert hasattr(status, "owned_temporary_directory")
    cancellation = KeyboardInterrupt("cleanup cancelled")

    def cleanup(*args, **kwargs):
        raise cancellation

    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(cleanup))
    with pytest.raises(KeyboardInterrupt) as caught:
        with status.owned_temporary_directory(prefix="owned-", directory=tmp_path):
            raise PermissionError("primary denied")
    assert caught.value is cancellation
