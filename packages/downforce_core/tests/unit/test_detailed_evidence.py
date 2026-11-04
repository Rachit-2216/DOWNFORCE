"""Offline evidence must preserve wire bytes and fail closed on ambiguity."""

import importlib
import json
import pickle
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


def evidence():
    return importlib.import_module("downforce_core.detailed.evidence")


@pytest.mark.parametrize("separator", [b"\n", b"\r\n"])
@pytest.mark.parametrize("trailing", [False, True])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_status_stream_preserves_final_record(separator, trailing, bom):
    lines = [b'00:00:01.000{"Status":"Started"}', b'00:01:01.500{"Status":"Finished"}']
    body = bom + separator.join(lines) + (separator if trailing else b"")
    parsed = evidence().parse_status_stream(body)
    assert parsed["source_bytes"] == body
    assert parsed["valid"] is True
    assert [r["status"] for r in parsed["records"]] == ["Started", "Finished"]
    assert [r["time"] for r in parsed["records"]] == ["00:00:01.000", "00:01:01.500"]
    assert [bytes.fromhex(r["source_hex"]) for r in parsed["records"]] == lines


@pytest.mark.parametrize(
    "body,code",
    [
        (b"00:00:01.000{bad}", "malformed-record"),
        (b'00:60:01.000{"Status":"Started"}', "invalid-timestamp"),
        (b'0:00:01.000{"Status":"Started"}', "invalid-timestamp"),
        (b'00:00:01.000{"Status":"Mystery"}', "unknown-status"),
        (b'00:00:02.000{"Status":"Started"}\n00:00:01.000{"Status":"Ends"}', "nonmonotonic"),
        (b'00:00:01.000{"Status":"Started"}\n00:00:01.000{"Status":"Ends"}', "timestamp-conflict"),
        (b'00:00:01.000{"Status":"Started","Status":"Ends"}', "malformed-record"),
        (b'00:00:01.000{"Status":"Started"}\n\n', "malformed-record"),
        (b'00:00:01.000{"Status":"Started"}\xff', "malformed-record"),
        (b"", "empty-stream"),
    ],
)
def test_status_stream_reports_problem_without_dropping_bytes(body, code):
    parsed = evidence().parse_status_stream(body)
    assert parsed["source_bytes"] == body
    assert parsed["valid"] is False
    assert code in [issue["code"] for issue in parsed["issues"]]


def test_status_restart_never_promotes_an_earlier_terminal():
    body = (
        b'00:00:01.000{"Status":"Started"}\n'
        b'00:00:02.000{"Status":"Finalised"}\n00:00:03.000{"Status":"Started"}'
    )
    parsed = evidence().parse_status_stream(body)
    assert parsed["valid"]
    assert evidence().status_completion(parsed, "past-unverified", supported=True) == "unverified"


def test_coincident_finalised_ends_is_reported_but_not_an_ambiguous_restart():
    body = (
        b'00:00:01.000{"Status":"Started"}\n'
        b'00:00:02.000{"Status":"Finalised"}\n00:00:02.000{"Status":"Ends"}'
    )
    parsed = evidence().parse_status_stream(body)
    assert parsed["valid"] is True
    assert parsed["notes"] == [{"record": 3, "code": "coincident-terminal-progression"}]
    assert [r["status"] for r in parsed["records"]] == ["Started", "Finalised", "Ends"]


def test_archive_publication_does_not_override_latest_aborted():
    from downforce_core.detailed.reconciliation import classify_completion

    assert (
        classify_completion(
            "past-unverified", ["Started", "Aborted"], supported=True, archive_complete=True
        )
        == "abandoned-no-data"
    )


@pytest.mark.parametrize("integrity", ["invalid", "ambiguous"])
def test_derived_invalid_stream_cannot_become_completed(integrity):
    from downforce_core.detailed.reconciliation import reclassify_evidence

    source = {
        "state": "completed",
        "statuses": ["Started", "Ends"],
        "completion_integrity": integrity,
    }
    row = reclassify_evidence(
        {"completion": "past-unverified", "fastf1_supported": True}, source, {"Status": "Complete"}
    )
    assert row["state"] == "unverified"


