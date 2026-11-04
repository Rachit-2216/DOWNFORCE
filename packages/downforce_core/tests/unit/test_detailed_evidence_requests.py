"""Offline transport responses exercise the bounded official capture operation."""

import importlib.util
import json
from pathlib import Path

import pytest
import requests
from downforce_core.detailed.evidence import OFFICIAL_BASE, sha256
from fastf1 import Cache

CASES = [
    (
        "2021-02-practice-1",
        2021,
        2,
        "Emilia Romagna Grand Prix",
        "Imola",
        "2021/2021-04-18_Emilia_Romagna_Grand_Prix/2021-04-16_Practice_1/",
        "2021-04-16",
    ),
    (
        "2020-01-practice-1",
        2020,
        1,
        "Austrian Grand Prix",
        "Spielberg",
        "2020/2020-07-05_Austrian_Grand_Prix/2020-07-03_Practice_1/",
        "2020-07-03",
    ),
]


def script():
    path = Path(__file__).resolve().parents[4] / "scripts/request_detailed_evidence.py"
    assert path.is_file(), "bounded evidence capture script is missing"
    spec = importlib.util.spec_from_file_location("evidence_requests_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def root(tmp_path):
    (tmp_path / "completion").mkdir()
    rows = []
    for identity, season, round_number, event, circuit, timing_path, date in CASES:
        rows.append(
            {
                "session_id": identity,
                "season": season,
                "round_number": round_number,
                "event_id": identity[:7],
                "event_name": event,
                "circuit_name": circuit,
                "session_type": "practice-1",
                "provider_session_name": "Practice 1",
                "fastf1_slot": 1,
                "provider": "fastf1",
                "scheduled_at": date + "T14:30:00+05:30",
            }
        )
        (tmp_path / "completion" / (identity + ".json")).write_text(
            json.dumps(
                {
                    "session_id": identity,
                    "timing_path": timing_path,
                    "state": "unverified",
                }
            )
        )
    (tmp_path / "inventory.json").write_text(json.dumps({"sessions": rows}))
    (tmp_path / "reconciliation-cache").mkdir()
    (tmp_path / "reconciliation-cache/fastf1_http_cache.sqlite").write_bytes(b"old exact cache")
    return tmp_path


@pytest.fixture
def offline(monkeypatch):
    calls = []
    statuses = [200, 404, 200]

    class OfflineAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append((request.url, kwargs))
            response = requests.Response()
            response.status_code = statuses[len(calls) - 1]
            response.url = request.url
            response.request = request
            response.headers.update({"X-Evidence": "exact", "Location": "https://other.invalid/"})
            response._content = b"\xef\xbb\xbfexact body\r\n\xff"
            response._content_consumed = True
            return response

        def close(self):
            pass

    adapter = OfflineAdapter()
    monkeypatch.setitem(Cache._requests_session.adapters, "https://", adapter)
    monkeypatch.setattr(Cache, "_enable_default_cache", lambda: pytest.fail("no cache init"))
    monkeypatch.setattr(Cache, "enable_cache", lambda *_a, **_k: pytest.fail("no cache enable"))
    return calls, statuses


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_success_and_404_preserve_exact_response_sources_without_old_cache_writes(root, offline):
    module = script()
    before = files(root)
    native_limits = type(Cache._requests_session)._RATE_LIMITS
    result = module.capture(root, run_id="first")
    assert result["outcome"] == "completed"
    assert len(offline[0]) == 3
    assert [url for url, _ in offline[0]] == [
        OFFICIAL_BASE + CASES[0][5] + "ArchiveStatus.json",
        OFFICIAL_BASE + CASES[1][5] + "ArchiveStatus.json",
        OFFICIAL_BASE + CASES[1][5] + "SessionStatus.jsonStream",
    ]
    for _url, kwargs in offline[0]:
        assert kwargs["timeout"] == 45
    directory = root / "evidence-requests/first"
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert len(manifest["requests"]) == 3
    for source in manifest["sources"]:
        snapshot = json.loads((root / source["path"]).read_bytes())
        original_bytes = before[source["source_path"]]
        assert bytes.fromhex(snapshot["body_hex"]) == original_bytes
        assert snapshot["body_sha256"] == source["body_sha256"] == sha256(original_bytes)
    for number, status in enumerate([200, 404, 200], 1):
        record = json.loads((directory / f"response-{number:02}.json").read_bytes())
        body = bytes.fromhex(record["body_hex"])
        assert body == b"\xef\xbb\xbfexact body\r\n\xff"
        assert record["http_status"] == status
        assert record["headers"]["X-Evidence"] == "exact"
        assert record["actual_url"] == record["request_url"] == offline[0][number - 1][0]
        assert record["body_sha256"] == sha256(body)
        assert record["observed_at"].endswith("+00:00")
        identity = manifest["requests"][number - 1]["session_id"]
        assert record["source_completion_sha256"] == sha256(before[f"completion/{identity}.json"])
    assert type(Cache._requests_session)._RATE_LIMITS is native_limits
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("status", [401, 403, 302, 500])
def test_first_denial_redirect_or_unexpected_status_is_saved_and_stops(root, offline, status):
    module = script()
    offline[1][0] = status
    with pytest.raises((PermissionError, ValueError)):
        module.capture(root, run_id="stopped")
    assert len(offline[0]) == 1  # Location must never trigger an alternate host.
    directory = root / "evidence-requests/stopped"
    response = json.loads((directory / "response-01.json").read_bytes())
    assert response["http_status"] == status
    assert bytes.fromhex(response["body_hex"]) == b"\xef\xbb\xbfexact body\r\n\xff"
    assert response["headers"]["Location"] == "https://other.invalid/"
    outcome = json.loads((directory / "outcome.json").read_bytes())
    assert outcome["outcome"] == "failed"
    assert len(outcome["responses"]) == 1
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert len(manifest["planned_requests"]) == 3
    assert len(manifest["requests"]) == 1


def test_capture_persistence_failure_stops_before_next_request(root, offline, monkeypatch):
    module = script()
    original = module.publish_proof

    def fail_response(data_root, relative, value):
        if "response-" in relative:
            raise OSError("disk full")
        return original(data_root, relative, value)

    monkeypatch.setattr(module, "publish_proof", fail_response)
    with pytest.raises(OSError, match="disk full"):
        module.capture(root, run_id="failed-observer")
    assert len(offline[0]) == 1
    outcome = json.loads((root / "evidence-requests/failed-observer/outcome.json").read_bytes())
    assert outcome["error"]["type"] == "OSError"
    assert outcome["responses"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "2022-index"),
        ("timing_path", "2021/other_event/other_session/"),
        ("timing_path", "2021/../2022/Index.json/"),
        ("timing_path", "https://other.invalid/"),
    ],
)
def test_wrong_completion_identity_or_path_rejected_before_http(root, offline, field, value):
    module = script()
    path = root / "completion" / (CASES[0][0] + ".json")
    source = json.loads(path.read_bytes())
    source[field] = value
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="identity|path"):
        module.capture(root, run_id="bad-source")
    assert offline[0] == []


