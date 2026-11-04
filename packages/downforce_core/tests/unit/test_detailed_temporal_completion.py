"""Date rollover uses new captured authority without mutating frozen sources."""

import importlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from downforce_core.detailed.evidence import parse_status_stream, sha256
from test_detailed_evidence import finalizer
from test_detailed_study_runner import native_session, run, runner, save

POLICY = "status-wire-terminal-aux-v1"
INDEX = "evidence-requests/temporal-index-test"
RUN = "evidence-requests/temporal-sessions-test"
BODY = (
    b'00:00:01.000{"Status":"Started","Started":"Started"}\r\n'
    b'00:01:00.000{"Status":"Finished","Started":"Finished"}\r\n'
    b'00:02:00.000{"Status":"Finalised","Started":"Finished"}\r\n'
    b'00:02:00.000{"Status":"Ends","Started":"Finished"}\r\n'
)


def temporal():
    return importlib.import_module("downforce_core.detailed.temporal_completion")


def read(root, relative):
    return json.loads((root / relative).read_bytes())


def digest(root, relative):
    return sha256((root / relative).read_bytes())


def write(root, relative, value):
    save(root / relative, value)


def response(url, body, observed="2026-01-03T00:00:02+00:00", **fields):
    return {
        **fields,
        "request_url": url,
        "actual_url": url,
        "transport_request_url": url,
        "http_status": 200,
        "headers": {"Content-Type": "application/json", "Content-Length": str(len(body))},
        "header_fields": [["Content-Type", "application/json"], ["Content-Length", str(len(body))]],
        "body_hex": body.hex(),
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "observed_at": observed,
        "completion_promotion": False,
    }


