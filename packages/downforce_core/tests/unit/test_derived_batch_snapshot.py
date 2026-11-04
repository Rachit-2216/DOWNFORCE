"""Operation-local derived replay preserves wire proofs and cache authority."""

import gc
import inspect
import json
import pickle
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from downforce_core.detailed import evidence
from test_detailed_evidence import fallback_root, response


def derived_root(root, count=7):
    rows = fallback_root(root)
    publish_entries(root, rows[:count])
    return rows


def publish_entries(root, rows):
    for entry in rows:
        source = (root / f"completion/{entry['session_id']}.json").read_bytes()
        proof = evidence.derive_completion(
            entry, source, evidence.completion_wire(root, entry, json.loads(source))
        )
        evidence.publish_derived_completion(root, proof)


def count_cache_reads(monkeypatch):
    decoded = Counter()
    scans = Counter()
    original_unpickle = evidence.primitive_unpickle
    original_connect = sqlite3.connect

    def counted_unpickle(blob):
        decoded[blob] += 1
        return original_unpickle(blob)

    def counted_connect(path, *args, **kwargs):
        assert str(path).endswith("?mode=ro")
        assert kwargs["uri"] is True
        db = original_connect(path, *args, **kwargs)

        def trace(sql):
            if sql == "SELECT key, value FROM responses ORDER BY key":
                scans[str(path)] += 1

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(evidence, "primitive_unpickle", counted_unpickle)
    monkeypatch.setattr(evidence.sqlite3, "connect", counted_connect)
    return decoded, scans


def test_derived_batch_scans_once_preserves_exact_legacy_proofs_and_hashes(tmp_path, monkeypatch):
    rows = fallback_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        mirror = evidence.CONFIGURED_MIRROR_BASE + saved["url"].removeprefix(evidence.OFFICIAL_BASE)
        saved["url"] = saved["request"]["url"] = mirror
        blob = pickle.dumps(saved)
        db.execute("UPDATE responses SET value=? WHERE key='0'", (blob,))
        db.execute("INSERT INTO responses VALUES ('duplicate', ?, NULL)", (blob,))
        db.execute(
            "INSERT INTO responses VALUES ('unrelated', ?, NULL)",
            (pickle.dumps(response("https://example.test/unrelated")),),
        )
    publish_entries(tmp_path, rows[:7])
    expected = {
        path.stem: evidence.load_derived_completion(
            tmp_path, next(row for row in rows if row["session_id"] == path.stem)
        )
        for path in (tmp_path / "completion-derived").glob("*.json")
    }
    hashes = {
        path.stem: evidence.sha256(path.read_bytes())
        for path in (tmp_path / "completion-derived").glob("*.json")
    }
    with sqlite3.connect(database) as db:
        expected_decodes = Counter(row[0] for row in db.execute("SELECT value FROM responses"))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    decoded, scans = count_cache_reads(monkeypatch)
    corrections, actual_hashes = evidence.load_derived_corrections(tmp_path, rows)
    assert list(scans.values()) == [1]
    assert decoded == expected_decodes
    assert evidence._typed_finite_json_equal(corrections, expected)
    assert list(corrections) == list(expected)
    assert actual_hashes == hashes
    assert {path: path.read_bytes() for path in before} == before
    decoded.clear()
    scans.clear()
    assert evidence.load_derived_corrections(tmp_path, rows) == (corrections, actual_hashes)
    assert list(scans.values()) == [1]
    assert decoded == expected_decodes


@pytest.mark.parametrize("mutation", ["content", "same-bytes-replacement", "sidecar"])
def test_derived_batch_rejects_cache_mutation_before_return(tmp_path, monkeypatch, mutation):
    rows = derived_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    original_derive = evidence.derive_completion
    replayed = 0

    def mutate(entry, source, wire):
        nonlocal replayed
        replayed += 1
        if replayed == 7:
            if mutation == "content":
                with sqlite3.connect(database) as db:
                    db.execute("UPDATE responses SET expires=1 WHERE key='0'")
            elif mutation == "same-bytes-replacement":
                gc.collect()
                data = database.read_bytes()
                database.rename(database.with_suffix(".original"))
                database.write_bytes(data)
            else:
                Path(str(database) + "-journal").write_bytes(b"new member")
        return original_derive(entry, source, wire)

    monkeypatch.setattr(evidence, "derive_completion", mutate)
    with pytest.raises(ValueError, match="cached source changed"):
        evidence.load_derived_corrections(tmp_path, rows)
    assert replayed == 7


def test_derived_batch_rejects_mutation_during_first_scan(tmp_path, monkeypatch):
    rows = derived_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    original_unpickle = evidence.primitive_unpickle
    changed = False

    def mutate(blob):
        nonlocal changed
        if not changed:
            changed = True
            Path(str(database) + "-journal").write_bytes(b"changed during scan")
        return original_unpickle(blob)

    monkeypatch.setattr(evidence, "primitive_unpickle", mutate)
    with pytest.raises(ValueError, match="cached source changed during fallback snapshot"):
        evidence.load_derived_corrections(tmp_path, rows)