def cache(path: Path, records: list[dict]):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE responses (key TEXT PRIMARY KEY, value BLOB, expires INTEGER)")
        for n, record in enumerate(records):
            db.execute(
                "INSERT INTO responses VALUES (?, ?, NULL)",
                (str(n), pickle.dumps(record, protocol=5)),
            )


def response(url="https://example.test/exact", body=b"wire", status=200):
    return {
        "_content": body,
        "created_at": "2026-10-01T00:00:00+00:00",
        "headers": {},
        "request": {"url": url, "method": "GET"},
        "status_code": status,
        "url": url,
    }


def test_primitive_http_cache_is_read_only_and_exact(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache(path, [response(), response(url="https://example.test/exact?other")])
    before = path.read_bytes()
    record = evidence().read_cached_response(path, "https://example.test/exact")
    assert record["body_hex"] == b"wire".hex()
    assert record["created_at"] == "2026-10-01T00:00:00+00:00"
    assert record["cache_keys"] == ["0"]
    with sqlite3.connect(path) as db:
        blob = db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0]
    assert record["response_pickle_sha256"] == [evidence().sha256(blob)]
    assert path.read_bytes() == before


def test_parser_rejects_over_bound_without_losing_source_bytes(monkeypatch):
    module = evidence()
    monkeypatch.setattr(module, "MAX_BODY_BYTES", 3)
    parsed = module.parse_status_stream(b"long")
    assert parsed["source_bytes"] == b"long"
    assert parsed["issues"] == [{"record": None, "code": "body-too-large"}]
    assert not parsed["valid"]


@pytest.mark.parametrize(
    "records,message",
    [
        ([response(status=403)], "HTTP 200"),
        ([response(), response(body=b"other")], "conflicting"),
        ([response(url="https://example.test/wrong")], "missing"),
    ],
)
def test_primitive_http_cache_rejects_unusable_source(tmp_path, records, message):
    path = tmp_path / "cache.sqlite"
    cache(path, records)
    with pytest.raises(ValueError, match=message):
        evidence().read_cached_response(path, "https://example.test/exact")


def test_primitive_http_cache_rejects_globals_and_bounded_content(tmp_path):
    with pytest.raises(ValueError, match="globals"):
        evidence().primitive_unpickle(pickle.dumps(Path("unsafe")))
    path = tmp_path / "cache.sqlite"
    cache(path, [response(body=b"long")])
    with pytest.raises(ValueError, match="bounded"):
        evidence().read_cached_response(path, "https://example.test/exact", max_body_bytes=3)


@pytest.mark.parametrize(
    "code,opcode,width", [(42, b"\x82", 1), (300, b"\x83", 2), (70000, b"\x84", 4)]
)
def test_cached_pickle_extensions_never_execute_even_with_primitive_return(
    monkeypatch, code, opcode, width
):
    import copyreg

    calls = []

    def harmless_marker():
        calls.append("executed")
        return "primitive"

    monkeypatch.setitem(copyreg._extension_cache, code, harmless_marker)
    blob = b"\x80\x04}\x8c\x01x" + opcode + code.to_bytes(width, "little") + b")Rs."
    failure = None
    try:
        evidence().primitive_unpickle(blob)
    except ValueError as exc:
        failure = exc
    assert calls == []
    assert failure is not None


def original(body, identity="2021-02-practice-1", path="2021/event/session/"):
    records = [json.loads(line[12:]) for line in body.decode("utf-8-sig").split("\r\n")[:-1]]
    return json.dumps(
        {
            "session_id": identity,
            "timing_path": path,
            "state": "unverified",
            "statuses": [r["Status"] for r in records],
            "times": [],
            "error": None,
        }
    ).encode()