@pytest.fixture
def captured(tmp_path):
    entries = [
        {
            "session_id": f"2026-01-practice-{n}",
            "event_id": "2026-01",
            "season": 2026,
            "round_number": 1,
            "event_name": "Example Grand Prix",
            "country": "Example",
            "circuit_name": "Example Circuit",
            "session_type": f"practice-{n}",
            "provider_session_name": f"Practice {n}",
            "scheduled_at": f"2026-01-02T0{n}:00:00+00:00",
            "completion": "future",
            "fastf1_slot": n,
            "fastf1_supported": True,
            "provider": "fastf1",
        }
        for n in (1, 2)
    ]
    write(
        tmp_path,
        "inventory.json",
        {
            "complete_inventory": True,
            "discovered_at": "2026-01-01T00:00:00+00:00",
            "sessions": entries,
            "fastf1_version": "3.8.3",
        },
    )
    sources = []
    sessions = []
    for n, entry in enumerate(entries, 1):
        write(
            tmp_path,
            f"completion/{entry['session_id']}.json",
            {
                "session_id": entry["session_id"],
                "state": "future",
                "statuses": [],
                "times": [],
                "error": None,
                "timing_key": None,
                "timing_path": None,
                "observed_at": "2026-01-01T00:01:00+00:00",
                "fastf1_version": "3.8.3",
            },
        )
        path = f"2026/2026-01-04_Example_Grand_Prix/2026-01-02_Practice_{n}/"
        sessions.append(
            {
                "Key": n + 100,
                "Type": "Practice",
                "Number": n,
                "Name": f"Practice {n}",
                "StartDate": f"2026-01-02T0{n}:00:00",
                "EndDate": f"2026-01-02T0{n + 1}:00:00",
                "GmtOffset": "00:00:00",
                "Path": path,
            }
        )
        sources.append(
            {
                "session_key": n + 100,
                "session_type": "Practice",
                "session_name": f"Practice {n}",
                "date_start": entry["scheduled_at"],
                "date_end": f"2026-01-02T0{n + 1}:00:00+00:00",
                "meeting_key": 10,
                "circuit_key": 12,
                "circuit_short_name": "Circuit",
                "country_key": 36,
                "country_code": "EXP",
                "country_name": "Example",
                "location": "Example Circuit",
                "gmt_offset": "00:00:00",
                "year": 2026,
                "is_cancelled": False,
            }
        )
    write(tmp_path, "openf1-inventory.json", {"sessions": sources, "failures": []})
    discovery_digest = digest(tmp_path, "inventory.json")
    index_url = "https://livetiming.formula1.com/static/2026/Index.json"
    index_body = json.dumps(
        {
            "Year": 2026,
            "Meetings": [
                {
                    "Number": 1,
                    "Key": 10,
                    "Name": "Example Grand Prix",
                    "Location": "Example Circuit",
                    "Country": {"Key": 36, "Code": "EXP", "Name": "Example"},
                    "Circuit": {"Key": 12, "ShortName": "Circuit"},
                    "Sessions": sessions,
                }
            ],
        }
    ).encode()
    write(
        tmp_path,
        INDEX + "/intent.json",
        {
            "schema_version": "temporal-index-capture-v1",
            "request_url": index_url,
            "allow_redirects": False,
            "started_at": "2026-01-03T00:00:00+00:00",
            "discovery_sha256": discovery_digest,
            "source_discovery_hex": (tmp_path / "inventory.json").read_bytes().hex(),
            "completion_promotion": False,
            "fastf1_version": "3.8.3",
        },
    )
    write(
        tmp_path,
        INDEX + "/response.json",
        response(
            index_url,
            index_body,
            observed="2026-01-03T00:00:01+00:00",
            schema_version="temporal-index-response-v1",
            discovery_sha256=discovery_digest,
        ),
    )
    write(
        tmp_path,
        INDEX + "/outcome.json",
        {
            "schema_version": "temporal-index-outcome-v1",
            "request_url": index_url,
            "run_path": INDEX,
            "finished_at": "2026-01-03T00:00:01+00:00",
            "responses": 1,
            "http_status": 200,
            "error": None,
            "completion_promotion": False,
        },
    )
    index_digest = digest(tmp_path, INDEX + "/response.json")
    planned = []
    for entry, session, provider in zip(entries, sessions, sources, strict=True):
        for resource in ("SessionStatus.jsonStream", "ArchiveStatus.json"):
            planned.append(
                {
                    "session_id": entry["session_id"],
                    "request_url": "https://livetiming.formula1.com/static/"
                    + session["Path"]
                    + resource,
                    "timing_path": session["Path"],
                    "timing_key": session["Key"],
                    "resource": resource,
                    "source_completion_sha256": digest(
                        tmp_path, f"completion/{entry['session_id']}.json"
                    ),
                    "scheduled_at": entry["scheduled_at"],
                    "scheduled_end_at": provider["date_end"],
                }
            )
    write(
        tmp_path,
        RUN + "/intent.json",
        {
            "schema_version": "temporal-session-capture-v1",
            "started_at": "2026-01-03T00:00:02+00:00",
            "index_capture_path": INDEX + "/response.json",
            "index_capture_sha256": index_digest,
            "discovery_sha256": discovery_digest,
            "openf1_inventory_sha256": digest(tmp_path, "openf1-inventory.json"),
            "planned_requests": planned,
            "completion_promotion": False,
            "allow_redirects": False,
            "fastf1_version": "3.8.3",
        },
    )
    outcomes = []
    for n, request in enumerate(planned, 1):
        body = (
            BODY if request["resource"] == "SessionStatus.jsonStream" else b'{"Status":"Complete"}'
        )
        relative = f"{RUN}/response-{n:02d}.json"
        # Actual captures use local ISO strings; compare instants, preserve both strings.
        local = dict(request)
        local["scheduled_at"] = (
            datetime.fromisoformat(request["scheduled_at"])
            .astimezone(timezone(timedelta(hours=5, minutes=30)))
            .isoformat()
        )
        write(
            tmp_path,
            relative,
            response(
                request["request_url"],
                body,
                **{
                    **local,
                    "schema_version": "temporal-session-response-v1",
                    "index_capture_path": INDEX + "/response.json",
                    "index_capture_sha256": index_digest,
                    "discovery_sha256": discovery_digest,
                },
            ),
        )
        outcomes.append(
            {
                "path": relative,
                "sha256": digest(tmp_path, relative),
                "session_id": request["session_id"],
                "resource": request["resource"],
                "http_status": 200,
            }
        )
    write(
        tmp_path,
        RUN + "/outcome.json",
        {
            "schema_version": "temporal-session-outcome-v1",
            "run_path": RUN,
            "finished_at": "2026-01-03T00:00:03+00:00",
            "planned_count": len(planned),
            "responses": outcomes,
            "error": None,
            "completion_promotion": False,
        },
    )
    write(tmp_path, "reconciled-inventory.json", {"complete": True, "index_failures": []})
    return tmp_path, entries


