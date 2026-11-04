"""Proof-aware reconciliation and acquisition using real synthetic offline replay."""

import gc
from copy import deepcopy

import pandas as pd
import pytest
from downforce_core.detailed.independent_finalization import (
    BASIS,
    build_independent_finalization,
)
from downforce_core.detailed.reconciliation import finalize_inventory, reclassify_evidence
from downforce_core.detailed.study_runner import ready_inventory
from test_detailed_evidence import finalizer
from test_detailed_independent_finalization import CAPTURE, IDENTITY, read, save
from test_detailed_independent_finalization import bundle as bundle
from test_detailed_study_runner import inventory, native_session, run, runner


@pytest.fixture
def integration(bundle):
    root, entry = bundle
    proof = build_independent_finalization(root, entry, CAPTURE)
    save(root, f"completion-finalization/{IDENTITY}.json", proof)
    save(root, "openf1-inventory.json", {"sessions": [], "failures": []})
    save(root, "reconciled-inventory.json", {"complete": True, "index_failures": []})
    return root, entry, proof


def test_pure_narrow_promotion_preserves_every_strict_field(integration):
    root, entry, proof = integration
    source = read(root, f"completion-derived/{IDENTITY}.json")
    original = deepcopy(source)
    assert reclassify_evidence(entry, source, {"Status": "Complete"})["state"] == "unverified"
    result = reclassify_evidence(
        entry, source, {"Status": "Complete"}, independent_finalization=proof
    )
    assert result["state"] == "completed"
    assert result["completion_basis"] == BASIS
    assert result["chronology_valid"] is False
    assert result["source_classification"] == "unverified"
    assert all(result[key] == value for key, value in source.items() if key != "state")
    assert len(result["statuses"]) == 14 and result["statuses"][-1] == "Started"
    assert result["completion_integrity"] == "ambiguous"
    assert source == original


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("entry", "session_id", "2020-01-practice-2"),
        ("entry", "event_name", "Styrian Grand Prix"),
        ("entry", "completion", "future"),
        ("entry", "completion", "cancelled"),
        ("entry", "scheduled_at", "2999-01-01T00:00:00+00:00"),
        ("entry", "fastf1_supported", False),
        ("source", "completion_integrity", "invalid"),
        ("source", "cancelled", True),
        ("source", "statuses", ["Started", "Aborted"]),
        ("source", "source_completion_sha256", "forged"),
        ("proof", "basis", "archive-only"),
        ("proof", "chronology_valid", True),
        ("proof", "schema_version", "unknown"),
        ("proof", "state", "unverified"),
        ("proof", "time_nanoseconds", [1] * 14),
        ("proof", "source_classification", "completed"),
    ],
)
def test_pure_forged_or_ineligible_proof_rejected(integration, target, key, value):
    root, entry, proof = integration
    values = {
        "entry": deepcopy(entry),
        "source": read(root, f"completion-derived/{IDENTITY}.json"),
        "proof": deepcopy(proof),
    }
    values[target][key] = value
    with pytest.raises(ValueError):
        reclassify_evidence(
            values["entry"], values["source"], independent_finalization=values["proof"]
        )


def finalize_ready(integration):
    root, _, _ = integration
    result = finalizer().finalize(root, offline=True)
    catalog, _ = ready_inventory(root)
    assert catalog == result
    return result


def test_offline_finalizer_replays_proof_without_mutating_sources(integration, monkeypatch):
    root, _, _ = integration
    monkeypatch.setattr(
        finalizer().fastf1.Cache,
        "requests_get",
        lambda *_args, **_kwargs: pytest.fail("offline HTTP forbidden"),
    )
    immutable = {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != "reconciled-inventory.json"
    }
    first = finalize_ready(integration)
    assert first["sessions"][0]["completion_basis"] == BASIS
    assert set(first["completion_finalization_sha256"]) == {IDENTITY}
    digest = (root / "reconciled-inventory.json").read_bytes()
    assert finalize_ready(integration) == first
    assert (root / "reconciled-inventory.json").read_bytes() == digest
    assert all(path.read_bytes() == value for path, value in immutable.items())