def test_discovery_identity_rejected_before_http(root, offline):
    module = script()
    path = root / "inventory.json"
    discovery = json.loads(path.read_bytes())
    discovery["sessions"][0]["event_name"] = "Wrong event"
    path.write_text(json.dumps(discovery))
    with pytest.raises(ValueError, match="identity"):
        module.capture(root, run_id="bad-discovery")
    assert offline[0] == []


def test_existing_evidence_run_never_overwritten(root, offline):
    module = script()
    module.capture(root, run_id="immutable")
    before = files(root / "evidence-requests/immutable")
    with pytest.raises(FileExistsError):
        module.capture(root, run_id="immutable")
    assert len(offline[0]) == 3
    assert files(root / "evidence-requests/immutable") == before


def test_linked_output_path_rejected_before_http(root, offline, monkeypatch):
    module = script()
    monkeypatch.setattr(Path, "is_junction", lambda p: p.name == "evidence-requests")
    with pytest.raises(ValueError, match="linked"):
        module.capture(root, run_id="unsafe")
    assert offline[0] == []
    assert not (root / "evidence-requests").exists()


def test_transport_exception_preserves_attempt_provenance_without_retry(root, offline, monkeypatch):
    module = script()

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("normal transport timeout")

    monkeypatch.setattr(Cache._requests_session.adapters["https://"], "send", timeout)
    with pytest.raises(requests.Timeout, match="normal transport timeout"):
        module.capture(root, run_id="timeout")
    outcome = json.loads((root / "evidence-requests/timeout/outcome.json").read_bytes())
    assert outcome["outcome"] == "failed"
    assert len(outcome["attempts"]) == 1
    assert outcome["responses"] == []
    assert outcome["error"]["type"] == "Timeout"
    assert len(json.loads((root / "runtime/fastf1-budget.json").read_bytes())["timestamps"]) == 1
    assert offline[0] == []


def test_preconfigured_http_cache_rejected_before_native_request(root, offline, monkeypatch):
    module = script()
    monkeypatch.setattr(Cache, "_CACHE_DIR", str(root / "old-cache"))
    with pytest.raises(ValueError, match="fresh uncached"):
        module.capture(root, run_id="not-fresh")
    assert offline[0] == []
    assert not (root / "runtime").exists()