def test_auxiliary_policy_is_explicit_and_keeps_legacy_interpretation():
    legacy = parse_status_stream(BODY)
    assert legacy["valid"] is False
    assert legacy["parser_policy_version"] == "status-wire-v1"
    parsed = parse_status_stream(BODY, terminal_policy=POLICY)
    assert parsed["valid"] is True
    assert parsed["source_bytes"] == BODY
    assert parsed["parser_policy_version"] == POLICY
    assert parsed["notes"] == [{"record": 4, "code": "coincident-terminal-progression"}]
    assert parsed["records"] == legacy["records"]
    assert parse_status_stream(BODY) == legacy


@pytest.mark.parametrize(
    "previous,current",
    [
        ({"Status": "Finalised", "aux": True}, {"Status": "Ends", "aux": 1}),
        ({"Status": "Finalised", "aux": 1}, {"Status": "Ends", "aux": 1.0}),
        ({"Status": "Finalised", "aux": {"x": True}}, {"Status": "Ends", "aux": {"x": 1}}),
        ({"Status": "Ends", "aux": 1}, {"Status": "Finalised", "aux": 1}),
        ({"Status": "Finished", "aux": 1}, {"Status": "Started", "aux": 1}),
        ({"Status": "Finalised", "aux": 1}, {"Status": "Ends"}),
    ],
)
def test_terminal_policy_never_waives_payload_changes_or_reverse_restart(previous, current):
    body = b"\n".join(b"00:00:01.000" + json.dumps(row).encode() for row in (previous, current))
    assert parse_status_stream(body, terminal_policy=POLICY)["valid"] is False


def test_terminal_policy_rejects_overflowing_json_number():
    body = b'00:00:01.000{"Status":"Finalised","aux":1e999}\n'
    assert parse_status_stream(body, terminal_policy=POLICY)["valid"] is False


def test_captured_rollover_build_and_exact_loader_leave_originals_immutable(captured):
    root, entries = captured
    original_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    assert proof["state"] == "completed"
    assert proof["source_classification"] == "future"
    assert proof["effective_entry"]["completion"] == "past-unverified"
    assert proof["parser"]["parser_policy_version"] == POLICY
    assert proof["archive"] == {"Status": "Complete"}
    assert all(path.read_bytes() == raw for path, raw in original_files.items())
    write(root, f"completion-temporal/{entries[0]['session_id']}.json", proof)
    proofs, hashes = temporal().load_temporal_completions(root, entries)
    assert proofs == {entries[0]["session_id"]: proof}
    assert hashes == {
        entries[0]["session_id"]: digest(
            root, f"completion-temporal/{entries[0]['session_id']}.json"
        )
    }
    effective = temporal().effective_source_view(entries, proofs, hashes)
    assert effective[entries[1]["session_id"]] == entries[1]
    assert entries[0]["completion"] == "future"
    assert (
        effective[entries[0]["session_id"]]["completion_temporal_sha256"]
        == hashes[entries[0]["session_id"]]
    )


def replace_response(root, n, mutate):
    relative = f"{RUN}/response-{n:02d}.json"
    value = read(root, relative)
    mutate(value)
    write(root, relative, value)
    outcome = read(root, RUN + "/outcome.json")
    outcome["responses"][n - 1]["sha256"] = digest(root, relative)
    write(root, RUN + "/outcome.json", outcome)


