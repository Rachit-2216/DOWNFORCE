"""Explicit native recapture never guesses or rewrites legacy HTTP status."""

import gzip
import json
import os
from datetime import datetime

import pytest
from downforce_core.detailed import openf1, openf1_transport
from downforce_core.storage.parquet import file_sha256
from test_detailed_proofs import save
from test_openf1_enrichment import historical_fixture
from test_openf1_safety import response, transport

FIELD = "openf1_evidence_path"


def current(tmp_path, monkeypatch, endpoints=("drivers",)):
    root, session = historical_fixture(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", endpoints)
    clock, _, _ = transport(
        monkeypatch, [response(body=b'[{"session_key":1,"old":1}]')] * len(endpoints)
    )
    clock[0] -= 3600  # The later explicit refresh has a genuinely later budget clock.
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    directory = root / "study/openf1" / session["session_id"]
    return root, session, directory, report


def legacy(tmp_path, monkeypatch, endpoints=("drivers",)):
    root, session, directory, report = current(tmp_path, monkeypatch, endpoints)
    for path in directory.glob("*-attempt-*.json"):
        path.unlink()
    (directory / "attempts.json").unlink()
    report.pop("attempts")
    report.pop("attempts_index_sha256")
    for endpoint in endpoints:
        path = directory / f"{endpoint}-source.json"
        source = json.loads(path.read_bytes())
        source.pop("http_status")
        save(path, source)
        record_path = directory / f"{endpoint}.json"
        record = json.loads(record_path.read_bytes())
        record.pop("http_status")
        record.pop("availability")
        record["observed_at"] = "2026-10-01T00:00:00+00:00"
        save(record_path, record)
        report["endpoints"][endpoint] = record
    save(directory / "measurement.json", report)
    return root, session, directory


def preserved(directory):
    return {
        path.relative_to(directory).as_posix(): (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }


def refresh(root, session):
    function = getattr(openf1, "refresh_enrichment", None)
    assert callable(function), "explicit native reverification operation is required"
    return function(root, session, openf1.OpenF1Client(root))


def bind(root, session, report):
    path = root / "study/session-measurements" / f"{session['session_id']}.json"
    value = json.loads(path.read_bytes())
    for key in (FIELD, "openf1_measured", "openf1_raw_bytes", "openf1_canonical_bytes"):
        value[key] = report[key]
    save(path, value)


def test_default_legacy_missing_status_remains_rejected_without_http(tmp_path, monkeypatch):
    root, session, old = legacy(tmp_path, monkeypatch)
    before = preserved(old)

    class Offline:
        def fetch(self, *_args):
            pytest.fail("unknown legacy HTTP status is not authority for a request")

    with pytest.raises(ValueError, match="URL/status"):
        openf1.measure_enrichment(root, session, Offline())
    assert preserved(old) == before


def test_bound_missing_fresh_view_is_rejected_without_refetch_or_legacy_fallback(
    tmp_path, monkeypatch
):
    root, session, old, report = current(tmp_path, monkeypatch)
    before = preserved(old)
    report[FIELD] = f"study/openf1/reverified/{session['session_id']}"
    bind(root, session, report)
    _, requests, _ = transport(monkeypatch, [])
    with pytest.raises(ValueError, match="reverified.*missing|missing.*reverified"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert requests == [] and preserved(old) == before


def test_explicit_native_refresh_preserves_and_leases_legacy_and_captures_changed_body(
    tmp_path, monkeypatch
):
    root, session, old = legacy(tmp_path, monkeypatch)
    before = preserved(old)
    body = b' [{"session_key":1,"new_value":1.000000000001,"segments":[2048,2049]}]\n'
    _, requests, _ = transport(monkeypatch, [response(body=body, headers={"X-Observed": "fresh"})])
    build = openf1_transport.build_opener

    class Opener:
        def __init__(self, *args):
            self.original = build(*args)

        def open(self, request, timeout):
            if os.name == "nt":
                with pytest.raises(PermissionError):
                    (old / "drivers-source.json").write_bytes(b"changed")
            return self.original.open(request, timeout)

    monkeypatch.setattr(openf1_transport, "build_opener", Opener)
    report = refresh(root, session)
    relative = f"study/openf1/reverified/{session['session_id']}"
    assert report[FIELD] == relative and len(requests) == 1
    fresh = root / relative
    record = report["endpoints"]["drivers"]
    assert record["http_status"] == 200 and record["availability"] == "available"
    assert gzip.decompress((fresh / record["raw_file"]).read_bytes()) == body
    assert datetime.fromisoformat(record["observed_at"]) > datetime.fromisoformat(
        "2026-10-01T00:00:00+00:00"
    )
    attempts = json.loads((fresh / "attempts.json").read_bytes())["attempts"]
    attempt = json.loads((fresh / next(iter(attempts))).read_bytes())
    assert attempt["http_status"] == 200 and attempt["headers"] == {"X-Observed": "fresh"}
    assert attempt["raw_file_sha256"] == file_sha256(fresh / record["raw_file"])
    assert preserved(old) == before
    candidate = root / "study/session-measurements" / f"{session['session_id']}.json"
    assert FIELD not in json.loads(candidate.read_bytes())  # Explicit publication is separate.
    value = json.loads(candidate.read_bytes())
    value[FIELD] = relative
    save(candidate, value)

    class Offline:
        def fetch(self, *_args):
            pytest.fail("the candidate-bound fresh view must be reused without HTTP")

    assert openf1.measure_enrichment(root, session, Offline()) == report
    assert preserved(old) == before


def test_refresh_rejects_protocol_only_client(tmp_path, monkeypatch):
    root, session, old = legacy(tmp_path, monkeypatch)
    before = preserved(old)
    with pytest.raises(ValueError, match="owned OpenF1Client"):
        openf1.refresh_enrichment(root, session, object())
    assert preserved(old) == before


@pytest.mark.parametrize("status", [401, 403])
def test_refresh_denial_retains_fresh_attempt_stops_http_and_preserves_old(
    tmp_path, monkeypatch, status
):
    root, session, old = legacy(tmp_path, monkeypatch, ("drivers", "laps"))
    before = preserved(old)
    body = b' {"detail":"denied", "exact":1.000000000001}\n'
    _, requests, _ = transport(monkeypatch, [response(status, body)])
    client = openf1.OpenF1Client(root)
    with pytest.raises(PermissionError):
        openf1.refresh_enrichment(root, session, client)
    with pytest.raises(PermissionError):
        openf1.refresh_enrichment(root, session, client)
    assert len(requests) == 1 and preserved(old) == before
    fresh = root / f"study/openf1/reverified/{session['session_id']}"
    attempt = json.loads(next(fresh.glob("*-attempt-*.json")).read_bytes())
    assert attempt["http_status"] == status
    assert gzip.decompress((fresh / attempt["raw_file"]).read_bytes()) == body
    assert not (fresh / "measurement.json").exists()


def test_interrupted_fresh_normalization_resumes_captured_native_body_without_http(
    tmp_path, monkeypatch
):
    root, session, old = legacy(tmp_path, monkeypatch)
    before = preserved(old)
    _, requests, _ = transport(monkeypatch, [response(body=b'[{"session_key":1,"fresh":true}]')])
    writer = openf1.pq.write_table

    def interrupted(*_args, **_kwargs):
        raise OSError("interrupted fresh normalization")

    monkeypatch.setattr(openf1.pq, "write_table", interrupted)
    with pytest.raises(OSError, match="interrupted fresh"):
        refresh(root, session)
    fresh = root / f"study/openf1/reverified/{session['session_id']}"
    captured = preserved(fresh)
    assert any(name.endswith("-source.json") for name in captured)
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    report = refresh(root, session)
    assert len(requests) == 1 and report[FIELD] == fresh.relative_to(root).as_posix()
    assert {name: preserved(fresh)[name] for name in captured} == captured
    assert preserved(old) == before


@pytest.mark.parametrize(
    "marker",
    [
        None,
        False,
        "../outside",
        "D:/outside",
        "study/openf1/reverified/another-session",
        "study/openf1/reverified/2024-01-race/../2024-01-race",
    ],
)
def test_invalid_marker_rejected_before_http_without_default_fallback(
    tmp_path, monkeypatch, marker
):
    root, session, old, report = current(tmp_path, monkeypatch)
    before = preserved(old)
    report[FIELD] = marker
    bind(root, session, report)
    _, requests, _ = transport(monkeypatch, [])
    with pytest.raises(ValueError, match="evidence path"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert requests == [] and preserved(old) == before


@pytest.mark.parametrize("damage", ["missing-manifest", "incomplete-endpoints", "bad-wire"])
def test_broken_bound_fresh_proof_never_uses_valid_default_or_refetches(
    tmp_path, monkeypatch, damage
):
    root, session, old, _ = current(tmp_path, monkeypatch)
    before = preserved(old)
    _, requests, _ = transport(monkeypatch, [response(body=b'[{"session_key":1,"fresh":true}]')])
    report = refresh(root, session)
    bind(root, session, report)
    fresh = root / report[FIELD]
    if damage == "missing-manifest":
        (fresh / "measurement.json").unlink()
    elif damage == "incomplete-endpoints":
        value = json.loads((fresh / "measurement.json").read_bytes())
        value["endpoints"] = {}
        save(fresh / "measurement.json", value)
    else:
        (fresh / report["endpoints"]["drivers"]["raw_file"]).write_bytes(b"bad")
    count = len(requests)
    with pytest.raises(ValueError):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert len(requests) == count and preserved(old) == before


def test_linked_bound_leaf_rejected_without_legacy_fallback(tmp_path, monkeypatch):
    root, session, old, report = current(tmp_path, monkeypatch)
    before = preserved(old)
    relative = f"study/openf1/reverified/{session['session_id']}"
    fresh = root / relative
    fresh.parent.mkdir()
    try:
        fresh.symlink_to(old, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"native directory symlink unavailable: {exc}")
        import subprocess

        linked = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(fresh), str(old)],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if linked.returncode != 0:
            pytest.skip("ordinary test-owned directory junction unavailable")
    report[FIELD] = relative
    bind(root, session, report)
    _, requests, _ = transport(monkeypatch, [])
    with pytest.raises(ValueError, match="linked"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert requests == [] and preserved(old) == before


def test_validated_study_measurements_read_the_candidate_bound_fresh_view(tmp_path, monkeypatch):
    from downforce_core.detailed.paths import DetailedPaths
    from downforce_core.detailed.proofs import validated_study_measurements

    root, session, old = legacy(tmp_path, monkeypatch, openf1_transport.ENDPOINTS)
    before = preserved(old)
    _, requests, _ = transport(
        monkeypatch,
        [response(body=b'[{"session_key":1,"fresh":true}]')] * len(openf1_transport.ENDPOINTS),
    )
    report = refresh(root, session)
    bind(root, session, report)
    samples = validated_study_measurements(DetailedPaths(root.parent, override=root))
    assert len(samples) == 1 and samples[0][FIELD] == report[FIELD]
    assert samples[0]["openf1_wire_exact"] and samples[0]["openf1_native_schema_exact"]
    assert len(requests) == 12 and preserved(old) == before


def test_fresh_view_requires_indexed_native_attempt_not_only_copied_source_metadata(
    tmp_path, monkeypatch
):
    from downforce_core.detailed.proofs import _enrichment

    root, session, old = legacy(tmp_path, monkeypatch)
    before = preserved(old)
    transport(monkeypatch, [response(body=b'[{"session_key":1,"fresh":true}]')])
    report = refresh(root, session)
    fresh = root / report[FIELD]
    index = json.loads((fresh / "attempts.json").read_bytes())
    for name in index["attempts"]:
        (fresh / name).unlink()
    (fresh / "attempts.json").unlink()
    report.pop("attempts")
    report.pop("attempts_index_sha256")
    report["openf1_raw_bytes"] = sum(
        path.stat().st_size
        for path in fresh.iterdir()
        if path.suffix == ".gz" or path.suffix == ".json" and path.name != "measurement.json"
    )
    save(fresh / "measurement.json", report)
    bind(root, session, report)
    with pytest.raises(ValueError, match="native HTTP attempt"):
        _enrichment(root, session, endpoints=("drivers",))
    assert preserved(old) == before