def test_derived_correction_is_bound_to_original_and_wire(tmp_path):
    body = b'00:00:01.000{"Status":"Started"}\r\n00:00:02.000{"Status":"Finished"}'
    entry = {
        "session_id": "2021-02-practice-1",
        "season": 2021,
        "completion": "past-unverified",
        "fastf1_supported": True,
    }
    url = "https://livetiming.formula1.com/static/2021/event/session/SessionStatus.jsonStream"
    path = tmp_path / "cache.sqlite"
    cache(path, [response(url, body)])
    source = original(body)
    proof = evidence().derive_completion(entry, source, evidence().read_cached_response(path, url))
    assert proof["statuses"] == ["Started", "Finished"]
    assert proof["state"] == "unverified"
    assert proof["source_completion_sha256"] == evidence().sha256(source)
    assert proof["wire"]["body_sha256"] == evidence().sha256(body)
    assert proof["parser"]["parser_policy_version"] == evidence().PARSER_POLICY_VERSION
    bad = json.loads(source)
    bad["statuses"] = ["Aborted"]
    with pytest.raises(ValueError, match="sequence"):
        evidence().derive_completion(entry, json.dumps(bad).encode(), proof["wire"])
    with pytest.raises(ValueError, match="identity"):
        evidence().derive_completion({**entry, "session_id": "wrong"}, source, proof["wire"])


def schedule_fixture():
    rows = []
    for n in range(1, 23):
        row = {
            "round_number": n,
            "country": "Test",
            "location": "Circuit",
            "event_name": f"Grand Prix {n}",
            "event_date": "2022-03-20T00:00:00",
            "gmt_offset": "+03:00",
            "f1_api_support": True,
        }
        for slot, label in enumerate(
            ["Practice 1", "Practice 2", "Practice 3", "Qualifying", "Race"], 1
        ):
            row[f"session{slot}"] = label
            row[f"session{slot}_date"] = f"2022-03-20T{slot + 10:02d}:00:00"
        rows.append(row)
    return json.dumps(
        {key: {str(n): row[key] for n, row in enumerate(rows)} for key in rows[0]}
    ).encode()


def fallback_root(root):
    module = evidence()
    schedule = schedule_fixture()
    rows = module.schedule_sessions(schedule, 2022, now=datetime(2026, 1, 1, tzinfo=UTC))
    (root / "completion").mkdir()
    (root / "discovery-cache").mkdir()
    (root / "reconciliation-cache").mkdir()
    (root / "runtime").mkdir()
    (root / "inventory.json").write_text(
        json.dumps(
            {
                "complete_inventory": True,
                "discovered_at": "2026-01-01T00:00:00+00:00",
                "sessions": rows,
            }
        )
    )
    failures = [{"year": 2022, "error": "HTTPError"}]
    (root / "reconciled-inventory.json").write_text(
        json.dumps({"complete": True, "index_failures": failures})
    )
    (root / "runtime/official-index-retry-test.json").write_text(
        json.dumps(
            {
                "url": module.index_url(2022),
                "state": "failed",
                "error": "provider access denied: HTTP 403",
            }
        )
    )
    cache(
        root / "discovery-cache/fastf1_http_cache.sqlite",
        [response(module.schedule_url(2022), schedule)],
    )
    wire_records = []
    body = b'00:00:01.000{"Status":"Started"}\r\n00:00:02.000{"Status":"Ends"}\r\n'
    for entry in rows:
        relative = module.schedule_timing_paths(schedule, 2022)[entry["session_id"]]
        url = module.status_url(entry, {"timing_path": relative})
        wire_records.append(response(url, body))
        source = json.loads(original(body, entry["session_id"], relative))
        source.update(state="completed", times=["0:00:01", "0:00:02"])
        (root / "completion" / (entry["session_id"] + ".json")).write_text(json.dumps(source))
    cache(root / "reconciliation-cache/fastf1_http_cache.sqlite", wire_records)
    return rows


