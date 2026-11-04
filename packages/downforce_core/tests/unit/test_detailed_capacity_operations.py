"""Offline denial checks exercise real writer boundaries without creating data."""

import importlib.util
import json
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pytest
from downforce_core.detailed import status
from downforce_core.detailed.audit import write_sample_table
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import StorageIntegrityError


def deny_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status.shutil, "disk_usage", lambda _: SimpleNamespace(free=30 * 1024**3))


@pytest.mark.parametrize("writer", ["native", "raw-pack", "canonical"])
def test_low_space_writer_refuses_all_data_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source")
    destination = tmp_path / "destination"
    raw = RawEvidenceStore(destination)
    canonical = DetailedStore(tmp_path, detailed_root=destination)
    deny_disk(monkeypatch)
    with pytest.raises(status.CapacityStop):
        if writer == "native":
            write_sample_table(pd.DataFrame({"x": [1]}), destination / "x.parquet")
        elif writer == "raw-pack":
            raw.pack(source, session_id="2018-01-race", provider="fastf1", provider_version="test")
        else:
            canonical.publish("2018-01-race", {"x": pa.table({"x": [1]})}, {})
    assert not destination.exists()
    assert (source / "data").read_bytes() == b"source"


def test_raw_restore_denial_creates_no_destination_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source")
    store = RawEvidenceStore(tmp_path / "raw")
    bundle = store.pack(
        source, session_id="2018-01-race", provider="fastf1", provider_version="test"
    )
    deny_disk(monkeypatch)
    with pytest.raises(status.CapacityStop):
        store.restore(bundle, tmp_path / "missing" / "restored")
    assert not (tmp_path / "missing").exists()


def test_read_only_store_constructors_do_not_create_or_check_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        status.shutil, "disk_usage", lambda *_: pytest.fail("read-only constructor")
    )
    RawEvidenceStore(tmp_path / "raw")
    DetailedStore(tmp_path, detailed_root=tmp_path / "canonical")
    assert list(tmp_path.iterdir()) == []