def test_redirect_switch_is_explicit_at_native_get_boundary(root, offline, monkeypatch):
    module = script()
    original = Cache._requests_session.get
    arguments = []

    def get(*args, **kwargs):
        arguments.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(Cache._requests_session, "get", get)
    module.capture(root, run_id="no-redirects")
    assert arguments == [{"allow_redirects": False, "timeout": 45}] * 3


@pytest.mark.parametrize("status", [200, 302, 401, 403])
def test_truncated_body_records_available_metadata_without_inventing_bytes(
    root, offline, monkeypatch, status
):
    from urllib3.exceptions import ProtocolError

    module = script()
    calls = []
    closed = []

    class BrokenRaw:
        def stream(self, _amount, decode_content=True):
            yield b"incomplete bytes"
            raise ProtocolError("truncated provider response")

        def read(self, *args, **kwargs):
            raise ProtocolError("truncated provider response")

        def close(self):
            closed.append("close")

        def release_conn(self):
            closed.append("release")

    def broken_response(request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers["X-Denial-Provenance"] = "retained"
        if status == 302:
            response.headers["Location"] = "https://other.invalid/must-not-follow"
        response.raw = BrokenRaw()
        return response

    monkeypatch.setattr(Cache._requests_session.adapters["https://"], "send", broken_response)
    expected = PermissionError if status in (401, 403) else requests.exceptions.ChunkedEncodingError
    with pytest.raises(expected):
        module.capture(root, run_id="truncated")
    directory = root / "evidence-requests/truncated"
    assert len(calls) == 1
    assert closed == ["close", "release"]
    assert (directory / "response-01.json").is_file(), "received status/headers were lost"
    record = json.loads((directory / "response-01.json").read_bytes())
    assert record["http_status"] == status
    assert record["headers"]["X-Denial-Provenance"] == "retained"
    assert record["actual_url"] == record["request_url"] == calls[0]
    assert record["prepared_request_url"] == calls[0]
    assert record["observed_at"].endswith("+00:00")
    assert record["source_completion_sha256"] == sha256(
        (root / "completion" / (CASES[0][0] + ".json")).read_bytes()
    )
    assert record["body_available"] is False
    assert record["body_error"]["type"] == "ChunkedEncodingError"
    assert not {"body_hex", "body_bytes", "body_sha256"}.intersection(record)
    outcome = json.loads((directory / "outcome.json").read_bytes())
    assert outcome["outcome"] == "failed"
    assert len(outcome["attempts"]) == len(outcome["responses"]) == 1
    if status in (401, 403):
        assert outcome["error"]["type"] == "PermissionError"
        assert outcome["error"]["cause_type"] == "ChunkedEncodingError"


def test_default_capture_run_ids_are_safe_unique_and_never_reuse_evidence(root, offline):
    from downforce_core.domain.identifiers import validate_safe_identifier

    module = script()
    offline[1].extend([200, 404, 200])
    first = module.capture(root)
    first_directory = root / "evidence-requests" / first["run_id"]
    before = files(first_directory)
    second = module.capture(root)
    assert first["run_id"] != second["run_id"]
    for outcome in (first, second):
        assert validate_safe_identifier(outcome["run_id"]) == outcome["run_id"]
        assert outcome["run_id"] == outcome["run_id"].lower()
        assert outcome["outcome"] == "completed"
        directory = root / "evidence-requests" / outcome["run_id"]
        assert directory.is_dir()
        assert json.loads((directory / "outcome.json").read_bytes()) == outcome
    assert len(offline[0]) == 6
    assert files(first_directory) == before


def test_default_cli_entrypoint_runs_with_offline_native_transport(
    root, offline, monkeypatch, capsys
):
    import runpy
    import sys

    from downforce_core.domain.identifiers import validate_safe_identifier

    path = Path(__file__).resolve().parents[4] / "scripts/request_detailed_evidence.py"
    monkeypatch.setattr(sys, "argv", [str(path), "--root", str(root)])
    runpy.run_path(str(path), run_name="__main__")
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["outcome"] == "completed"
    assert validate_safe_identifier(outcome["run_id"]) == outcome["run_id"]
    assert (root / "evidence-requests" / outcome["run_id"] / "manifest.json").is_file()
    assert len(offline[0]) == 3


@pytest.mark.parametrize("run_id", ["UPPERCASE", "20261002T052148Z-run", "../escape"])
def test_explicit_unsafe_run_id_is_rejected_without_normalization_or_http(root, offline, run_id):
    module = script()
    with pytest.raises(ValueError):
        module.capture(root, run_id=run_id)
    assert offline[0] == []
    assert not (root / "evidence-requests").exists()
