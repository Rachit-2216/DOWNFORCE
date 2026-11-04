"""Operation-local authority receipts: real replay, synthetic transport only."""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from downforce_core.detailed import openf1
from downforce_core.detailed.enrichment_authority import _Admission
from downforce_core.storage.parquet import file_sha256
from test_detailed_independent_finalization import bundle  # noqa: F401
from test_detailed_independent_integration import integration  # noqa: F401
from test_detailed_proofs import save
from test_detailed_schedule_revision import revised  # noqa: F401
from test_detailed_temporal_completion import captured  # noqa: F401
from test_openf1_enrichment import historical_fixture
from test_openf1_safety import response, transport


@pytest.fixture
def admit(request):
    def enter(root, session):
        admission = _Admission(root, session)
        admission.__enter__()
        request.addfinalizer(lambda: admission.__exit__(None, None, None))
        return admission

    return enter


def counted_validator(monkeypatch):
    original = openf1.validated_historical_session
    calls = []

    def counted(root, session):
        calls.append(session["session_id"])
        return original(root, session)

    monkeypatch.setattr(openf1, "validated_historical_session", counted)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel sharing contract")
def test_windows_native_operation_reads_sqlite_and_parquet_while_source_writes_are_denied(
    tmp_path, monkeypatch
):
    assert callable(getattr(_Admission, "__enter__", None)), "admission must own read leases"
    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    cache = root / "reconciliation-cache/fastf1_http_cache.sqlite"
    cache.parent.mkdir()
    with sqlite3.connect(cache) as db:
        db.execute("CREATE TABLE test (value INTEGER)")
        db.execute("INSERT INTO test VALUES (42)")
    db.close()
    source = root / "inventory.json"
    original = source.read_bytes()
    _, requests, _ = transport(monkeypatch, [response(body=b'[{"session_key":1}]')])
    from downforce_core.detailed import openf1_transport

    build = openf1_transport.build_opener

    class Opener:
        def __init__(self, *args):
            self.original = build(*args)

        def open(self, request, timeout):
            with sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True) as db:
                assert db.execute("SELECT value FROM test").fetchone() == (42,)
            db.close()
            with openf1.pq.ParquetFile(
                root / "preflight/2024-01-race/attempt/laps.parquet"
            ) as reader:
                assert reader.read().num_rows == 2
            for mutate in (
                lambda: source.write_bytes(b"changed"),
                source.unlink,
                lambda: source.rename(source.with_suffix(".renamed")),
            ):
                with pytest.raises(OSError) as denied:
                    mutate()
                assert denied.value.winerror == 32 or denied.value.errno == 13
            assert source.read_bytes() == original
            return self.original.open(request, timeout)

    monkeypatch.setattr(openf1_transport, "build_opener", Opener)
    calls = counted_validator(monkeypatch)
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert report["openf1_measured"] and len(requests) == 1 and len(calls) == 2
    source.write_bytes(original)  # The successful operation released its read lease.


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel sharing contract")
def test_close_cancellation_propagates_after_all_handles_release_even_with_primary(tmp_path):
    root, session = historical_fixture(tmp_path)
    with _Admission(root, session) as admission:
        leases = admission._leases
        assert leases is not None
        close = leases.api.close
        calls = []
        count = len(leases._owned)

        def interrupt_once(handle):
            assert close(handle)
            calls.append(handle)
            if len(calls) == 1:
                raise KeyboardInterrupt("cancel during lease release")
            return True

        leases.api.close = interrupt_once
        primary = ValueError("original operation failed")
        with pytest.raises(KeyboardInterrupt, match="cancel during lease release") as cancellation:
            leases.close(primary)
        assert cancellation.value.__cause__ is primary
        assert len(calls) == count and leases.closed and leases._owned == []
        leases.close()
        assert len(calls) == count
    (root / "inventory.json").write_bytes((root / "inventory.json").read_bytes())