@pytest.mark.parametrize(
    "problem",
    [
        "map-missing",
        "map-stale",
        "proof-missing",
        "capture-missing",
        "body-changed",
        "cache-missing",
        "source-changed",
        "derived-changed",
        "state-forged",
        "basis-forged",
        "chronology-forged",
        "integrity-forged",
        "status-forged",
        "unknown-proof-flag",
    ],
)
def test_ready_rejects_stale_dependencies_and_canonical_forgery(integration, problem):
    root, _, _ = integration
    finalize_ready(integration)
    catalog = read(root, "reconciled-inventory.json")
    row = catalog["sessions"][0]
    if problem == "map-missing":
        catalog.pop("completion_finalization_sha256")
    elif problem == "map-stale":
        catalog["completion_finalization_sha256"][IDENTITY] = "stale"
    elif problem.endswith("missing"):
        gc.collect()  # Release SQLite context-manager connections on Windows.
        relative = {
            "proof-missing": f"completion-finalization/{IDENTITY}.json",
            "capture-missing": CAPTURE,
            "cache-missing": "reconciliation-cache/fastf1_http_cache.sqlite",
        }[problem]
        (root / relative).unlink()
    elif problem in {"body-changed", "source-changed", "derived-changed"}:
        relative = {
            "body-changed": "evidence-requests/fia/response.html",
            "source-changed": f"completion/{IDENTITY}.json",
            "derived-changed": f"completion-derived/{IDENTITY}.json",
        }[problem]
        with (root / relative).open("ab") as stream:
            stream.write(b" ")
    else:
        key, value = {
            "state-forged": ("state", "cancelled"),
            "basis-forged": ("completion_basis", "archive"),
            "chronology-forged": ("chronology_valid", True),
            "integrity-forged": ("completion_integrity", "verified"),
            "status-forged": ("statuses", ["Started", "Finalised"]),
            "unknown-proof-flag": ("basis", BASIS),
        }[problem]
        row[key] = value
    save(root, "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError):
        ready_inventory(root)


def proof_session(proof):
    session = native_session()
    session.session_status = pd.DataFrame(
        {"Status": proof["statuses"], "Time": pd.to_timedelta(proof["time_nanoseconds"], unit="ns")}
    )
    return session


def test_fresh_capture_and_existing_reuse_follow_independent_rule(integration):
    root, _, proof = integration
    finalize_ready(integration)
    result = run(root, lambda *_: proof_session(proof))
    assert result["new_writes"] == 1 and result["failed"] == 0
    measurement = read(root, f"preflight/{IDENTITY}/measurement.json")
    assert measurement["completion_basis"] == BASIS
    assert measurement["chronology_valid"] is False
    assert (
        measurement["completion_finalization_sha256"]
        == read(root, "reconciled-inventory.json")["completion_finalization_sha256"][IDENTITY]
    )
    assert measurement["session_statuses"] == proof["statuses"]
    assert run(root, lambda *_: pytest.fail("reuse must not load"))["reused"] == 1


@pytest.mark.parametrize("change", ["drop", "add", "reorder", "status", "time"])
def test_fresh_native_must_match_all_wire_records_exactly(integration, change):
    root, _, proof = integration
    finalize_ready(integration)
    session = proof_session(proof)
    frame = session.session_status
    if change == "drop":
        session.session_status = frame.iloc[:-1]
    elif change == "add":
        session.session_status = pd.concat([frame, frame.iloc[-1:]], ignore_index=True)
    elif change == "reorder":
        session.session_status = frame.iloc[::-1]
    elif change == "status":
        frame.loc[13, "Status"] = "Finalised"
    else:
        frame.loc[13, "Time"] += pd.Timedelta(1, unit="ns")
    result = run(root, lambda *_: session)
    assert result["failed"] == 1
    assert not (root / f"preflight/{IDENTITY}/measurement.json").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("completion_basis", "archive"),
        ("chronology_valid", True),
        ("completion_finalization_sha256", "stale"),
    ],
)
def test_reuse_rejects_mismatched_independent_metadata(integration, key, value):
    root, _, proof = integration
    finalize_ready(integration)
    run(root, lambda *_: proof_session(proof))
    measurement = read(root, f"preflight/{IDENTITY}/measurement.json")
    measurement[key] = value
    for path in (root / f"preflight/{IDENTITY}").rglob("measurement.json"):
        save(root, path.relative_to(root).as_posix(), measurement)
    with pytest.raises(ValueError):
        run(root, lambda *_: pytest.fail("metadata must reject before provider"))