def rebind_index(root, mutate):
    relative = INDEX + "/response.json"
    value = read(root, relative)
    body = json.loads(bytes.fromhex(value["body_hex"]))
    mutate(body)
    raw = json.dumps(body).encode()
    value.update(body_hex=raw.hex(), body_bytes=len(raw), body_sha256=sha256(raw))
    value["headers"]["Content-Length"] = str(len(raw))
    value["header_fields"][1][1] = str(len(raw))
    write(root, relative, value)
    new_digest = digest(root, relative)
    intent = read(root, RUN + "/intent.json")
    intent["index_capture_sha256"] = new_digest
    write(root, RUN + "/intent.json", intent)
    for n in range(1, len(intent["planned_requests"]) + 1):
        replace_response(root, n, lambda record: record.update(index_capture_sha256=new_digest))


def test_unrelated_testing_or_other_meeting_numbers_do_not_rewrite_target_identity(captured):
    root, entries = captured

    def mutate(index):
        other = deepcopy(index["Meetings"][0])
        other.update(Name="Pre-Season Testing", Key=11)
        for row in other["Sessions"]:
            row["Key"] += 1000
        index["Meetings"].insert(0, other)

    rebind_index(root, mutate)
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    assert proof["effective_entry"]["event_name"] == entries[0]["event_name"]
    assert proof["state"] == "completed"


@pytest.mark.parametrize(
    "problem", ["target-duplicate", "global-key-conflict", "provider-date", "provider-key-alias"]
)
def test_index_and_provider_conflicts_cannot_be_hidden_by_exact_target_selection(captured, problem):
    root, entries = captured

    def mutate(index):
        if problem == "target-duplicate":
            other = deepcopy(index["Meetings"][0])
            other["Key"] += 1
            for row in other["Sessions"]:
                row["Key"] += 1000
            index["Meetings"].append(other)
        elif problem == "global-key-conflict":
            other = deepcopy(index["Meetings"][0])
            other.update(Name="Other", Number=2)
            index["Meetings"].append(other)

    if problem.startswith("provider-"):
        value = read(root, "openf1-inventory.json")
        if problem == "provider-date":
            value["sessions"][0]["date_end"] = "2026-01-02T04:00:00+00:00"
        else:
            value["sessions"][0]["session_key"] = 101.0
        write(root, "openf1-inventory.json", value)
        intent = read(root, RUN + "/intent.json")
        intent["openf1_inventory_sha256"] = digest(root, "openf1-inventory.json")
        write(root, RUN + "/intent.json", intent)
    else:
        rebind_index(root, mutate)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize("resource", ["status", "archive"])