def test_gate_clock_rejects_rollback_from_last_success_and_ignores_failed_gate(
    tmp_path, monkeypatch, admit
):
    root, session = historical_fixture(tmp_path)
    base = datetime.now(UTC)
    clock = [base]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(openf1, "datetime", Clock)
    calls = counted_validator(monkeypatch)
    admission = admit(root, session)
    clock[0] = base + timedelta(hours=2)
    admission.check()
    source = root / "inventory.json"
    original = source.stat()
    clock[0] = base + timedelta(hours=3)
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError):
        admission.check()
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    clock[0] = base + timedelta(hours=2)
    admission.check()  # The failed gate must not move the last-success clock.
    clock[0] = base + timedelta(hours=1)
    with pytest.raises(ValueError, match="clock moved backwards"):
        admission.check()
    assert len(calls) == 1  # Every full validator invocation used the real replay.


@pytest.mark.skipif(os.name != "nt", reason="Windows immutable directory attributes")
def test_immutable_directory_archive_attribute_change_is_rejected_under_lease(tmp_path, admit):
    import ctypes
    from ctypes import wintypes

    root, session = historical_fixture(tmp_path)
    admission = admit(root, session)
    path = root / "completion"
    attributes = path.stat(follow_symlinks=False).st_file_attributes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    change = kernel.SetFileAttributesW
    change.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    change.restype = wintypes.BOOL
    assert change(str(path), attributes ^ 0x20)  # ARCHIVE is writable with a read lease held.
    try:
        with pytest.raises(
            ValueError, match="authority directory changed|directory identity changed"
        ):
            admission.check()
    finally:
        assert change(str(path), attributes)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel sharing contract")
@pytest.mark.parametrize("kind", ["writer", "mapping"])
def test_existing_write_capability_prevents_admission_and_releases_partial_leases(
    tmp_path, monkeypatch, kind
):
    import mmap

    from downforce_core.detailed.input_leases import _LeaseSet

    root, session = historical_fixture(tmp_path)
    source = root / "inventory.json"
    original = source.read_bytes()
    instances = []
    initialize = _LeaseSet.__init__

    def track(self, *args, **kwargs):
        instances.append(self)
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(_LeaseSet, "__init__", track)
    writer = source.open("r+b")
    mapping = mmap.mmap(writer.fileno(), 0, access=mmap.ACCESS_WRITE) if kind == "mapping" else None
    if mapping is not None:
        writer.close()
    try:
        with pytest.raises(OSError) as denied:
            with _Admission(root, session):
                pytest.fail("existing write capability must prevent lease admission")
        assert denied.value.winerror == 32
    finally:
        if mapping is not None:
            mapping.close()
        writer.close()
    assert len(instances) == 1 and instances[0].closed and instances[0]._owned == []
    source.write_bytes(original)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel sharing contract")
@pytest.mark.parametrize("phase", ["entry", "request", "normalization", "finish"])
def test_every_operation_failure_releases_all_input_handles(tmp_path, monkeypatch, phase):
    from downforce_core.detailed.input_leases import _LeaseSet

    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    initialize = _LeaseSet.__init__
    instances = []

    def track(self, *args, **kwargs):
        instances.append(self)
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(_LeaseSet, "__init__", track)
    validate = openf1.validated_historical_session
    calls = []

    def replay(path, row):
        result = validate(path, row)
        calls.append(True)
        if phase == "entry" or phase == "finish" and len(calls) == 2:
            raise KeyboardInterrupt("synthetic replay cancellation")
        return result

    monkeypatch.setattr(openf1, "validated_historical_session", replay)
    if phase == "normalization":

        def failed_write(*args, **kwargs):
            raise OSError("synthetic normalization failure")

        monkeypatch.setattr(openf1.pq, "write_table", failed_write)

    class Client:
        def fetch(self, endpoint, session_key):
            if phase == "request":
                raise KeyboardInterrupt("synthetic request cancellation")
            return b'[{"session_key":1}]', "https://api.openf1.org/v1/drivers?session_key=1"

    with pytest.raises(OSError if phase == "normalization" else KeyboardInterrupt):
        openf1.measure_enrichment(root, session, Client())
    assert len(instances) == 1 and instances[0].closed and instances[0]._owned == []
    source = root / "inventory.json"
    source.write_bytes(source.read_bytes())


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel sharing contract")
@pytest.mark.parametrize("damage", ["missing-handle", "wrong-handle", "missing-sha", "closed"])
def test_metadata_fast_path_requires_complete_correct_live_leases(tmp_path, admit, damage):
    from dataclasses import replace

    root, session = historical_fixture(tmp_path)
    admission = admit(root, session)
    leases = admission._leases
    assert leases is not None
    if damage == "missing-handle":
        del leases.files["inventory.json"]
    elif damage == "wrong-handle":
        leases.files["inventory.json"] = replace(
            leases.files["inventory.json"], handle=leases.files["openf1-inventory.json"].handle
        )
    elif damage == "missing-sha":
        admission.inputs.files["inventory.json"] = replace(
            admission.inputs.files["inventory.json"], sha256=None
        )
    else:
        leases.close()
    with pytest.raises((ValueError, OSError)):
        admission.check()