def test_captured_journal_resume_uses_same_proof_rule(integration, monkeypatch):
    root, _, proof = integration
    finalize_ready(integration)
    original_publish = runner()._publish_once
    monkeypatch.setattr(
        runner(), "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda *_: proof_session(proof))
    assert read(root, "study/acquisition-journal.json")["sessions"][IDENTITY]["state"] == "captured"
    monkeypatch.setattr(runner(), "_publish_once", original_publish)
    assert run(root, lambda *_: pytest.fail("resume must not load"))["new_writes"] == 1


def test_independent_reuse_requires_status_metadata(integration):
    root, _, proof = integration
    finalize_ready(integration)
    run(root, lambda *_: proof_session(proof))
    measurement = read(root, f"preflight/{IDENTITY}/measurement.json")
    measurement.pop("session_statuses")
    for path in (root / f"preflight/{IDENTITY}").rglob("measurement.json"):
        save(root, path.relative_to(root).as_posix(), measurement)
    with pytest.raises(ValueError, match="metadata"):
        run(root, lambda *_: pytest.fail("missing exact metadata must reject"))


@pytest.mark.parametrize("change", ["status", "time", "source_index"])
def test_saved_native_cannot_relabel_checksums_to_bypass_independent_parity(
    integration, monkeypatch, change
):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from downforce_core.storage.parquet import file_sha256

    root, _, proof = integration
    finalize_ready(integration)
    publish = runner()._publish_once
    monkeypatch.setattr(
        runner(), "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda *_: proof_session(proof))
    monkeypatch.setattr(runner(), "_publish_once", publish)
    journal = read(root, "study/acquisition-journal.json")
    record = journal["sessions"][IDENTITY]
    path = root / record["attempt"] / "session_status.parquet"
    frame = pq.read_table(path).to_pandas()
    if change == "status":
        frame.loc[13, "Status"] = "Finalised"
    elif change == "time":
        frame.loc[13, "Time"] += pd.Timedelta(1, unit="ns")
    else:
        frame.loc[13, "source_index"] = 0
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path)
    record["measurement"]["tables"]["session_status"].update(
        sha256=file_sha256(path), bytes=path.stat().st_size
    )
    save(root, "study/acquisition-journal.json", journal)
    with pytest.raises(ValueError, match="native completion"):
        run(root, lambda *_: pytest.fail("saved forged native capture must reject before provider"))
    assert not (root / f"preflight/{IDENTITY}/measurement.json").exists()


@pytest.mark.parametrize("gate", ["loader", "capture", "resume", "reuse"])
def test_current_proof_is_rechecked_at_native_io_gates(integration, monkeypatch, gate):
    root, _, proof = integration
    finalize_ready(integration)
    path = root / f"completion-finalization/{IDENTITY}.json"

    def stale():
        path.write_text(path.read_text() + " ")

    def load(*_):
        if gate == "loader":
            stale()
        return proof_session(proof)

    if gate == "capture":
        capture = runner().capture_native_tables

        def stale_capture(*args):
            result = capture(*args)
            stale()
            return result

        monkeypatch.setattr(runner(), "capture_native_tables", stale_capture)
    if gate == "resume":
        publish = runner()._publish_once
        monkeypatch.setattr(
            runner(), "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        with pytest.raises(KeyboardInterrupt):
            run(root, load)
        monkeypatch.setattr(runner(), "_publish_once", publish)
        stale()
    elif gate == "reuse":
        assert run(root, load)["new_writes"] == 1
        stale()
    if gate in {"resume", "reuse"}:
        with pytest.raises(ValueError):
            run(root, lambda *_: pytest.fail("stale proof must reject before provider"))
    else:
        assert run(root, load)["failed"] == 1
        assert not (root / f"preflight/{IDENTITY}/measurement.json").exists()


def test_archive_alone_stays_unverified_without_separate_proof(integration):
    root, _, _ = integration
    (root / f"completion-finalization/{IDENTITY}.json").unlink()
    result = finalizer().finalize(root, offline=True)
    assert result["sessions"][0]["state"] == "unverified"
    assert result["completion_finalization_sha256"] == {}
    assert "completion_basis" not in result["sessions"][0]
    with pytest.raises(ValueError, match="unverified"):
        ready_inventory(root)


@pytest.mark.parametrize(
    "key,value",
    [
        ("completion_basis", BASIS),
        ("chronology_valid", False),
        ("basis", BASIS),
        ("completion_finalization_sha256", "forged"),
        ("completion_warning", "proof"),
    ],
)
def test_ordinary_inventory_rejects_independent_markers_without_proof(tmp_path, key, value):
    root = tmp_path / "ordinary"
    inventory(root, count=1)
    catalog = read(root, "reconciled-inventory.json")
    catalog["sessions"][0][key] = value
    save(root, "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError, match="markers"):
        ready_inventory(root)


def test_reconciliation_rejects_independent_proof_with_provider_cancellation(integration):
    from datetime import UTC, datetime

    root, entry, proof = integration
    source = read(root, f"completion-derived/{IDENTITY}.json")
    with pytest.raises(ValueError, match="cancellation"):
        finalize_inventory(
            read(root, "inventory.json"),
            {IDENTITY: source},
            [
                {
                    "session_key": 1,
                    "year": 2020,
                    "session_name": "Practice 1",
                    "date_start": entry["scheduled_at"],
                    "is_cancelled": True,
                }
            ],
            now=datetime.now(UTC),
            independent_finalizations={IDENTITY: proof},
        )


def test_refreshing_source_checksum_cannot_hide_changed_proof_dependency(integration):
    from downforce_core.storage.parquet import file_sha256

    root, _, _ = integration
    finalize_ready(integration)
    path = root / f"completion/{IDENTITY}.json"
    source = read(root, path.relative_to(root).as_posix())
    source["state"] = "completed"
    save(root, path.relative_to(root).as_posix(), source)
    catalog = read(root, "reconciled-inventory.json")
    catalog["completion_evidence_sha256"][IDENTITY] = file_sha256(path)
    save(root, "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError):
        ready_inventory(root)


@pytest.mark.parametrize(
    "key", ["dependencies", "classification", "archive", "capture_path", "discovery_sha256"]
)
def test_pure_rejects_incomplete_proof_schema(integration, key):
    root, entry, proof = integration
    incomplete = deepcopy(proof)
    incomplete.pop(key)
    with pytest.raises(ValueError):
        reclassify_evidence(
            entry,
            read(root, f"completion-derived/{IDENTITY}.json"),
            independent_finalization=incomplete,
        )


@pytest.mark.parametrize(
    "problem",
    [
        "null-rows",
        "missing-row-field",
        "nonstring-row-field",
        "wrong-rank",
        "duplicate-driver",
        "zero-laps",
        "invalid-time",
        "extra-classification-field",
        "float-season",
        "float-driver-count",
        "truncated-archive",
        "empty-archive-url",
        "invalid-archive-hex",
        "invalid-archive-digest",
        "nondatetime-archive-field",
        "elapsed-dict",
        "elapsed-bool",
        "elapsed-nan",
        "elapsed-inf",
        "elapsed-negative",
        "elapsed-perturbed",
    ],
)
def test_pure_rejects_malformed_nested_schema_and_elapsed(integration, problem):
    root, entry, proof = integration
    malformed = deepcopy(proof)
    classification = malformed["classification"]
    row = classification["rows"][0]
    archive = malformed["archive"]
    if problem == "null-rows":
        classification["rows"] = [None] * 20
    elif problem == "missing-row-field":
        row.pop("time")
    elif problem == "nonstring-row-field":
        row["laps"] = 20
    elif problem == "wrong-rank":
        row["position"] = "2"
    elif problem == "duplicate-driver":
        row["driver"] = classification["rows"][1]["driver"]
    elif problem == "zero-laps":
        row["laps"] = "0"
    elif problem == "invalid-time":
        row["time"] = "not-a-time"
    elif problem == "extra-classification-field":
        classification["verified"] = True
    elif problem == "float-season":
        classification["season"] = 2020.0
    elif problem == "float-driver-count":
        classification["classified_drivers"] = 20.0
    elif problem == "truncated-archive":
        malformed["archive"] = {
            key: archive[key]
            for key in ("schema_version", "session_id", "source_completion_sha256", "http_status")
        }
    elif problem == "empty-archive-url":
        archive["url"] = ""
    elif problem == "invalid-archive-hex":
        archive["body_hex"] = "not-hex"
    elif problem == "invalid-archive-digest":
        archive["capture_sha256"] = "forged"
    elif problem == "nondatetime-archive-field":
        archive["observed_at"] = {"bad": True}
    else:
        malformed["elapsed_before_boundary_seconds"] = {
            "elapsed-dict": {"bad": True},
            "elapsed-bool": True,
            "elapsed-nan": float("nan"),
            "elapsed-inf": float("inf"),
            "elapsed-negative": -1,
            "elapsed-perturbed": 5400.017,
        }[problem]
    with pytest.raises(ValueError):
        reclassify_evidence(
            entry,
            read(root, f"completion-derived/{IDENTITY}.json"),
            independent_finalization=malformed,
        )


@pytest.mark.parametrize(
    "path,value",
    [
        (("chronology_valid",), 0),
        (("chronology_valid",), 0.0),
        (("fastf1_supported",), 1),
        (("season",), 2020.0),
        (("round_number",), 1.0),
        (("fastf1_slot",), 1.0),
        (("wire", "http_status"), 200.0),
        (("parser", "valid"), 0),
    ],
)
def test_ready_rejects_type_coercion_in_replayed_independent_row(integration, path, value):
    root, _, _ = integration
    finalize_ready(integration)
    catalog = read(root, "reconciled-inventory.json")
    target = catalog["sessions"][0]
    for key in path[:-1]:
        target = target[key]
    assert type(target[path[-1]]) is not type(value)
    assert target[path[-1]] == value  # Demonstrate the Python equality counterexample.
    target[path[-1]] = value
    save(root, "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError, match="replayed independent finality"):
        ready_inventory(root)


@pytest.mark.parametrize("value", [0, 0.0])
@pytest.mark.parametrize("gate", ["saved", "reuse", "resume"])
def test_native_rejects_type_coercion_in_finality_metadata(integration, monkeypatch, gate, value):
    root, entry, proof = integration
    finalize_ready(integration)
    if gate == "resume":
        publish = runner()._publish_once
        monkeypatch.setattr(
            runner(), "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        with pytest.raises(KeyboardInterrupt):
            run(root, lambda *_: proof_session(proof))
        monkeypatch.setattr(runner(), "_publish_once", publish)
        journal = read(root, "study/acquisition-journal.json")
        record = journal["sessions"][IDENTITY]
        assert record["state"] == "captured"
        assert record["measurement"]["chronology_valid"] is False
        record["measurement"]["chronology_valid"] = value
        save(root, "study/acquisition-journal.json", journal)
    else:
        assert run(root, lambda *_: proof_session(proof))["new_writes"] == 1
        measurement = read(root, f"preflight/{IDENTITY}/measurement.json")
        assert measurement["chronology_valid"] is False
        measurement["chronology_valid"] = value
        for path in (root / f"preflight/{IDENTITY}").rglob("measurement.json"):
            save(root, path.relative_to(root).as_posix(), measurement)
    with pytest.raises(ValueError, match="independent finality metadata"):
        if gate == "saved":
            record = read(root, "study/acquisition-journal.json")["sessions"][IDENTITY]
            runner()._validate_measurement(
                root / record["attempt"],
                read(root, f"preflight/{IDENTITY}/measurement.json"),
                entry,
                root=root,
            )
        else:
            run(root, lambda *_: pytest.fail("typed metadata must reject before provider"))
    if gate == "resume":
        assert not (root / f"preflight/{IDENTITY}/measurement.json").exists()