def test_entire_recorded_batch_semantics_are_replayed_not_just_the_requested_slot(
    captured, resource
):
    root, entries = captured
    body = b"00:00:01.000{broken}" if resource == "status" else b'{"Status":"Generating"}'

    def mutate(value):
        value.update(body_hex=body.hex(), body_bytes=len(body), body_sha256=sha256(body))
        value["headers"]["Content-Length"] = str(len(body))
        value["header_fields"][1][1] = str(len(body))

    replace_response(root, 3 if resource == "status" else 4, mutate)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize(
    "problem",
    [
        "source-changed",
        "discovery-changed",
        "provider-changed",
        "index-changed",
        "entry-alias",
        "response-url",
        "transport-url",
        "redirect",
        "http-alias",
        "body-sha",
        "body-count",
        "response-source",
        "response-index",
        "response-key",
        "response-date",
        "response-naive",
        "response-future",
        "response-before-end",
        "status-malformed",
        "status-restart",
        "status-aux-change",
        "status-nonfinite",
        "archive-incomplete",
        "archive-extra",
        "plan-duplicate",
        "plan-unrecorded",
        "outcome-duplicate",
        "outcome-alias",
        "outcome-error",
        "unrecorded-response",
        "header-mismatch",
        "linked-response",
        "unknown-session",
    ],
)
def test_replay_rejects_corruption_even_when_capture_file_hash_is_rebound(captured, problem):
    root, entries = captured
    entry = deepcopy(entries[0])
    if problem in {"source-changed", "discovery-changed", "provider-changed", "index-changed"}:
        relative = {
            "source-changed": f"completion/{entry['session_id']}.json",
            "discovery-changed": "inventory.json",
            "provider-changed": "openf1-inventory.json",
            "index-changed": INDEX + "/response.json",
        }[problem]
        (root / relative).write_bytes((root / relative).read_bytes() + b" ")
    elif problem == "entry-alias":
        entry["fastf1_slot"] = True
    elif problem == "unrecorded-response":
        write(root, RUN + "/response-05.json", read(root, RUN + "/response-01.json"))
    elif problem == "linked-response":
        target = root / RUN / "response-01.json"
        alias = root / "alias.json"
        target.rename(alias)
        try:
            target.symlink_to(alias)
        except OSError:
            pytest.skip("Windows symlink privilege unavailable")
    elif problem.startswith("plan-") or problem == "unknown-session":
        intent = read(root, RUN + "/intent.json")
        if problem == "plan-duplicate":
            intent["planned_requests"][1] = intent["planned_requests"][0]
        elif problem == "unknown-session":
            intent["planned_requests"][0]["session_id"] = "2026-99-practice-1"
        else:
            intent["planned_requests"].pop()
        write(root, RUN + "/intent.json", intent)
    elif problem.startswith("outcome-"):
        value = read(root, RUN + "/outcome.json")
        if problem == "outcome-duplicate":
            value["responses"][1] = value["responses"][0]
        elif problem == "outcome-alias":
            value["planned_count"] = 4.0
        else:
            value["error"] = "TransportError"
        write(root, RUN + "/outcome.json", value)
    else:
        fields = {
            "response-url": ("actual_url", "https://example.test/redirect"),
            "transport-url": ("transport_request_url", "https://example.test/substitute"),
            "redirect": ("redirects", 1),
            "http-alias": ("http_status", 200.0),
            "body-sha": ("body_sha256", "0" * 64),
            "body-count": ("body_bytes", 1),
            "response-source": ("source_completion_sha256", "0" * 64),
            "response-index": ("index_capture_sha256", "0" * 64),
            "response-key": ("timing_key", 101.0),
            "response-date": ("scheduled_at", "2026-01-02T03:00:00+00:00"),
            "response-naive": ("observed_at", "2026-01-03T00:00:02"),
            "response-future": ("observed_at", "2999-01-03T00:00:02+00:00"),
            "response-before-end": ("observed_at", "2026-01-01T00:00:02+00:00"),
            "header-mismatch": ("header_fields", [["Content-Length", "1"]]),
        }
        bodies = {
            "status-malformed": b"00:00:01.000{broken}",
            "status-restart": BODY + b'00:03:00.000{"Status":"Started"}',
            "status-aux-change": BODY.replace(
                b'{"Status":"Ends","Started":"Finished"}', b'{"Status":"Ends","Started":"Started"}'
            ),
            "status-nonfinite": BODY.replace(b'"Started":"Finished"', b'"Started":1e999'),
            "archive-incomplete": b'{"Status":"Generating"}',
            "archive-extra": b'{"Status":"Complete","additional":true}',
        }
        if problem in bodies:
            body = bodies[problem]

            def mutate(value):
                value.update(body_hex=body.hex(), body_bytes=len(body), body_sha256=sha256(body))
                value["headers"]["Content-Length"] = str(len(body))
                value["header_fields"][1][1] = str(len(body))

            replace_response(root, 2 if problem.startswith("archive-") else 1, mutate)
        else:
            key, value = fields[problem]
            replace_response(root, 1, lambda row: row.update({key: value}))
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entry, RUN)