def test_native_operation_replays_full_public_authority_exactly_twice(tmp_path, monkeypatch):
    root, session = historical_fixture(tmp_path)
    calls = counted_validator(monkeypatch)
    _, requests, _ = transport(
        monkeypatch, [response(body=b'[{"session_key":1,"value":1.0000000000001}]')] * 12
    )
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert len(requests) == 12
    assert len(calls) == 2

    class OfflineClient:
        def fetch(self, endpoint, session_key):
            pytest.fail("cached evidence cannot trigger HTTP")

    calls.clear()
    assert openf1.measure_enrichment(root, session, OfflineClient()) == report
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["rewrite", "replace", "missing", "added-proof", "sidecar"])
def test_source_mutation_after_admission_stops_before_publication(tmp_path, monkeypatch, damage):
    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    source = root / "completion" / f"{session['session_id']}.json"
    fetched = []

    class SourceClient:
        def fetch(self, endpoint, session_key):
            fetched.append(endpoint)
            if damage == "rewrite":
                before = source.stat()
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            elif damage == "replace":
                replacement = source.with_suffix(".replacement")
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
            elif damage == "missing":
                source.unlink()
            elif damage == "added-proof":
                directory = root / "completion-temporal"
                directory.mkdir(exist_ok=True)
                (directory / "unexpected.json").write_bytes(b"{}")
            else:
                directory = root / "reconciliation-cache"
                directory.mkdir(exist_ok=True)
                (directory / "fastf1_http_cache.sqlite-wal").write_bytes(b"changed")
            return b'[{"session_key":1}]', "https://api.openf1.org/v1/drivers?session_key=1"

    with pytest.raises((ValueError, OSError)):
        openf1.measure_enrichment(root, session, SourceClient())
    assert fetched == ["drivers"]
    assert not list((root / "study/openf1" / session["session_id"]).glob("*.gz"))


def test_public_validator_stays_fresh_between_operations(tmp_path, monkeypatch):
    root, session = historical_fixture(tmp_path)
    openf1.validated_historical_session(root, session)
    source = root / "completion" / f"{session['session_id']}.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        openf1.validated_historical_session(root, session)


def test_caller_session_mutation_is_rejected_at_the_next_publication_gate(tmp_path, monkeypatch):
    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))

    class Client:
        def fetch(self, endpoint, session_key):
            session["event_name"] = "Changed caller identity"
            return b'[{"session_key":1}]', "https://api.openf1.org/v1/drivers?session_key=1"

    with pytest.raises(ValueError):
        openf1.measure_enrichment(root, session, Client())
    assert not list((root / "study/openf1" / session["session_id"]).glob("*.gz"))