def benchmark_module():
    script = Path(__file__).resolve().parents[4] / "scripts" / "benchmark_detailed_storage.py"
    spec = importlib.util.spec_from_file_location("synthetic_benchmark", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("operation", ["benchmark", "parquet", "provider_restore"])
def test_benchmark_low_space_stops_before_temporary_copy_or_provider_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    module = benchmark_module()
    root = tmp_path / "detailed"
    source = root / "preflight" / "2018-01-race" / "attempt" / "raw-cache"
    source.mkdir(parents=True)
    (source / "data").write_bytes(b"raw")
    descriptor = write_sample_table(pd.DataFrame({"x": [1]}), source.parent / "laps.parquet")
    store = RawEvidenceStore(root / "study" / "raw-bundles")
    bundle = store.pack(
        source, session_id="2018-01-race", provider="fastf1", provider_version="test"
    )
    compression = root / "study" / "compression"
    compression.mkdir()
    (compression / "2018-01-race-deflate-6-metrics-v2.json").write_text(
        json.dumps({"bundle_sha256": bundle})
    )
    snapshot = {
        "session_id": "2018-01-race",
        "cache_path": source.relative_to(root).as_posix(),
        "source_manifest": source_manifest(source),
        "logical_bytes": 3,
        "measurement": {
            "fastf1_version": "test",
            "tables": {"laps": descriptor},
            "session": {"season": 2018, "round_number": 1, "fastf1_slot": 5},
        },
    }
    monkeypatch.setattr(module, "freeze", lambda _: [snapshot])
    monkeypatch.setattr(module.fastf1, "get_session", lambda *_: pytest.fail("provider access"))
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    deny_disk(monkeypatch)
    with pytest.raises(status.CapacityStop):
        getattr(module, operation)(root)
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_native_capacity_stop_not_converted_to_failed_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed.sampling import capture_native_tables
    from test_detailed_sampling import loaded_session

    denial = status.CapacityStop("controlled capacity denial")

    def stop(*_):
        raise denial

    monkeypatch.setattr("downforce_core.detailed.sampling.write_sample_table", stop)
    with pytest.raises(status.CapacityStop) as caught:
        capture_native_tables(loaded_session(), tmp_path)
    assert caught.value is denial
    assert list(tmp_path.iterdir()) == []


def test_candidate_capacity_stop_before_publication_keeps_source_and_study_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed.measurement import measure_candidate

    root = tmp_path / "detailed"
    identity = "2018-01-race"
    attempt = root / "preflight" / identity / "attempt"
    cache = attempt / "raw-cache"
    cache.mkdir(parents=True)
    (cache / "source").write_bytes(b"original")
    descriptor = write_sample_table(
        pd.DataFrame({"Speed": [1.25]}), attempt / "car_data/44.parquet"
    )
    measurement = {
        "session": {"session_id": identity, "season": 2018, "session_type": "race"},
        "tables": {"car_data/44": descriptor},
        "fastf1_version": "test",
    }
    (attempt / "measurement.json").write_text(json.dumps(measurement))
    store = RawEvidenceStore(root / "study" / "raw-bundles")
    bundle = store.pack(cache, session_id=identity, provider="fastf1", provider_version="test")
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    deny_disk(monkeypatch)
    with pytest.raises(status.CapacityStop):
        measure_candidate(
            tmp_path,
            root,
            measurement,
            {"round_trip_exact": True, "bundle_sha256": bundle, "observed_peak_temporary_bytes": 0},
        )
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_runner_propagates_typed_capacity_stop_without_next_sample_or_failure_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import downforce_core.detailed.study_runner as module
    from test_detailed_study_runner import inventory, native_session

    root = tmp_path / "detailed"
    inventory(root)
    calls = []

    def loader(*_):
        calls.append("load")
        return native_session()

    def capture(*_):
        raise status.CapacityStop("controlled disk-capacity stop")

    monkeypatch.setattr(module, "capture_native_tables", capture)
    with pytest.raises(status.CapacityStop):
        module.run_study(
            tmp_path,
            detailed_root=root,
            load_session=loader,
            free_bytes=lambda _: 10**12,
            report=lambda _: None,
        )
    assert calls == ["load"]
    assert not (root / "study" / "acquisition-summary.json").exists()


def test_actual_proof_peak_dominates_current_operation_estimates(tmp_path: Path) -> None:
    from downforce_core.detailed.proofs import validated_study_measurements
    from test_detailed_proofs import study

    paths, measured = study(tmp_path)
    actual = validated_study_measurements(paths)[0]
    assert actual["temporary_peak_bytes"] >= 2 * 1024**3
    assert actual["temporary_peak_bytes"] >= actual["conservative_operation_peak_bytes"]
    assert actual["reported_temporary_peak_bytes"] == measured["temporary_peak_bytes"]


@pytest.mark.parametrize("damage", ["missing", "stale", "malformed"])
def test_benchmark_source_rejection_creates_no_data_or_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    module = benchmark_module()
    root = tmp_path / "detailed"
    source = root / "source"
    source.mkdir(parents=True)
    (source / "file").write_bytes(b"original")
    snapshot = {
        "session_id": "2018-01-race",
        "cache_path": "source",
        "source_manifest": source_manifest(source),
        "logical_bytes": 8,
        "measurement": {"tables": {}, "fastf1_version": "test"},
    }
    if damage == "missing":
        (source / "file").unlink()
        source.rmdir()
    elif damage == "stale":
        (source / "file").write_bytes(b"changed")
    else:
        snapshot["source_manifest"]["files"][0]["bytes"] = True
    monkeypatch.setattr(module, "freeze", lambda _: [snapshot])
    monkeypatch.setattr(module.fastf1, "get_session", lambda *_: pytest.fail("provider request"))
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    with pytest.raises((ValueError, StorageIntegrityError)):
        module.benchmark(root, levels=(6,))
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_current_status_replays_artifacts_read_only_and_stale_source_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fastf1
    from downforce_core.detailed.cli import project_study
    from test_detailed_proofs import study

    paths, measured = study(tmp_path)
    project_study(paths)
    monkeypatch.setattr(fastf1.Cache, "requests_get", lambda *_: pytest.fail("HTTP forbidden"))
    before = {path: path.read_bytes() for path in paths.root.rglob("*") if path.is_file()}
    current = status.storage_status(paths)
    assert current["projection_current"] is True
    assert current["current_safety"] == "verified"
    assert {path: path.read_bytes() for path in paths.root.rglob("*") if path.is_file()} == before
    source = (
        paths.root / "preflight" / measured["session_id"] / "attempt" / "raw-cache/evidence.bin"
    )
    source.write_bytes(b"stale")
    stale = status.storage_status(paths)
    assert stale["projection_current"] is False
    assert stale["safe_to_sync"] is False
    assert stale["projection"] == current["projection"]


def test_archive_estimate_exceeds_actual_incompressible_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = random.Random(0).randbytes(256000)
    for number in range(30):
        (source / f"file-{number}").write_bytes(payload[number:] + payload[:number])
    snapshot = source_manifest(source)
    metadata_bytes = len(json.dumps(snapshot).encode("utf-8")) + 4096
    store = RawEvidenceStore(tmp_path / "raw")
    identity = store.pack(
        source, session_id="2018-01-race", provider="fastf1", provider_version="test"
    )
    assert (
        status.archive_peak_bytes(snapshot, metadata_bytes=metadata_bytes)
        > (store.root / f"{identity}.zip").stat().st_size
    )


def test_ready_low_space_runner_does_not_publish_plan_or_create_attempt(tmp_path: Path) -> None:
    import downforce_core.detailed.study_runner as module
    from test_detailed_study_runner import inventory

    root = tmp_path / "detailed"
    inventory(root)
    module.ready_inventory(root)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(status.CapacityStop):
        module.run_study(
            tmp_path,
            detailed_root=root,
            free_bytes=lambda _: 30 * 1024**3,
            load_session=lambda *_: pytest.fail("low disk forbids provider calls"),
            report=lambda _: None,
        )
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not (root / "study").exists()
    assert not (root / "preflight").exists()


@pytest.fixture
def synthetic_benchmark_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = benchmark_module()
    root = tmp_path / "detailed"
    source = root / "preflight/2018-01-race/attempt/raw-cache"
    source.mkdir(parents=True)
    (source / "data").write_bytes(b"raw")
    descriptor = write_sample_table(pd.DataFrame({"x": [1]}), source.parent / "laps.parquet")
    store = RawEvidenceStore(root / "study/raw-bundles")
    bundle = store.pack(
        source, session_id="2018-01-race", provider="fastf1", provider_version="test"
    )
    compression = root / "study/compression"
    compression.mkdir()
    (compression / "2018-01-race-deflate-6-metrics-v2.json").write_text(
        json.dumps({"bundle_sha256": bundle})
    )
    snapshot = {
        "session_id": "2018-01-race",
        "cache_path": source.relative_to(root).as_posix(),
        "source_manifest": source_manifest(source),
        "logical_bytes": 3,
        "measurement": {"fastf1_version": "test", "tables": {"laps": descriptor}},
    }
    monkeypatch.setattr(module, "freeze", lambda _: [snapshot])
    monkeypatch.setattr(module.fastf1, "get_session", lambda *_: pytest.fail("provider access"))
    return module, root


@pytest.mark.parametrize("operation", ["benchmark", "parquet", "provider_restore"])
def test_benchmark_primary_denial_survives_owned_temporary_cleanup_denial(
    synthetic_benchmark_source, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    module, root = synthetic_benchmark_source
    denial = PermissionError(13, "benchmark operation denied", "benchmark-original.tmp")
    denial.winerror = 32
    cleanup_denial = PermissionError(13, "cleanup denied", "cleanup.tmp")
    cleanup_denial.winerror = 5
    monitors = []
    original_monitor = module.WorkingPeak

    def monitor(*args):
        result = original_monitor(*args)
        monitors.append(result)
        return result

    def denied(*args, **kwargs):
        raise denial

    def denied_cleanup(*args, **kwargs):
        raise cleanup_denial

    monkeypatch.setattr(module, "WorkingPeak", monitor)
    if operation == "benchmark":
        monkeypatch.setattr(module.RawEvidenceStore, "pack", denied)
    elif operation == "parquet":
        monkeypatch.setattr(module.pq, "write_table", denied)
    else:
        monkeypatch.setattr(module.RawEvidenceStore, "restore", denied)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(denied_cleanup))
    try:
        with pytest.raises(PermissionError) as caught:
            if operation == "benchmark":
                module.benchmark(root, levels=(1,))
            else:
                getattr(module, operation)(root)
        assert caught.value is denial
        assert caught.value.winerror == 32
        assert caught.value.filename == "benchmark-original.tmp"
        assert any(
            "cleanup" in note and "winerror=5" in note and "cleanup.tmp" in note
            for note in getattr(denial, "__notes__", [])
        )
    finally:
        for running in monitors:
            running.stop()


def test_benchmark_capacity_stop_stops_owned_working_peak_thread(
    synthetic_benchmark_source, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, root = synthetic_benchmark_source
    denial = status.CapacityStop("controlled packing capacity stop")
    monitors = []
    original_monitor = module.WorkingPeak

    def monitor(*args):
        result = original_monitor(*args)
        monitors.append(result)
        return result

    def denied(*args, **kwargs):
        raise denial

    monkeypatch.setattr(module, "WorkingPeak", monitor)
    monkeypatch.setattr(module.RawEvidenceStore, "pack", denied)
    try:
        with pytest.raises(status.CapacityStop) as caught:
            module.benchmark(root, levels=(1,))
        assert caught.value is denial
        assert len(monitors) == 1
        assert monitors[0].done.is_set()
        assert not monitors[0].thread.is_alive()
    finally:
        for running in monitors:
            running.stop()


def test_benchmark_primary_failure_survives_owned_monitor_stop_failure(
    synthetic_benchmark_source, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, root = synthetic_benchmark_source
    denial = status.CapacityStop("controlled packing capacity stop")
    stop_error = OSError(5, "monitor stop failed", "monitor.tmp")
    monitors = []
    stops = []
    original_monitor = module.WorkingPeak

    def monitor(*args):
        result = original_monitor(*args)
        monitors.append(result)
        original_stop = result.stop

        def stop():
            stops.append(result)
            original_stop()
            raise stop_error

        result.stop = stop
        return result

    def denied(*args, **kwargs):
        raise denial

    monkeypatch.setattr(module, "WorkingPeak", monitor)
    monkeypatch.setattr(module.RawEvidenceStore, "pack", denied)
    try:
        with pytest.raises(status.CapacityStop) as caught:
            module.benchmark(root, levels=(1,))
        assert caught.value is denial
        assert stops == monitors
        assert len(monitors) == 1
        assert not monitors[0].thread.is_alive()
        assert any("monitor.tmp" in note for note in getattr(denial, "__notes__", []))
    finally:
        for running in monitors:
            original_monitor.stop(running)


def test_successful_benchmark_stops_monitor_once_and_keeps_source_exact(
    synthetic_benchmark_source, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, root = synthetic_benchmark_source
    source = root / "preflight/2018-01-race/attempt/raw-cache"
    original_source = source_manifest(source)
    monitors = []
    stops = []
    original_monitor = module.WorkingPeak

    def monitor(*args):
        result = original_monitor(*args)
        monitors.append(result)
        original_stop = result.stop

        def stop():
            stops.append(result)
            return original_stop()

        result.stop = stop
        return result

    monkeypatch.setattr(module, "WorkingPeak", monitor)
    module.benchmark(root, levels=(1,))
    assert stops == monitors
    assert len(monitors) == 1
    assert monitors[0].done.is_set()
    assert not monitors[0].thread.is_alive()
    assert source_manifest(source) == original_source
    assert not list((root / "study").glob("compression-*"))
    report = json.loads((root / "study/compression/2018-01-race-deflate-1.json").read_text())
    assert report["round_trip_exact"] is True
