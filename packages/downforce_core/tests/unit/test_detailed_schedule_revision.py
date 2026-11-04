"""Explicit captured schedule revisions preserve the frozen discovery authority."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from downforce_core.detailed.evidence import sha256
from test_detailed_temporal_completion import (
    INDEX,
    RUN,
    captured,  # noqa: F401
    digest,
    read,
    ready,
    rebind_index,
    replace_response,
    response,
    temporal,
    write,
)

PROVIDER = "evidence-requests/provider-schedule-test"
PURPOSE = "verify same-key qualifying schedule revision; no completion promotion"


def set_body(value, body):
    value.update(body_hex=body.hex(), body_bytes=len(body), body_sha256=sha256(body))
    value["headers"]["Content-Length"] = str(len(body))
    value["header_fields"][1][1] = str(len(body))


@pytest.fixture
def revised(captured):  # noqa: F811
    root, entries = captured
    fresh = read(root, "openf1-inventory.json")["sessions"]
    for row in fresh:
        for field in ("date_start", "date_end"):
            row[field] = (datetime.fromisoformat(row[field]) + timedelta(minutes=30)).isoformat()

    def shifted(index):
        for row in index["Meetings"][0]["Sessions"]:
            for field in ("StartDate", "EndDate"):
                row[field] = (
                    datetime.fromisoformat(row[field]) + timedelta(minutes=30)
                ).isoformat()

    rebind_index(root, shifted)
    url = "https://api.openf1.org/v1/sessions?year=2026"
    original_hash = digest(root, "openf1-inventory.json")
    write(
        root,
        PROVIDER + "/intent.json",
        {
            "schema_version": "provider-schedule-intent-v1",
            "request_url": url,
            "allow_redirects": False,
            "started_at": "2026-01-03T00:00:01+00:00",
            "original_provider_sha256": original_hash,
            "purpose": PURPOSE,
            "completion_promotion": False,
        },
    )
    write(
        root,
        PROVIDER + "/response.json",
        response(
            url,
            json.dumps(fresh).encode(),
            observed="2026-01-03T00:00:01.500000+00:00",
            schema_version="provider-schedule-response-v1",
            original_provider_sha256=original_hash,
        ),
    )
    write(
        root,
        PROVIDER + "/outcome.json",
        {
            "schema_version": "provider-schedule-outcome-v1",
            "request_url": url,
            "run_path": PROVIDER,
            "finished_at": "2026-01-03T00:00:01.750000+00:00",
            "responses": 1,
            "http_status": 200,
            "error": None,
            "completion_promotion": False,
        },
    )
    intent = read(root, RUN + "/intent.json")
    intent.update(
        schema_version="temporal-session-capture-v2",
        provider_schedule_capture_path=PROVIDER + "/response.json",
        provider_schedule_capture_sha256=digest(root, PROVIDER + "/response.json"),
    )
    for request in intent["planned_requests"]:
        for field in ("scheduled_at", "scheduled_end_at"):
            request[field] = (
                datetime.fromisoformat(request[field]) + timedelta(minutes=30)
            ).isoformat()
    write(root, RUN + "/intent.json", intent)
    for n, request in enumerate(intent["planned_requests"], 1):
        replace_response(
            root,
            n,
            lambda row, request=request: row.update(
                scheduled_at=request["scheduled_at"], scheduled_end_at=request["scheduled_end_at"]
            ),
        )
    return captured


def test_positive_same_delay_uses_two_fresh_sources_without_rewriting_frozen_sources(revised):
    root, entries = revised
    frozen = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    original = read(root, "openf1-inventory.json")["sessions"][0]
    assert proof["original_provider_session"] == original
    assert proof["schedule_revision"] == {
        "original_date_start": original["date_start"],
        "original_date_end": original["date_end"],
        "revised_date_start": proof["provider_session"]["date_start"],
        "revised_date_end": proof["provider_session"]["date_end"],
        "delay_seconds": 1800.0,
        "provider_schedule_capture_path": PROVIDER + "/response.json",
        "provider_schedule_capture_sha256": digest(root, PROVIDER + "/response.json"),
    }
    assert proof["effective_entry"]["scheduled_at"] == entries[0]["scheduled_at"]
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    result, effective = ready(revised)
    assert result["summary"] == {"completed": 2}
    assert effective[entries[0]["session_id"]]["scheduled_at"] == entries[0]["scheduled_at"]
    assert {
        PROVIDER + "/" + name for name in ("intent.json", "response.json", "outcome.json")
    } <= set(proof["dependencies"])


def rebind_provider(root, mutate):
    relative = PROVIDER + "/response.json"
    value = read(root, relative)
    mutate(value)
    write(root, relative, value)
    intent = read(root, RUN + "/intent.json")
    intent["provider_schedule_capture_sha256"] = digest(root, relative)
    write(root, RUN + "/intent.json", intent)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-intent",
        "missing-response",
        "missing-outcome",
        "extra-member",
        "stale-hash",
        "body-hash",
        "body-length",
        "bad-hex",
        "header-alias",
        "header-duplicate",
        "http-alias",
        "redirect",
        "transport",
        "wrong-year-url",
        "source-sha",
        "naive",
        "future",
        "stale",
        "after-batch",
        "outcome-error",
        "outcome-http",
        "outcome-count",
        "outcome-run",
        "outcome-schema",
        "outcome-promotion",
        "intent-source",
        "intent-redirect",
        "intent-purpose",
        "intent-schema",
        "intent-promotion",
        "intent-before-response",
        "response-after-outcome",
        "missing-v2",
        "missing-authority",
        "unknown-schema",
        "metadata-in-response",
        "missing-session-response",
        "original-changed",
        "row-identity",
        "row-alias",
        "extra-key",
        "missing-key",
        "duplicate-key",
        "duplicate-identical-key",
        "nonpositive-key",
        "key-alias",
        "provider-date-conflict",
        "end-only",
        "earlier",
        "zero",
        "date-naive",
        "date-future",
        "nonfinite",
        "non-list",
        "non-dict-row",
        "second-row-corrupt",
        "status-restart",
        "archive-generating",
    ],
)
def test_v2_rejects_missing_or_corrupt_authority_even_with_rebound_capture_hash(revised, problem):
    root, entries = revised
    if problem.startswith("missing-") and problem in {
        "missing-intent",
        "missing-response",
        "missing-outcome",
        "missing-session-response",
    }:
        target = (
            RUN + "/response-04.json"
            if problem == "missing-session-response"
            else PROVIDER + "/" + problem.removeprefix("missing-") + ".json"
        )
        (root / target).unlink()
    elif problem == "extra-member":
        write(root, PROVIDER + "/unrecorded.json", {})
    elif problem == "original-changed":
        target = root / "openf1-inventory.json"
        target.write_bytes(target.read_bytes() + b" ")
    elif problem.startswith("outcome-"):
        value = read(root, PROVIDER + "/outcome.json")
        key, item = {
            "outcome-error": ("error", "TransportError"),
            "outcome-http": ("http_status", 201),
            "outcome-count": ("responses", True),
            "outcome-run": ("run_path", "other"),
            "outcome-schema": ("schema_version", "unknown"),
            "outcome-promotion": ("completion_promotion", True),
        }[problem]
        value[key] = item
        write(root, PROVIDER + "/outcome.json", value)
    elif problem.startswith("intent-"):
        value = read(root, PROVIDER + "/intent.json")
        key, item = {
            "intent-source": ("original_provider_sha256", "0" * 64),
            "intent-redirect": ("allow_redirects", True),
            "intent-purpose": ("purpose", "waive"),
            "intent-schema": ("schema_version", "unknown"),
            "intent-promotion": ("completion_promotion", True),
            "intent-before-response": ("started_at", "2026-01-03T00:00:01.600000+00:00"),
        }[problem]
        value[key] = item
        write(root, PROVIDER + "/intent.json", value)
    elif problem in {"missing-v2", "missing-authority", "unknown-schema", "stale-hash"}:
        value = read(root, RUN + "/intent.json")
        if problem == "missing-v2":
            value["schema_version"] = "temporal-session-capture-v1"
            value.pop("provider_schedule_capture_path")
            value.pop("provider_schedule_capture_sha256")
        elif problem == "missing-authority":
            value.pop("provider_schedule_capture_sha256")
        elif problem == "unknown-schema":
            value["schema_version"] = "temporal-session-capture-v3"
        else:
            value["provider_schedule_capture_sha256"] = "0" * 64
        write(root, RUN + "/intent.json", value)
    elif problem in {"metadata-in-response", "status-restart", "archive-generating"}:

        def mutate(value):
            if problem == "metadata-in-response":
                value["provider_schedule_capture_sha256"] = "0" * 64
            else:
                body = (
                    bytes.fromhex(value["body_hex"]) + b'00:03:00.000{"Status":"Started"}'
                    if problem == "status-restart"
                    else b'{"Status":"Generating"}'
                )
                set_body(value, body)

        replace_response(root, 3 if problem != "archive-generating" else 4, mutate)
    else:

        def mutate(value):
            fields = {
                "body-hash": ("body_sha256", "0" * 64),
                "body-length": ("body_bytes", 1),
                "bad-hex": ("body_hex", "zz"),
                "http-alias": ("http_status", 200.0),
                "redirect": ("actual_url", "https://example.test/redirect"),
                "transport": ("transport_request_url", "https://example.test/substitute"),
                "wrong-year-url": ("request_url", "https://api.openf1.org/v1/sessions?year=2025"),
                "source-sha": ("original_provider_sha256", "0" * 64),
                "naive": ("observed_at", "2026-01-03T00:00:01.500000"),
                "future": ("observed_at", "2999-01-03T00:00:01+00:00"),
                "stale": ("observed_at", "2026-01-03T00:00:00+00:00"),
                "after-batch": ("observed_at", "2026-01-03T00:00:02+00:00"),
                "response-after-outcome": ("observed_at", "2026-01-03T00:00:01.900000+00:00"),
            }
            if problem in fields:
                key, item = fields[problem]
                value[key] = item
            elif problem == "header-alias":
                value["headers"]["Content-Length"] = 123
            elif problem == "header-duplicate":
                value["header_fields"].append(["content-type", "application/json"])
            else:
                rows = json.loads(bytes.fromhex(value["body_hex"]))
                if problem in {"duplicate-key", "duplicate-identical-key"}:
                    rows.append(dict(rows[0]))
                    if problem == "duplicate-key":
                        rows[-1]["date_end"] = rows[1]["date_end"]
                elif problem == "non-list":
                    rows = {"sessions": rows}
                elif problem == "non-dict-row":
                    rows.append(None)
                elif problem == "second-row-corrupt":
                    rows[1]["country_key"] = 999
                elif problem == "missing-key":
                    rows.pop(0)
                else:
                    key, item = {
                        "row-identity": ("country_key", 999),
                        "row-alias": ("country_key", 36.0),
                        "extra-key": ("extra", True),
                        "nonpositive-key": ("session_key", 0),
                        "key-alias": ("session_key", 101.0),
                        "provider-date-conflict": ("date_start", "2026-01-02T01:40:00+00:00"),
                        "end-only": ("date_start", "2026-01-02T01:00:00+00:00"),
                        "earlier": ("date_start", "2026-01-02T00:30:00+00:00"),
                        "zero": ("date_start", "2026-01-02T01:00:00+00:00"),
                        "date-naive": ("date_start", "2026-01-02T01:30:00"),
                        "date-future": ("date_start", "2999-01-02T01:30:00+00:00"),
                        "nonfinite": ("country_key", float("inf")),
                    }[problem]
                    rows[0][key] = item
                set_body(value, json.dumps(rows).encode())

        rebind_provider(root, mutate)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize(
    "member",
    [
        "inventory.json",
        "openf1-inventory.json",
        PROVIDER + "/response.json",
        INDEX + "/response.json",
        RUN + "/response-04.json",
    ],
)
def test_v2_rechecks_dependency_bytes_for_mutation_during_replay(revised, monkeypatch, member):
    root, entries = revised
    original = temporal()._Replay.document
    changed = False

    def document(self, relative):
        nonlocal changed
        result = original(self, relative)
        if relative == member and not changed:
            changed = True
            path = root / member
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(temporal()._Replay, "document", document)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


def test_v2_published_revision_is_full_typed_replay_and_not_a_waiver(revised):
    root, entries = revised
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    proof["schedule_revision"]["delay_seconds"] = True
    write(root, f"completion-temporal/{entries[0]['session_id']}.json", proof)
    with pytest.raises(ValueError):
        temporal().load_temporal_completions(root, entries)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-02T01:00:00", "2026-01-02T02:00:00"),  # no revision
        ("2026-01-02T00:30:00", "2026-01-02T01:30:00"),  # earlier, equal shift
        ("2026-01-02T01:00:00", "2026-01-02T02:30:00"),  # only end moved
        ("2026-01-02T01:30:00", "2026-01-02T02:45:00"),  # duration changed
        ("2999-01-02T01:30:00", "2999-01-02T02:30:00"),  # agreed but future
    ],
)
def test_even_two_fresh_sources_agreeing_cannot_authorize_non_delay_dates(revised, start, end):
    root, entries = revised

    def mutate_index(index):
        index["Meetings"][0]["Sessions"][0].update(StartDate=start, EndDate=end)

    def mutate_provider(value):
        rows = json.loads(bytes.fromhex(value["body_hex"]))
        rows[0].update(date_start=start + "+00:00", date_end=end + "+00:00")
        set_body(value, json.dumps(rows).encode())

    rebind_index(root, mutate_index)
    rebind_provider(root, mutate_provider)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize("problem", ["old-start", "old-end", "old-duplicate"])
def test_rebound_original_provider_still_requires_exact_frozen_schedule(revised, problem):
    root, entries = revised
    provider = read(root, "openf1-inventory.json")
    if problem == "old-start":
        provider["sessions"][0]["date_start"] = "2026-01-02T01:15:00+00:00"
    elif problem == "old-end":
        provider["sessions"][0]["date_end"] = provider["sessions"][0]["date_start"]
    else:
        provider["sessions"].append(dict(provider["sessions"][0]))
    write(root, "openf1-inventory.json", provider)
    original_hash = digest(root, "openf1-inventory.json")
    intent = read(root, RUN + "/intent.json")
    intent["openf1_inventory_sha256"] = original_hash
    write(root, RUN + "/intent.json", intent)
    intent = read(root, PROVIDER + "/intent.json")
    intent["original_provider_sha256"] = original_hash
    write(root, PROVIDER + "/intent.json", intent)
    rebind_provider(root, lambda value: value.update(original_provider_sha256=original_hash))
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize("directory", [PROVIDER, INDEX, RUN])
def test_v2_rechecks_capture_membership_when_new_file_appears_during_replay(
    revised, monkeypatch, directory
):
    root, entries = revised
    original = temporal()._Replay.document

    def document(self, relative):
        result = original(self, relative)
        if relative == RUN + "/response-04.json":
            write(root, directory + "/unrecorded.json", {})
        return result

    monkeypatch.setattr(temporal()._Replay, "document", document)
    with pytest.raises(ValueError):
        temporal().build_temporal_completion(root, entries[0], RUN)


def test_revision_cannot_rehabilitate_future_observation_after_original_start(revised):
    root, entries = revised
    identity = entries[0]["session_id"]
    relative = f"completion/{identity}.json"
    source = read(root, relative)
    source["observed_at"] = "2026-01-02T01:15:00+00:00"
    write(root, relative, source)
    source_hash = digest(root, relative)
    intent = read(root, RUN + "/intent.json")
    for request in intent["planned_requests"]:
        if request["session_id"] == identity:
            request["source_completion_sha256"] = source_hash
    write(root, RUN + "/intent.json", intent)
    for n in (1, 2):
        replace_response(root, n, lambda row: row.update(source_completion_sha256=source_hash))
    with pytest.raises(ValueError, match="future observation and later capture dates differ"):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize("source", ["revised-provider", "official", "original-provider"])
@pytest.mark.parametrize("fields", [("start", "end"), ("end",)])
def test_v2_never_truncates_submicrosecond_schedule_precision(revised, source, fields):
    root, entries = revised

    def precise(raw):
        return raw.replace("00+00:00", "00.0000009+00:00") if "+00:00" in raw else raw + ".0000009"

    if source == "official":

        def mutate(index):
            session = index["Meetings"][0]["Sessions"][0]
            for field in fields:
                name = "StartDate" if field == "start" else "EndDate"
                session[name] = precise(session[name])

        rebind_index(root, mutate)
    elif source == "revised-provider":

        def mutate(value):
            rows = json.loads(bytes.fromhex(value["body_hex"]))
            for field in fields:
                name = "date_" + field
                rows[0][name] = precise(rows[0][name])
            set_body(value, json.dumps(rows).encode())

        rebind_provider(root, mutate)
    else:
        provider = read(root, "openf1-inventory.json")
        for field in fields:
            name = "date_" + field
            provider["sessions"][0][name] = precise(provider["sessions"][0][name])
        write(root, "openf1-inventory.json", provider)
        original_hash = digest(root, "openf1-inventory.json")
        intent = read(root, RUN + "/intent.json")
        intent["openf1_inventory_sha256"] = original_hash
        write(root, RUN + "/intent.json", intent)
        intent = read(root, PROVIDER + "/intent.json")
        intent["original_provider_sha256"] = original_hash
        write(root, PROVIDER + "/intent.json", intent)
        rebind_provider(root, lambda value: value.update(original_provider_sha256=original_hash))
    with pytest.raises(ValueError, match="precision"):
        temporal().build_temporal_completion(root, entries[0], RUN)


@pytest.mark.parametrize("precision", ["seconds", "milliseconds", "microseconds"])
def test_v2_accepts_representable_precision_and_equivalent_timezone_instants(revised, precision):
    root, entries = revised

    def mutate(value):
        rows = json.loads(bytes.fromhex(value["body_hex"]))
        for row in rows:
            for field in ("date_start", "date_end"):
                row[field] = (
                    datetime.fromisoformat(row[field])
                    .astimezone(timezone(timedelta(hours=5, minutes=30)))
                    .isoformat(timespec=precision)
                )
        set_body(value, json.dumps(rows).encode())

    rebind_provider(root, mutate)
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    assert proof["schedule_revision"]["delay_seconds"] == 1800.0
    assert proof["provider_session"]["date_start"].endswith("+05:30")


def test_v2_rejects_submicrosecond_frozen_entry_even_when_discovery_hashes_are_rebound(revised):
    root, entries = revised
    entries[0]["scheduled_at"] = "2026-01-02T01:00:00.0000009+00:00"
    inventory = read(root, "inventory.json")
    inventory["sessions"] = entries
    write(root, "inventory.json", inventory)
    discovery_hash = digest(root, "inventory.json")
    intent = read(root, INDEX + "/intent.json")
    intent.update(
        discovery_sha256=discovery_hash,
        source_discovery_hex=(root / "inventory.json").read_bytes().hex(),
    )
    write(root, INDEX + "/intent.json", intent)
    response = read(root, INDEX + "/response.json")
    response["discovery_sha256"] = discovery_hash
    write(root, INDEX + "/response.json", response)
    index_hash = digest(root, INDEX + "/response.json")
    intent = read(root, RUN + "/intent.json")
    intent.update(discovery_sha256=discovery_hash, index_capture_sha256=index_hash)
    write(root, RUN + "/intent.json", intent)
    for n in range(1, 5):
        replace_response(
            root,
            n,
            lambda row: row.update(
                discovery_sha256=discovery_hash, index_capture_sha256=index_hash
            ),
        )
    with pytest.raises(ValueError, match="precision"):
        temporal().build_temporal_completion(root, entries[0], RUN)


def test_precision_guard_does_not_change_legacy_v1_iso_parsing(captured):  # noqa: F811
    root, entries = captured
    provider = read(root, "openf1-inventory.json")
    for row in provider["sessions"]:
        for field in ("date_start", "date_end"):
            row[field] = row[field].replace("00+00:00", "00.0000009+00:00")
    write(root, "openf1-inventory.json", provider)
    intent = read(root, RUN + "/intent.json")
    intent["openf1_inventory_sha256"] = digest(root, "openf1-inventory.json")
    write(root, RUN + "/intent.json", intent)
    proof = temporal().build_temporal_completion(root, entries[0], RUN)
    assert proof["state"] == "completed"
    assert "schedule_revision" not in proof