@pytest.mark.parametrize(
    "relative",
    [
        "inventory.json",
        "openf1-inventory.json",
        "reconciled-inventory.json",
        "study/sample-plan.json",
        "preflight/2024-01-race/measurement.json",
        "preflight/2024-01-race/attempt/measurement.json",
        "preflight/2024-01-race/attempt/laps.parquet",
        "preflight/2024-01-race/attempt/raw-cache/evidence.bin",
        "study/session-measurements/2024-01-race.json",
        "study/compression/2024-01-race-deflate-6-metrics-v2.json",
        "candidate",
        "raw-bundle",
    ],
)
def test_receipt_hashes_complete_target_and_source_bytes_even_with_restored_mtime(
    tmp_path, relative, admit
):
    root, session = historical_fixture(tmp_path)
    admission = admit(root, session)
    if relative == "candidate":
        path = next((root / "study/candidates/canonical").rglob("laps.parquet"))
    elif relative == "raw-bundle":
        path = next((root / "study/raw-bundles").glob("*.zip"))
    else:
        path = root / relative
    before = path.stat()
    raw = path.read_bytes()
    if os.name == "nt":
        with pytest.raises(OSError):
            path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        assert path.read_bytes() == raw
        admission.check()
        return
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="authority input changed"):
        admission.check()


def test_completed_operation_never_returns_without_fresh_final_validator(tmp_path, monkeypatch):
    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    original = openf1.validated_historical_session
    calls = []

    def validator(path, row):
        calls.append(True)
        result = original(path, row)
        if len(calls) == 2:
            source = root / "inventory.json"
            source.write_bytes(source.read_bytes() + b" ")
        return result

    monkeypatch.setattr(openf1, "validated_historical_session", validator)

    class Client:
        def fetch(self, endpoint, session_key):
            return b'[{"session_key":1}]', "https://api.openf1.org/v1/drivers?session_key=1"

    with pytest.raises((ValueError, OSError)):
        openf1.measure_enrichment(root, session, Client())
    assert len(calls) == 2


@pytest.mark.parametrize("direction", ["backwards", "expired-future"])
def test_receipt_rechecks_current_clock_and_catalog_future_deadlines(
    tmp_path, monkeypatch, direction, admit
):
    root, session = historical_fixture(tmp_path)
    clock = [datetime.now(UTC)]
    discovery = json.loads((root / "inventory.json").read_bytes())
    future = {
        **discovery["sessions"][0],
        "session_id": "2024-02-race",
        "scheduled_at": (clock[0] + timedelta(hours=1)).isoformat(),
        "completion": "future",
    }
    discovery["sessions"].append(future)
    save(root / "inventory.json", discovery)
    save(
        root / "completion/2024-02-race.json",
        {"session_id": future["session_id"], "state": "future"},
    )
    catalog = json.loads((root / "reconciled-inventory.json").read_bytes())
    catalog["discovery_sha256"] = file_sha256(root / "inventory.json")
    catalog["sessions"].append({**future, "state": "future"})
    catalog["completion_evidence_sha256"][future["session_id"]] = file_sha256(
        root / "completion/2024-02-race.json"
    )
    save(root / "reconciled-inventory.json", catalog)
    plan = json.loads((root / "study/sample-plan.json").read_bytes())
    plan["inventory_sha256"] = file_sha256(root / "reconciled-inventory.json")
    save(root / "study/sample-plan.json", plan)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(openf1, "datetime", Clock)
    admission = admit(root, session)
    clock[0] += timedelta(hours=-1 if direction == "backwards" else 2)
    with pytest.raises(ValueError, match="backwards|future exclusion expired"):
        admission.check()