def test_schedule_backend_transform_is_utc_and_exact_count():
    rows = evidence().schedule_sessions(
        schedule_fixture(), 2022, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert len(rows) == 110
    assert rows[0]["scheduled_at"] == "2022-03-20T08:00:00+00:00"
    assert rows[0]["fastf1_slot"] == 1


def test_index_fallback_is_replayable_not_a_trusted_boolean(tmp_path):
    rows = fallback_root(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    proof = evidence().build_index_fallback(tmp_path, 2022)
    assert (
        proof["resolution"]
        == "historical-index-unavailable/session-identity-and-completion-verified"
    )
    assert proof["matched_session_ids"] == [r["session_id"] for r in rows]
    assert len(proof["completion_sources"]) == 110
    assert proof["historical_403_records"]
    assert evidence().validate_index_fallback(tmp_path, proof) == proof
    assert {p: p.read_bytes() for p in before} == before
    broken = {**proof, "verified": True, "completion_sources": {}}
    with pytest.raises(ValueError, match="stale|differs"):
        evidence().validate_index_fallback(tmp_path, broken)
    changed = tmp_path / "completion" / (rows[0]["session_id"] + ".json")
    changed.write_text(changed.read_text() + " ")
    with pytest.raises(ValueError, match="stale|differs"):
        evidence().validate_index_fallback(tmp_path, proof)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: r[0].update(event_name="Wrong"), "identity"),
        (lambda r: r[0].update(scheduled_at="2022-03-20T09:00:00+00:00"), "identity"),
        (lambda r: r.pop(), "110"),
    ],
)
def test_index_fallback_rejects_wrong_discovery(tmp_path, change, message):
    rows = fallback_root(tmp_path)
    change(rows)
    path = tmp_path / "inventory.json"
    value = json.loads(path.read_text())
    value["sessions"] = rows
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=message):
        evidence().build_index_fallback(tmp_path, 2022)


def test_index_fallback_requires_all_wire_and_preserved_403(tmp_path):
    fallback_root(tmp_path)
    db_path = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM responses WHERE key='0'")
    with pytest.raises(ValueError, match="missing"):
        evidence().build_index_fallback(tmp_path, 2022)
    (tmp_path / "runtime/official-index-retry-test.json").unlink()
    with pytest.raises(ValueError, match="403"):
        evidence().build_index_fallback(tmp_path, 2022)