def test_derived_batch_next_call_rejects_new_contradictory_duplicate(tmp_path):
    rows = derived_root(tmp_path)
    evidence.load_derived_corrections(tmp_path, rows)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        saved["headers"]["changed"] = "contradictory"
        db.execute("INSERT INTO responses VALUES ('duplicate', ?, NULL)", (pickle.dumps(saved),))
    with pytest.raises(ValueError, match="conflicting cached responses for exact URL"):
        evidence.load_derived_corrections(tmp_path, rows)


def test_derived_batch_rejects_cross_host_conflict(tmp_path):
    rows = derived_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        mirror = evidence.CONFIGURED_MIRROR_BASE + saved["url"].removeprefix(evidence.OFFICIAL_BASE)
        saved["url"] = saved["request"]["url"] = mirror
        saved["_content"] = b'00:00:01.000{"Status":"Aborted"}\r\n'
        db.execute("INSERT INTO responses VALUES ('mirror', ?, NULL)", (pickle.dumps(saved),))
    message = "conflicting captured status observations across exact allowed URLs"
    with pytest.raises(ValueError, match=message):
        evidence.load_derived_corrections(tmp_path, rows)


@pytest.mark.parametrize("first_error", [False, True])
def test_derived_batch_retains_unsafe_unrelated_pickle_error_order(tmp_path, first_error):
    rows = derived_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        if first_error:
            saved = pickle.loads(
                db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0]
            )
            saved["request"]["url"] = "https://example.test/wrong-request"
            db.execute("UPDATE responses SET value=? WHERE key='0'", (pickle.dumps(saved),))
        db.execute("INSERT INTO responses VALUES ('unsafe', ?, NULL)", (pickle.dumps(Path("x")),))
    message = "cached request identity differs" if first_error else "pickle globals"
    with pytest.raises(ValueError, match=message):
        evidence.load_derived_corrections(tmp_path, rows)


@pytest.mark.parametrize("member", ["completion", "completion-derived"])
def test_derived_batch_next_call_reads_current_json(tmp_path, member):
    rows = derived_root(tmp_path)
    evidence.load_derived_corrections(tmp_path, rows)
    path = tmp_path / member / f"{rows[0]['session_id']}.json"
    if member == "completion":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        value = json.loads(path.read_bytes())
        value["state"] = "unverified"
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="derived completion is stale"):
        evidence.load_derived_corrections(tmp_path, rows)


def test_derived_batch_retains_unknown_identity_check(tmp_path):
    rows = derived_root(tmp_path)
    with pytest.raises(ValueError, match="unknown session identity"):
        evidence.load_derived_corrections(tmp_path, rows[7:])


def test_empty_derived_batch_still_verifies_its_reader(tmp_path, monkeypatch):
    verified = []
    original_verify = evidence._FallbackCacheReader.verify_unchanged

    def verify(reader):
        verified.append(reader)
        original_verify(reader)

    monkeypatch.setattr(evidence._FallbackCacheReader, "verify_unchanged", verify)
    assert evidence.load_derived_corrections(tmp_path, []) == ({}, {})
    assert len(verified) == 1


def test_public_derived_and_wire_readers_retain_legacy_scans_and_signatures(tmp_path, monkeypatch):
    rows = derived_root(tmp_path, count=1)
    entry = rows[0]
    source = json.loads((tmp_path / f"completion/{entry['session_id']}.json").read_bytes())
    expected = evidence.load_derived_completion(tmp_path, entry)
    decoded, scans = count_cache_reads(monkeypatch)
    assert evidence.load_derived_completion(tmp_path, entry) == expected
    assert list(scans.values()) == [2]
    assert sum(decoded.values()) == 220
    decoded.clear()
    scans.clear()
    assert evidence.completion_wire(tmp_path, entry, source) == expected["wire"]
    assert list(scans.values()) == [2]
    assert sum(decoded.values()) == 220
    decoded.clear()
    scans.clear()
    wire = evidence.read_cached_response(
        tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite",
        evidence.status_url(entry, source),
    )
    assert wire["body_hex"] == expected["wire"]["body_hex"]
    assert list(scans.values()) == [1]
    assert sum(decoded.values()) == 110
    assert list(inspect.signature(evidence.load_derived_completion).parameters) == ["root", "entry"]
    assert list(inspect.signature(evidence.completion_wire).parameters) == [
        "root",
        "entry",
        "source",
    ]
    parameters = inspect.signature(evidence.read_cached_response).parameters
    assert list(parameters) == ["cache", "expected_url", "max_body_bytes"]
    assert parameters["max_body_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["max_body_bytes"].default == evidence.MAX_BODY_BYTES