def test_public_paid_window_uses_fully_replayed_revised_provider_end(revised, monkeypatch, admit):  # noqa: F811
    from test_detailed_proofs import candidate_fixture
    from test_detailed_study_runner import run
    from test_detailed_temporal_completion import proof_native, read, ready, temporal

    root, entries = revised
    catalog, _ = ready(revised)
    proofs, _ = temporal().load_temporal_completions(root, entries)
    assert run(root, lambda entry, _cache: proof_native(proofs[entry["session_id"]]))["failed"] == 0
    session = catalog["sessions"][0]
    identity = session["session_id"]
    primary = read(root, f"preflight/{identity}/measurement.json")
    attempt = next(
        path.parent
        for path in (root / f"preflight/{identity}").glob("*/measurement.json")
        if json.loads(path.read_bytes()) == primary
    )
    candidate_fixture(root, primary, attempt)
    actual_end = datetime.fromisoformat(proofs[identity]["provider_session"]["date_end"])
    clock = [actual_end + timedelta(minutes=10)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(openf1, "datetime", Clock)
    with pytest.raises(ValueError, match="paid/live"):
        openf1.validated_historical_session(root, session)

    class OfflineClient:
        def fetch(self, endpoint, session_key):
            pytest.fail("paid/live cutoff must stop before HTTP")

    with pytest.raises(ValueError, match="paid/live"):
        openf1.measure_enrichment(root, session, OfflineClient())
    clock[0] = actual_end + timedelta(minutes=30, microseconds=1)
    assert openf1.validated_historical_session(root, session) == session
    admission = admit(root, session)
    assert admission.end == actual_end
    admission.check()


def test_existing_runtime_container_replacement_is_not_waived_as_mutable_budget_state(
    tmp_path, admit
):
    root, session = historical_fixture(tmp_path)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    outputs = root / "study/openf1"
    outputs.mkdir(exist_ok=True)
    admission = admit(root, session)
    if os.name == "nt":
        with pytest.raises(OSError):
            runtime.rename(outputs / "old-runtime")
        admission.check()
        return
    runtime.rename(outputs / "old-runtime")
    runtime.mkdir()
    with pytest.raises(ValueError, match="authority directory changed: runtime"):
        admission.check()


def test_collector_covers_fully_replayed_independent_finality_dependencies(integration):  # noqa: F811
    from downforce_core.detailed.enrichment_authority import _collect
    from test_detailed_independent_integration import finalize_ready

    root, entry, proof = integration
    finalize_ready(integration)
    inputs = _collect(root, entry["session_id"])
    assert set(proof["dependencies"]) <= set(inputs.files)
    assert proof["wire"]["cache_path"] in inputs.files
    for suffix in ("-wal", "-shm", "-journal"):
        assert proof["wire"]["cache_path"] + suffix in inputs.files


def test_collector_follows_fully_replayed_legacy_fallback_native_paths_for_other_sample(tmp_path):
    import pandas as pd
    from downforce_core.detailed.enrichment_authority import _collect
    from test_detailed_evidence import evidence, fallback_root

    entries = fallback_root(tmp_path)
    entry = entries[0]
    identity = entry["session_id"]
    source_path = tmp_path / f"completion/{identity}.json"
    source = json.loads(source_path.read_bytes())
    source["times"] = []
    save(source_path, source)
    attempt = tmp_path / f"preflight/{identity}/original"
    attempt.mkdir(parents=True)
    table = attempt / "session_status.parquet"
    pd.DataFrame(
        {
            "source_index": [0, 1],
            "Time": pd.to_timedelta([1, 2], unit="s"),
            "Status": ["Started", "Ends"],
        }
    ).to_parquet(table, index=False)
    measurement = {
        "session": entry,
        "completion_confirmed": True,
        "session_statuses": ["Started", "Ends"],
        "tables": {
            "session_status": {
                "sha256": file_sha256(table),
                "bytes": table.stat().st_size,
                "rows": 2,
            }
        },
    }
    save(attempt / "measurement.json", measurement)
    save(attempt.parent / "measurement.json", measurement)
    proof = evidence().build_index_fallback(tmp_path)
    assert evidence().validate_index_fallback(tmp_path, proof) == proof
    relative = "evidence-repair/index-fallback-2022.json"
    save(tmp_path / relative, proof)
    catalog = json.loads((tmp_path / "reconciled-inventory.json").read_bytes())
    catalog["index_fallback_reports"] = {
        "2022": {"path": relative, "sha256": file_sha256(tmp_path / relative)}
    }
    save(tmp_path / "reconciled-inventory.json", catalog)
    inputs = _collect(tmp_path, "2024-01-race")
    native = proof["completion_sources"][identity]["native"]
    assert {
        native[field]
        for field in (
            "native_path",
            "published_measurement_path",
            "authoritative_attempt_measurement_path",
        )
    } <= set(inputs.files)
    assert set(proof["historical_403_records"]) <= set(inputs.files)
    assert relative in inputs.files
    assert "discovery-cache/fastf1_http_cache.sqlite-wal" in inputs.files