def test_missing_wire_blocks_even_with_native_but_captured_mirror_can_bind_native(tmp_path):
    import pandas as pd

    rows = fallback_root(tmp_path)
    entry = rows[0]
    identity = entry["session_id"]
    with sqlite3.connect(tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite") as db:
        db.execute("DELETE FROM responses WHERE key='0'")
    attempt = tmp_path / "preflight" / identity / "original"
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
                "sha256": evidence().sha256(table.read_bytes()),
                "bytes": table.stat().st_size,
                "rows": 2,
            }
        },
    }
    for path in (attempt / "measurement.json", attempt.parent / "measurement.json"):
        path.write_text(json.dumps(measurement))
    with pytest.raises(ValueError, match="missing"):
        evidence().build_index_fallback(tmp_path)
    source_path = tmp_path / "completion" / (identity + ".json")
    source = json.loads(source_path.read_text())
    source["times"] = []
    source_path.write_text(json.dumps(source))
    url = (
        evidence()
        .status_url(entry, source)
        .replace(
            "https://livetiming.formula1.com/static/",
            "https://livetiming-mirror.fastf1.dev/static/",
        )
    )
    body = b'00:00:01.000{"Status":"Started"}\r\n00:00:02.000{"Status":"Ends"}\r\n'
    with sqlite3.connect(tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite") as db:
        db.execute(
            "INSERT INTO responses VALUES ('mirror', ?, NULL)", (pickle.dumps(response(url, body)),)
        )
    proof = evidence().build_index_fallback(tmp_path)
    native = proof["completion_sources"][identity]["native"]
    assert native["native_sha256"] == evidence().sha256(table.read_bytes())
    assert native["statuses"] == ["Started", "Ends"]
    assert evidence().validate_index_fallback(tmp_path, proof) == proof
    frame = pd.read_parquet(table)
    frame["Time"] = pd.to_timedelta([1001, 2001], unit="ms")
    frame.to_parquet(table, index=False)
    measurement["tables"]["session_status"].update(
        sha256=evidence().sha256(table.read_bytes()), bytes=table.stat().st_size
    )
    for path in (attempt / "measurement.json", attempt.parent / "measurement.json"):
        path.write_text(json.dumps(measurement))
    with pytest.raises(ValueError, match="native.*wire|wire.*native"):
        evidence().build_index_fallback(tmp_path)
    table.write_bytes(table.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        evidence().validate_index_fallback(tmp_path, proof)


def test_derived_publication_is_idempotent_and_revalidated(tmp_path):
    rows = fallback_root(tmp_path)
    entry = rows[0]
    source_path = tmp_path / "completion" / (entry["session_id"] + ".json")
    original_bytes = source_path.read_bytes()
    proof = evidence().derive_completion(
        entry,
        original_bytes,
        evidence().completion_wire(tmp_path, entry, json.loads(original_bytes)),
    )
    assert evidence().publish_derived_completion(tmp_path, proof)
    assert not evidence().publish_derived_completion(tmp_path, proof)
    assert source_path.read_bytes() == original_bytes
    assert evidence().load_derived_completion(tmp_path, entry) == proof
    source_path.write_bytes(original_bytes + b" ")
    with pytest.raises(ValueError, match="stale|differs"):
        evidence().load_derived_completion(tmp_path, entry)


def test_index_history_is_blocking_without_validated_report_even_false_boolean(tmp_path):
    fallback_root(tmp_path)
    catalog = {
        "index_failures": [{"year": 2022, "error": "HTTPError"}],
        "blocking_index_failures": [],
        "index_fallback_verified": True,
    }
    blocking, resolutions = evidence().index_failure_resolutions(tmp_path, catalog)
    assert blocking == catalog["index_failures"]
    assert resolutions[0]["capabilities"]["session_identity"] == "unverified"
    report = evidence().build_index_fallback(tmp_path)
    relative = "evidence-repair/index-fallback-2022.json"
    evidence().publish_proof(tmp_path, relative, report)
    catalog["index_fallback_reports"] = {
        "2022": {"path": relative, "sha256": evidence().sha256((tmp_path / relative).read_bytes())}
    }
    assert evidence().index_failure_resolutions(tmp_path, catalog)[0] == []
    assert catalog["index_failures"] == [{"year": 2022, "error": "HTTPError"}]
    broken = json.loads((tmp_path / relative).read_text())
    broken["completion_sources"] = {}
    (tmp_path / relative).write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="checksum|stale"):
        evidence().index_failure_resolutions(tmp_path, catalog)


def test_evidence_reference_cannot_escape_or_use_absolute_path(tmp_path):
    with pytest.raises(ValueError, match="unsafe"):
        evidence().publish_proof(tmp_path, "../outside.json", {})
    with pytest.raises(ValueError, match="unsafe"):
        evidence().publish_proof(tmp_path, str(tmp_path / "absolute.json"), {})


def test_configured_mirror_remains_explicit_and_conflicting_hosts_fail(tmp_path):
    rows = fallback_root(tmp_path)
    entry = rows[0]
    source = json.loads((tmp_path / "completion" / (entry["session_id"] + ".json")).read_text())
    primary = evidence().status_url(entry, source)
    mirror = primary.replace(
        "https://livetiming.formula1.com/static/", "https://livetiming-mirror.fastf1.dev/static/"
    )
    database = tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite"
    with sqlite3.connect(database) as db:
        saved = pickle.loads(db.execute("SELECT value FROM responses WHERE key='0'").fetchone()[0])
        saved["url"] = saved["request"]["url"] = mirror
        db.execute("UPDATE responses SET value=? WHERE key='0'", (pickle.dumps(saved),))
    wire = evidence().completion_wire(tmp_path, entry, source)
    assert wire["url"] == mirror
    assert wire["source_origin"] == "fastf1-configured-mirror"
    proof = evidence().build_index_fallback(tmp_path)
    assert proof["completion_sources"][entry["session_id"]]["wire"]["url"] == mirror
    with sqlite3.connect(database) as db:
        bad = response(primary, b'00:00:01.000{"Status":"Started"}\r\n')
        db.execute("INSERT INTO responses VALUES ('primary', ?, NULL)", (pickle.dumps(bad),))
    with pytest.raises(ValueError, match="conflicting"):
        evidence().completion_wire(tmp_path, entry, source)


def test_status_url_encodes_unicode_transport_path_without_changing_source():
    source = {"timing_path": "2022/2022-11-13_São_Paulo_Grand_Prix/2022-11-13_Race/"}
    url = evidence().status_url({"season": 2022}, source)
    assert "S%C3%A3o_Paulo" in url
    assert source["timing_path"].startswith("2022/2022-11-13_São")


def finalizer():
    import importlib.util

    script = Path(__file__).resolve().parents[4] / "scripts/finalize_detailed_inventory.py"
    spec = importlib.util.spec_from_file_location("detailed_finalize_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unresolved_finalizer_root(root):
    from test_detailed_study_runner import inventory, save

    entry = inventory(root, count=1)[0]
    path = root / "completion" / (entry["session_id"] + ".json")
    source = json.loads(path.read_text())
    source.update(
        state="unverified",
        statuses=["Started", "Finished"],
        observed_at="2026-01-01T00:00:00+00:00",
    )
    save(path, source)


def test_finalizer_rejects_runtime_junction_before_budget_cache_or_http(tmp_path, monkeypatch):
    unresolved_finalizer_root(tmp_path)
    module = finalizer()
    calls = []
    original = Path.is_junction
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path == tmp_path / "runtime" or original(path),
    )
    monkeypatch.setattr(module.fastf1.Cache, "enable_cache", lambda *_: calls.append("cache"))
    monkeypatch.setattr(
        module.fastf1.Cache, "requests_get", lambda *_a, **_k: pytest.fail("no HTTP")
    )
    with pytest.raises(ValueError, match="linked"):
        module.finalize(tmp_path)
    assert not (tmp_path / "runtime/fastf1-budget.json").exists()
    assert calls == []
    assert not (tmp_path / "archive-finalization").exists()


@pytest.mark.parametrize("existing_budget", [False, True])
def test_finalizer_initializes_cooldown_and_cache_only_inside_owned_lock(
    tmp_path, monkeypatch, existing_budget
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    unresolved_finalizer_root(tmp_path)
    module = finalizer()
    budget_path = tmp_path / "runtime/fastf1-budget.json"
    if existing_budget:
        budget_path.parent.mkdir()
        budget_path.write_text('{"existing":"unchanged"}')
    locked = False
    calls = []

    @contextmanager
    def guard(_root):
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    def defer(_budget, not_before):
        assert locked, "cooldown must be initialized under the owned lock"
        calls.append(("defer", not_before))

    def enable(_cache):
        assert locked, "provider cache must be initialized under the owned lock"
        calls.append(("cache", None))

    def request(*_a, **_k):
        assert locked
        calls.append(("request", None))
        return SimpleNamespace(status_code=200, content=b'{"Status":"Complete"}')

    monkeypatch.setattr(module, "fastf1_guard", guard)
    monkeypatch.setattr(module.RequestBudget, "defer_until", defer)
    monkeypatch.setattr(module.fastf1.Cache, "enable_cache", enable)
    monkeypatch.setattr(module.fastf1.Cache, "requests_get", request)
    result = module.finalize(tmp_path)
    assert result["summary"] == {"completed": 1}
    assert [call[0] for call in calls] == (
        ["cache", "request"] if existing_budget else ["defer", "cache", "request"]
    )
    if existing_budget:
        assert budget_path.read_text() == '{"existing":"unchanged"}'


def test_offline_finalizer_uses_bound_corrections_preserving_originals_and_history(
    tmp_path, monkeypatch
):
    rows = fallback_root(tmp_path)
    module = finalizer()
    monkeypatch.setattr(
        module.fastf1.Cache, "requests_get", lambda *_a, **_k: pytest.fail("no requests")
    )
    (tmp_path / "openf1-inventory.json").write_text(json.dumps({"sessions": [], "failures": []}))
    entry = rows[0]
    source_path = tmp_path / "completion" / (entry["session_id"] + ".json")
    original_bytes = source_path.read_bytes()
    correction = evidence().derive_completion(
        entry,
        original_bytes,
        evidence().completion_wire(tmp_path, entry, json.loads(original_bytes)),
    )
    evidence().publish_derived_completion(tmp_path, correction)
    proof = evidence().build_index_fallback(tmp_path)
    evidence().publish_proof(tmp_path, "evidence-repair/index-fallback-2022.json", proof)
    result = module.finalize(tmp_path, offline=True)
    from downforce_core.detailed.study_runner import ready_inventory

    assert ready_inventory(tmp_path)[0]["blocking_index_failures"] == []
    assert result["index_failures"] == [{"year": 2022, "error": "HTTPError"}]
    assert result["blocking_index_failures"] == []
    assert result["completion_derived_sha256"][entry["session_id"]]
    assert (
        result["index_failure_resolutions"][0]["capabilities"]["official_index"]
        == "historically-unavailable"
    )
    assert source_path.read_bytes() == original_bytes
    before = (tmp_path / "reconciled-inventory.json").read_bytes()
    module.finalize(tmp_path, offline=True)
    assert (tmp_path / "reconciled-inventory.json").read_bytes() == before
    result["sessions"][0]["state"] = "unverified"
    (tmp_path / "reconciled-inventory.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="unverified"):
        ready_inventory(tmp_path)
    assert not (tmp_path / "study/sample-plan.json").exists()


def test_gate_does_not_trust_cleared_index_blockers(tmp_path):
    from downforce_core.detailed.study_runner import ready_inventory
    from test_detailed_study_runner import inventory, mutate_catalog

    inventory(tmp_path)
    mutate_catalog(
        tmp_path,
        lambda c: c.update(
            index_failures=[{"year": 2022}],
            blocking_index_failures=[],
            index_fallback_verified=True,
        ),
    )
    with pytest.raises(ValueError, match="index"):
        ready_inventory(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "wrong"),
        ("url", "https://example.test/swapped"),
        ("source_completion_sha256", "stale"),
    ],
)
def test_finalizer_rejects_swapped_archive_identity_and_source(tmp_path, field, value):
    rows = fallback_root(tmp_path)
    entry = rows[0]
    source_path = tmp_path / "completion" / (entry["session_id"] + ".json")
    source = json.loads(source_path.read_text())
    body = b'{"Status":"Complete"}'
    archive = {
        "session_id": entry["session_id"],
        "url": evidence()
        .status_url(entry, source)
        .replace("SessionStatus.jsonStream", "ArchiveStatus.json"),
        "http_status": 200,
        "body_hex": body.hex(),
        "body_sha256": evidence().sha256(body),
        "source_completion_sha256": evidence().sha256(source_path.read_bytes()),
        "observed_at": "2026-01-01T00:00:00+00:00",
    }
    archive[field] = value
    directory = tmp_path / "archive-finalization"
    directory.mkdir()
    (directory / (entry["session_id"] + ".json")).write_text(json.dumps(archive))
    (tmp_path / "openf1-inventory.json").write_text(json.dumps({"sessions": []}))
    with pytest.raises(ValueError, match="archive.*identity|archive.*source"):
        finalizer().finalize(tmp_path, offline=True)


def test_deeply_nested_json_is_a_malformed_record_not_an_uncaught_error():
    body = b'00:00:01.000{"Status":"Started","nested":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
    parsed = evidence().parse_status_stream(body)
    assert not parsed["valid"]
    assert "malformed-record" in [i["code"] for i in parsed["issues"]]


def repair_script():
    import importlib.util

    script = Path(__file__).resolve().parents[4] / "scripts/repair_detailed_evidence.py"
    spec = importlib.util.spec_from_file_location("detailed_repair_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repair_fixture(root):
    rows = fallback_root(root)
    cases = [
        (
            "2020-01-practice-1",
            2020,
            "Austrian Grand Prix",
            "2020/2020-07-05_Austrian_Grand_Prix/2020-07-03_Practice_1/",
            [("00:00:07.144", "Inactive"), ("00:09:47.629", "Started")]
            + [("01:39:47.645", value) for value in ["Finished", "Inactive", "Started"] * 4],
            True,
        ),
        (
            "2021-02-practice-1",
            2021,
            "Emilia Romagna Grand Prix",
            "2021/2021-04-18_Emilia_Romagna_Grand_Prix/2021-04-16_Practice_1/",
            [
                ("00:00:03.371", "Inactive"),
                ("00:15:00.039", "Started"),
                ("00:52:18.781", "Aborted"),
                ("00:54:36.669", "Inactive"),
                ("01:03:00.084", "Started"),
                ("01:15:00.053", "Finished"),
            ],
            False,
        ),
    ]
    for identity, season, event, relative, records, trailing in cases:
        body = (
            b"\xef\xbb\xbf"
            + b"\r\n".join(
                (time + json.dumps({"Status": status}, separators=(",", ":"))).encode()
                for time, status in records
            )
            + (b"\r\n" if trailing else b"")
        )
        entry = {
            **rows[0],
            "session_id": identity,
            "season": season,
            "event_name": event,
            "event_id": identity[:7],
            "round_number": int(identity[5:7]),
            "scheduled_at": f"{season}-03-20T08:00:00+00:00",
        }
        source = json.loads(original(body, identity, relative))
        source["times"] = [
            evidence()._time_string(t) for t, _ in (records if trailing else records[:-1])
        ]
        (root / "completion" / (identity + ".json")).write_text(json.dumps(source))
        rows.append(entry)
        with sqlite3.connect(root / "reconciliation-cache/fastf1_http_cache.sqlite") as db:
            db.execute(
                "INSERT INTO responses VALUES (?, ?, NULL)",
                (identity, pickle.dumps(response(evidence().status_url(entry, source), body))),
            )
    discovery = json.loads((root / "inventory.json").read_text())
    discovery["sessions"] = rows
    (root / "inventory.json").write_text(json.dumps(discovery))
    (root / "openf1-inventory.json").write_text(json.dumps({"sessions": [], "failures": []}))


def test_offline_repair_reproduces_three_findings_and_never_starts_acquisition(
    tmp_path, monkeypatch
):
    repair_fixture(tmp_path)
    script = repair_script()
    module = finalizer()
    monkeypatch.setattr(
        module.fastf1.Cache, "requests_get", lambda *_a, **_k: pytest.fail("no requests")
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = script.repair(tmp_path, publish=False)
    assert [finding["kind"] for finding in report["findings"]] == [
        "missing-nonnewline-final-record",
        "ambiguous-identical-timestamp-restarts",
        "historical-index-http-403",
    ]
    assert report["corrected_states"] == {
        "2021-02-practice-1": "unverified",
        "2020-01-practice-1": "unverified",
    }
    assert {p: p.read_bytes() for p in before} == before
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == len(before)
    script.repair(tmp_path, publish=True)
    report_path = tmp_path / "evidence-repair/repair-report.json"
    first = report_path.read_bytes()
    script.repair(tmp_path, publish=True)
    assert report_path.read_bytes() == first
    result = module.finalize(tmp_path, offline=True)
    assert result["summary"] == {"completed": 110, "unverified": 2}
    assert result["blocking_index_failures"] == []
    from downforce_core.detailed.study_runner import ready_inventory

    with pytest.raises(ValueError, match="unverified"):
        ready_inventory(tmp_path)
    assert not (tmp_path / "study/sample-plan.json").exists()
    assert not (tmp_path / "study/acquisition-journal.json").exists()
    assert {p: p.read_bytes() for p in before if p.name != "reconciled-inventory.json"} == {
        p: content for p, content in before.items() if p.name != "reconciled-inventory.json"
    }


def test_gate_rejects_completed_state_on_a_bound_invalid_derived_stream(tmp_path):
    from downforce_core.detailed.study_runner import ready_inventory
    from test_detailed_study_runner import inventory

    entries = inventory(tmp_path, count=1)
    entry = entries[0]
    body = (
        b'00:00:00.000{"Status":"Unknown"}\r\n'
        b'00:00:01.000{"Status":"Started"}\r\n00:00:02.000{"Status":"Ends"}\r\n'
    )
    relative = "2018/event/session/"
    source = json.loads(original(body, entry["session_id"], relative))
    source["times"] = ["0:00:00", "0:00:01", "0:00:02"]
    (tmp_path / "completion").mkdir(exist_ok=True)
    path = tmp_path / "completion" / (entry["session_id"] + ".json")
    path.write_text(json.dumps(source))
    (tmp_path / "reconciliation-cache").mkdir()
    cache(
        tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite",
        [response(evidence().status_url(entry, source), body)],
    )
    proof = evidence().derive_completion(
        entry, path.read_bytes(), evidence().completion_wire(tmp_path, entry, source)
    )
    assert proof["completion_integrity"] == "invalid"
    evidence().publish_derived_completion(tmp_path, proof)
    (tmp_path / "openf1-inventory.json").write_text(json.dumps({"sessions": []}))
    result = finalizer().finalize(tmp_path, offline=True)
    result["sessions"][0]["state"] = "completed"
    (tmp_path / "reconciled-inventory.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="integrity|unverified|formal"):
        ready_inventory(tmp_path)
