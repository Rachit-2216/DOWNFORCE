"""One strict cache scan per fallback replay, without changing its wire proof."""

import gc
import json
import pickle
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from downforce_core.detailed import evidence
from test_detailed_evidence import fallback_root, response


def test_fallback_scans_each_cache_blob_once_and_preserves_legacy_proof(tmp_path, monkeypatch):
    rows = fallback_root(tmp_path)
    entry = rows[0]
    source = json.loads((tmp_path / f"completion/{entry['session_id']}.json").read_bytes())
    official = evidence.status_url(entry, source)
    mirror = evidence.CONFIGURED_MIRROR_BASE + official.removeprefix(evidence.OFFICIAL_BASE)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        saved["url"] = saved["request"]["url"] = mirror
        blob = pickle.dumps(saved)
        db.execute("UPDATE responses SET value=? WHERE key='0'", (blob,))
        db.execute("INSERT INTO responses VALUES ('duplicate', ?, NULL)", (blob,))
        db.execute(
            "INSERT INTO responses VALUES ('unrelated', ?, NULL)",
            (pickle.dumps(response("https://example.test/unrelated")),),
        )
    # These public readers deliberately retain the legacy per-URL behavior.
    legacy_wires = {}
    for row in rows:
        captured = json.loads((tmp_path / f"completion/{row['session_id']}.json").read_bytes())
        legacy_wires[row["session_id"]] = evidence.completion_wire(tmp_path, row, captured)
    legacy_schedule = evidence.read_cached_response(
        tmp_path / "discovery-cache/fastf1_http_cache.sqlite", evidence.schedule_url(2022)
    )
    legacy_schedule["cache_path"] = "discovery-cache/fastf1_http_cache.sqlite"
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
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
    proof = evidence.build_index_fallback(tmp_path)
    assert proof["schedule"] == legacy_schedule
    assert {k: v["wire"] for k, v in proof["completion_sources"].items()} == legacy_wires
    assert proof["matched_session_ids"] == [r["session_id"] for r in rows]
    assert len(proof["completion_sources"]) == 110
    assert proof["historical_403_records"] == {
        "runtime/official-index-retry-test.json": evidence.sha256(
            (tmp_path / "runtime/official-index-retry-test.json").read_bytes()
        )
    }
    assert legacy_wires[entry["session_id"]]["source_origin"] == "fastf1-configured-mirror"
    assert legacy_wires[entry["session_id"]]["cache_keys"] == ["0", "duplicate"]
    # Two rows may contain identical bytes: count against their actual multiplicity.
    expected = Counter()
    for cache_path in [
        tmp_path / "discovery-cache/fastf1_http_cache.sqlite",
        database,
    ]:
        with original_connect(cache_path) as db:
            expected.update(row[0] for row in db.execute("SELECT value FROM responses"))
    assert decoded == expected
    assert list(scans.values()) == [1, 1]
    assert {p: p.read_bytes() for p in before} == before
    decoded.clear()
    scans.clear()
    assert evidence.validate_index_fallback(tmp_path, proof) == proof
    assert decoded == expected
    assert list(scans.values()) == [1, 1]


@pytest.mark.parametrize("mutation", ["content", "same-bytes-replacement", "sidecar"])
def test_fallback_rejects_cache_mutation_after_its_snapshot(tmp_path, monkeypatch, mutation):
    fallback_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    original_derive = evidence.derive_completion
    changed = False

    def mutate(entry, source, wire):
        nonlocal changed
        if not changed and entry["session_id"] == "2022-22-race":
            changed = True
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
    with pytest.raises(ValueError, match="cache.*changed|cache.*mutation"):
        evidence.build_index_fallback(tmp_path)


def test_fallback_next_call_freshly_detects_changed_duplicate(tmp_path):
    fallback_root(tmp_path)
    proof = evidence.build_index_fallback(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        saved["headers"]["changed"] = "contradictory"
        db.execute("INSERT INTO responses VALUES ('duplicate', ?, NULL)", (pickle.dumps(saved),))
    with pytest.raises(ValueError, match="conflicting cached responses for exact URL"):
        evidence.validate_index_fallback(tmp_path, proof)


def test_fallback_retains_missing_source_error_for_nonfile_cache(tmp_path):
    fallback_root(tmp_path)
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    gc.collect()
    database.rename(database.with_suffix(".original"))
    database.mkdir()
    with pytest.raises(ValueError, match="missing exact cached source URL"):
        evidence.build_index_fallback(tmp_path)


@pytest.mark.parametrize("first_error", [False, True])
def test_fallback_preserves_unsafe_unrelated_pickle_error_order(tmp_path, first_error):
    fallback_root(tmp_path)
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
        evidence.build_index_fallback(tmp_path)