@pytest.mark.parametrize(
    "key,value",
    [
        ("state", "future"),
        ("archive", {"Status": "Generating"}),
        ("effective_entry", {"completion": "past-unverified"}),
        ("unexpected", True),
        ("source_classification", "completed"),
        ("time_nanoseconds", [1, 2, 3, 4]),
    ],
)
def test_published_temporal_proof_is_exact_typed_replay_not_a_promotion_flag(captured, key, value):
    root, entries = captured
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    proof[key] = value
    write(root, f"completion-temporal/{entries[0]['session_id']}.json", proof)
    with pytest.raises(ValueError):
        temporal().load_temporal_completions(root, entries)


def publish(captured):
    root, entries = captured
    for entry in entries:
        proof = temporal().build_temporal_completion(root, entry, RUN)
        write(root, f"completion-temporal/{entry['session_id']}.json", proof)
    return temporal().load_temporal_completions(root, entries)


def ready(captured):
    root, entries = captured
    publish(captured)
    result = finalizer().finalize(root, offline=True)
    catalog, effective = runner().ready_inventory(root)
    assert result == catalog
    assert all(effective[row["session_id"]]["completion"] == "past-unverified" for row in entries)
    return result, effective


def test_offline_finalizer_and_readiness_use_bound_effective_sources_only(captured, monkeypatch):
    root, entries = captured
    monkeypatch.setattr(
        finalizer().fastf1.Cache, "requests_get", lambda *_a, **_k: pytest.fail("HTTP forbidden")
    )
    frozen = {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != "reconciled-inventory.json"
    }
    result, effective = ready(captured)
    assert result["summary"] == {"completed": 2}
    assert result["provider_states"] == {"completed": 2}
    assert result["completion_evidence_sha256"] == {
        row["session_id"]: digest(root, f"completion/{row['session_id']}.json") for row in entries
    }
    assert result["archive_evidence_sha256"] == {}
    assert set(result["completion_temporal_sha256"]) == {row["session_id"] for row in entries}
    for row in result["sessions"]:
        original = next(e for e in entries if e["session_id"] == row["session_id"])
        assert all(row[key] == value for key, value in original.items() if key != "completion")
        assert row["source_classification"] == "future"
        assert row["source_observation"] == read(root, f"completion/{row['session_id']}.json")
        assert (
            row["completion_temporal_sha256"]
            == effective[row["session_id"]]["completion_temporal_sha256"]
        )
    canonical_bytes = (root / "reconciled-inventory.json").read_bytes()
    assert finalizer().finalize(root, offline=True) == result
    assert (root / "reconciled-inventory.json").read_bytes() == canonical_bytes
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert not (root / "archive-finalization").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "map-missing",
        "map-stale",
        "proof-missing",
        "original-map-stale",
        "completion-forged",
        "country-forged",
        "circuit-forged",
        "slot-alias",
        "statuses-forged",
        "times-forged",
        "archive-forged",
        "source-observation-forged",
        "temporal-marker-forged",
        "provider-forged",
    ],
)
def test_ready_replays_temporal_proofs_and_rejects_canonical_aliases(captured, problem):
    root, entries = captured
    ready(captured)
    catalog = read(root, "reconciled-inventory.json")
    identity = entries[0]["session_id"]
    row = catalog["sessions"][0]
    if problem == "map-missing":
        catalog.pop("completion_temporal_sha256")
    elif problem == "map-stale":
        catalog["completion_temporal_sha256"][identity] = "0" * 64
    elif problem == "proof-missing":
        (root / f"completion-temporal/{identity}.json").unlink()
    elif problem == "original-map-stale":
        catalog["completion_evidence_sha256"][identity] = "0" * 64
    elif problem == "provider-forged":
        catalog["provider_entries"][0]["date_end"] = "2999-01-01T00:00:00+00:00"
    else:
        key, value = {
            "completion-forged": ("completion", "future"),
            "country-forged": ("country", "Other"),
            "circuit-forged": ("circuit_name", "Other"),
            "slot-alias": ("fastf1_slot", True),
            "statuses-forged": ("statuses", ["Started", "Ends"]),
            "times-forged": ("times", ["0:00:01"]),
            "archive-forged": ("archive_status", {"Status": "Generating"}),
            "source-observation-forged": ("source_observation", {"state": "completed"}),
            "temporal-marker-forged": ("completion_temporal_sha256", "0" * 64),
        }[problem]
        row[key] = value
    write(root, "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError):
        runner().ready_inventory(root)


def proof_native(proof):
    session = native_session()
    session.session_status = pd.DataFrame(
        {
            "Status": proof["evidence"]["statuses"],
            "Time": pd.to_timedelta(proof["time_nanoseconds"], unit="ns"),
        }
    )
    return session


def test_native_acquisition_and_reuse_bind_current_temporal_proof(captured):
    root, entries = captured
    _, effective = ready(captured)
    proofs, hashes = temporal().load_temporal_completions(root, entries)
    observed = []

    def loader(entry, _cache):
        observed.append(entry)
        return proof_native(proofs[entry["session_id"]])

    result = run(root, loader)
    assert result["failed"] == 0 and result["attempted"] == 2
    assert observed == list(effective.values())
    for entry in entries:
        value = read(root, f"preflight/{entry['session_id']}/measurement.json")
        assert value["session"] == effective[entry["session_id"]]
        assert value["completion_temporal_sha256"] == hashes[entry["session_id"]]
    reused = run(root, lambda *_a: pytest.fail("reuse must not call provider"))
    assert reused["attempted"] == 0 and reused["reused"] == 2


@pytest.mark.parametrize(
    "problem", ["status", "one-nanosecond", "order", "marker", "session-completion"]
)
def test_temporal_native_reuse_requires_exact_status_nanos_and_current_binding(captured, problem):
    root, entries = captured
    ready(captured)
    proofs, _ = temporal().load_temporal_completions(root, entries)
    assert run(root, lambda entry, _cache: proof_native(proofs[entry["session_id"]]))["failed"] == 0
    identity = entries[0]["session_id"]
    primary = root / f"preflight/{identity}/measurement.json"
    value = read(root, primary.relative_to(root).as_posix())
    attempts = [
        p for p in primary.parent.glob("*/measurement.json") if json.loads(p.read_bytes()) == value
    ]
    assert len(attempts) == 1
    if problem in {"marker", "session-completion"}:
        if problem == "marker":
            value["completion_temporal_sha256"] = "0" * 64
        else:
            value["session"]["completion"] = "future"
    else:
        path = attempts[0].parent / "session_status.parquet"
        frame = pd.read_parquet(path)
        if problem == "status":
            frame.loc[0, "Status"] = "Inactive"
        elif problem == "one-nanosecond":
            frame.loc[0, "Time"] += pd.Timedelta(1, unit="ns")
        else:
            frame = frame.iloc[::-1].reset_index(drop=True)
            frame["source_index"] = range(len(frame))
        frame.to_parquet(path, index=False)
        value["tables"]["session_status"].update(
            bytes=path.stat().st_size, sha256=sha256(path.read_bytes())
        )
    for path in (primary, attempts[0]):
        write(root, path.relative_to(root).as_posix(), value)
    with pytest.raises(ValueError):
        run(root, lambda *_a: pytest.fail("bad reuse must fail before provider"))


@pytest.mark.parametrize("wrong", [True, False])
def test_default_loader_requires_retained_verified_timing_path(captured, monkeypatch, wrong):
    root, _ = captured
    _, effective = ready(captured)
    entry = next(iter(effective.values()))
    session = proof_native(
        temporal().load_temporal_completions(root, captured[1])[0][entry["session_id"]]
    )
    session.api_path = "/static/" + ("2026/wrong/" if wrong else entry["timing_path"])
    called = []
    session.load = lambda **_kwargs: called.append(True)
    monkeypatch.setattr(runner().fastf1, "get_session", lambda *_a: session)
    monkeypatch.setattr(runner().fastf1.Cache, "enable_cache", lambda *_a: None)
    monkeypatch.setattr(runner().fastf1.Cache, "offline_mode", lambda *_a: None)
    if wrong:
        with pytest.raises(ValueError):
            runner()._load_fastf1(entry, root / "unused-cache")
        assert not called
    else:
        assert runner()._load_fastf1(entry, root / "unused-cache") is session
        assert called == [True]


def test_current_candidate_accounts_temporal_closure_without_rewriting_report_formula(captured):
    from downforce_core.detailed.proofs import _candidate
    from test_detailed_proofs import candidate_fixture

    root, entries = captured
    result, effective = ready(captured)
    proofs, _ = temporal().load_temporal_completions(root, entries)
    assert run(root, lambda entry, _cache: proof_native(proofs[entry["session_id"]]))["failed"] == 0
    identity = entries[0]["session_id"]
    primary = read(root, f"preflight/{identity}/measurement.json")
    attempt = next(
        p.parent
        for p in (root / f"preflight/{identity}").glob("*/measurement.json")
        if json.loads(p.read_bytes()) == primary
    )
    _, report = candidate_fixture(root, primary, attempt)
    before = read(root, f"study/session-measurements/{identity}.json")
    row = next(row for row in result["sessions"] if row["session_id"] == identity)
    sample = _candidate(root, report, row, effective[identity])
    files = {f"completion-temporal/{identity}.json", *proofs[identity]["dependencies"]}
    files.remove(f"completion/{identity}.json")  # Already in strict historical reported metadata.
    charge = sum((root / relative).stat().st_size for relative in files)
    legacy_proofs = sum(
        (root / relative).stat().st_size
        for relative in (
            f"study/session-measurements/{identity}.json",
            f"study/compression/{identity}-deflate-6-metrics-v2.json",
            f"preflight/{identity}/measurement.json",
        )
    )
    assert sample["temporal_authority_metadata_bytes"] == charge
    assert sample["temporal_authority_file_count"] == len(files)
    assert (
        sample["temporal_authority_cost_basis"] == "conservative-full-verified-closure-per-sample"
    )
    assert sample["reported_metadata_bytes"] == report["metadata_bytes"]
    assert sample["study_proof_metadata_bytes"] == legacy_proofs + charge
    assert sample["metadata_bytes"] == report["metadata_bytes"] + legacy_proofs + charge
    assert read(root, f"study/session-measurements/{identity}.json") == before


@pytest.mark.parametrize("gate", ["loader", "capture", "reuse", "resume"])
def test_temporal_authority_is_current_at_every_native_boundary(captured, monkeypatch, gate):
    root, entries = captured
    ready(captured)
    proofs, _ = temporal().load_temporal_completions(root, entries)
    identity = entries[0]["session_id"]
    proof_path = root / f"completion-temporal/{identity}.json"

    def stale():
        proof_path.write_bytes(proof_path.read_bytes() + b" ")

    def loader(entry, _cache):
        if gate == "loader":
            stale()
        return proof_native(proofs[entry["session_id"]])

    if gate == "capture":
        capture = runner().capture_native_tables

        def damaged_capture(*args):
            result = capture(*args)
            stale()
            return result

        monkeypatch.setattr(runner(), "capture_native_tables", damaged_capture)
    if gate == "resume":
        publish_once = runner()._publish_once
        monkeypatch.setattr(
            runner(), "_publish_once", lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        with pytest.raises(KeyboardInterrupt):
            run(root, loader)
        monkeypatch.setattr(runner(), "_publish_once", publish_once)
        stale()
    elif gate == "reuse":
        assert run(root, loader)["failed"] == 0
        stale()
    if gate in {"reuse", "resume"}:
        with pytest.raises(ValueError):
            run(root, lambda *_a: pytest.fail("changed authority must fail before provider"))
    else:
        assert run(root, loader)["failed"] >= 1
        assert not (root / f"preflight/{identity}/measurement.json").exists()
