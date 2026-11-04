"""Synthetic offline evidence for the reviewed Austrian FP1 finality exception."""

import importlib
import json
import pickle
import sqlite3

import pandas as pd
import pytest
from downforce_core.detailed import evidence

IDENTITY = "2020-01-practice-1"
FIA_URL = (
    "https://www.fia.com/events/fia-formula-one-world-championship/"
    "season-2020/austrian-grand-prix/session-classifications"
)
CAPTURE = "evidence-requests/fia/capture.json"


def module():
    return importlib.import_module("downforce_core.detailed.independent_finalization")


def save(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read(root, relative):
    return json.loads((root / relative).read_bytes())


def html():
    rows = "".join(
        f"<tr><td>{n}</td><td>Driver {n}</td><td>Team</td>"
        f"<td>20</td><td>240.000</td><td>1:05.{n:03d}</td></tr>"
        for n in range(1, 21)
    )
    return (
        f'<html><head><link rel="canonical" href="{FIA_URL}"></head><body>'
        "<h1>Austrian Grand Prix</h1><h2>SEASON 2020</h2><table>"
        "<thead><tr><th>PRACTICE 1 CLASSIFICATION 2020 - OFFICIAL</th></tr></thead>"
        "<tbody><tr><td>POS</td><td>DRIVER</td><td>TEAM</td><td>LAPS</td>"
        f"<td>KM/H</td><td>TIME</td></tr>{rows}</tbody></table></body></html>"
    ).encode()


def replace_html(root, body):
    (root / "evidence-requests/fia/response.html").write_bytes(body)
    capture = read(root, CAPTURE)
    capture.update(body_bytes=len(body), body_sha256=evidence.sha256(body))
    save(root, CAPTURE, capture)


def refresh_stream(root, entry, body):
    parsed = evidence.parse_status_stream(body)
    source = read(root, f"completion/{IDENTITY}.json")
    source["statuses"] = [r["status"] for r in parsed["records"]]
    source["times"] = [
        evidence._time_string(r["time"]) if r["time"] else None for r in parsed["records"]
    ]
    save(root, f"completion/{IDENTITY}.json", source)
    path = root / "reconciliation-cache/fastf1_http_cache.sqlite"
    response = {
        "url": evidence.status_url(entry, source),
        "_content": body,
        "status_code": 200,
        "request": {"url": evidence.status_url(entry, source), "method": "GET"},
        "created_at": "2025-01-01T00:00:00+00:00",
        "headers": {},
    }
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value BLOB)")
        db.execute("DELETE FROM responses")
        db.execute("INSERT INTO responses VALUES (?, ?)", ("fixture", pickle.dumps(response)))
    source_bytes = (root / f"completion/{IDENTITY}.json").read_bytes()
    wire = evidence.completion_wire(root, entry, source)
    save(
        root,
        f"completion-derived/{IDENTITY}.json",
        evidence.derive_completion(entry, source_bytes, wire),
    )
    digest = evidence.sha256(source_bytes)
    for relative in (
        CAPTURE,
        f"archive-finalization/{IDENTITY}.json",
        "evidence-requests/archive/response.json",
    ):
        if (root / relative).exists():
            value = read(root, relative)
            value["source_completion_sha256"] = digest
            save(root, relative, value)
    if (root / f"archive-finalization/{IDENTITY}.json").exists():
        archive = read(root, f"archive-finalization/{IDENTITY}.json")
        archive["capture_sha256"] = evidence.sha256((root / archive["capture_path"]).read_bytes())
        save(root, f"archive-finalization/{IDENTITY}.json", archive)


@pytest.fixture
def bundle(tmp_path):
    entry = {
        "session_id": IDENTITY,
        "event_id": "2020-01",
        "season": 2020,
        "round_number": 1,
        "event_name": "Austrian Grand Prix",
        "session_type": "practice-1",
        "provider_session_name": "Practice 1",
        "fastf1_slot": 1,
        "scheduled_at": "2020-07-03T09:00:00+00:00",
        "completion": "past-unverified",
        "fastf1_supported": True,
    }
    save(tmp_path, "inventory.json", {"complete_inventory": True, "sessions": [entry]})
    save(
        tmp_path,
        f"completion/{IDENTITY}.json",
        {
            "session_id": IDENTITY,
            "state": "unverified",
            "statuses": [],
            "times": [],
            "timing_path": "2020/2020-07-05_Austrian_Grand_Prix/2020-07-03_Practice_1/",
            "observed_at": "2025-01-01T00:00:00+00:00",
        },
    )
    (tmp_path / "reconciliation-cache").mkdir()
    statuses = ["Inactive", "Started"] + ["Finished", "Inactive", "Started"] * 4
    times = ["00:00:07.144", "00:09:47.629"] + ["01:39:47.645"] * 12
    body = (
        b"\xef\xbb\xbf"
        + b"\r\n".join(
            (time + json.dumps({"Status": status}, separators=(",", ":"))).encode()
            for time, status in zip(times, statuses, strict=True)
        )
        + b"\r\n"
    )
    refresh_stream(tmp_path, entry, body)
    source_hash = evidence.sha256((tmp_path / f"completion/{IDENTITY}.json").read_bytes())
    archive_url = evidence.status_url(entry, read(tmp_path, f"completion/{IDENTITY}.json")).replace(
        "SessionStatus.jsonStream", "ArchiveStatus.json"
    )
    archive_body = b'\xef\xbb\xbf{"Status":"Complete"}'
    archive = {
        "session_id": IDENTITY,
        "url": archive_url,
        "http_status": 200,
        "body_hex": archive_body.hex(),
        "body_sha256": evidence.sha256(archive_body),
        "observed_at": "2025-01-02T00:00:00+00:00",
        "source_completion_sha256": source_hash,
    }
    raw = {
        **archive,
        "schema_version": "official-response-evidence-v1",
        "source_completion_path": f"completion/{IDENTITY}.json",
        "resource": "ArchiveStatus.json",
        "request_url": archive_url,
        "actual_url": archive_url,
        "prepared_request_url": archive_url,
        "body_available": True,
        "body_bytes": len(archive_body),
        "headers": {},
    }
    save(tmp_path, "evidence-requests/archive/response.json", raw)
    save(
        tmp_path,
        f"archive-finalization/{IDENTITY}.json",
        {
            **archive,
            "schema_version": "captured-archive-finalization-v1",
            "capture_path": "evidence-requests/archive/response.json",
            "capture_sha256": evidence.sha256(
                (tmp_path / "evidence-requests/archive/response.json").read_bytes()
            ),
        },
    )
    body = html()
    headers = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    save(
        tmp_path,
        CAPTURE,
        {
            "schema_version": "direct-official-classification-capture-v1",
            "session_id": IDENTITY,
            "request_url": FIA_URL,
            "actual_url": FIA_URL,
            "http_status": 200,
            "redirects": 0,
            "curl_exit": 0,
            "started_at": "2025-01-03T00:00:00+00:00",
            "observed_at": "2025-01-03T00:00:01Z",
            "body_file": "response.html",
            "body_bytes": len(body),
            "body_sha256": evidence.sha256(body),
            "headers_file": "headers.txt",
            "headers_sha256": evidence.sha256(headers),
            "source_completion_sha256": source_hash,
            "discovery_sha256": evidence.sha256((tmp_path / "inventory.json").read_bytes()),
            "completion_promotion": False,
        },
    )
    (tmp_path / "evidence-requests/fia/response.html").write_bytes(body)
    (tmp_path / "evidence-requests/fia/headers.txt").write_bytes(headers)
    return tmp_path, entry


def test_independent_finality_is_deterministic_and_never_repairs_originals(bundle):
    root, entry = bundle
    originals = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    assert proof == module().build_independent_finalization(root, entry, CAPTURE)
    assert proof["state"] == "completed"
    assert proof["completion_integrity"] == "ambiguous"
    assert proof["chronology_valid"] is False
    assert proof["basis"] == "independent-official-classification-and-archive"
    assert proof["parser"]["valid"] is False
    assert proof["elapsed_before_boundary_seconds"] == 5400.016
    assert proof["classification"]["classified_drivers"] == 20
    assert all(path.read_bytes() == body for path, body in originals.items())
    save(root, f"completion-finalization/{IDENTITY}.json", proof)
    proofs, hashes = module().load_independent_finalizations(root, [entry])
    assert proofs == {IDENTITY: proof}
    assert hashes[IDENTITY] == evidence.sha256(
        (root / f"completion-finalization/{IDENTITY}.json").read_bytes()
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("session_id", "2020-01-practice-2"),
        ("season", 2021),
        ("round_number", 2),
        ("event_name", "Styrian Grand Prix"),
        ("session_type", "practice-2"),
        ("completion", "future"),
        ("completion", "cancelled"),
        ("fastf1_supported", False),
        ("scheduled_at", "2020-07-03T09:00:00"),
        ("scheduled_at", "2999-01-01T00:00:00Z"),
    ],
)
def test_wrong_or_ineligible_identity_is_rejected(bundle, key, value):
    root, entry = bundle
    entry = {**entry, key: value}
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "key,value",
    [
        ("request_url", FIA_URL + "?x=1"),
        ("actual_url", FIA_URL.replace("https:", "http:")),
        ("http_status", 403),
        ("redirects", 1),
        ("curl_exit", 1),
        ("body_sha256", "0" * 64),
        ("headers_sha256", "0" * 64),
        ("body_bytes", 1),
        ("discovery_sha256", "0" * 64),
        ("source_completion_sha256", "0" * 64),
        ("session_id", "2021-01-practice-1"),
        ("started_at", "2025-01-03T00:00:00"),
        ("observed_at", "2999-01-01T00:00:00Z"),
        ("observed_at", "2025-01-02T00:00:00Z"),
        ("body_file", "../../outside.html"),
    ],
)
def test_bad_classification_capture_is_rejected(bundle, key, value):
    root, entry = bundle
    capture = read(root, CAPTURE)
    capture[key] = value
    save(root, CAPTURE, capture)
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


def test_conflicting_response_content_types_are_rejected(bundle):
    root, entry = bundle
    path = root / "evidence-requests/fia/headers.txt"
    headers = path.read_bytes().replace(b"\r\n\r\n", b"\r\nContent-Type: application/json\r\n\r\n")
    path.write_bytes(headers)
    capture = read(root, CAPTURE)
    capture["headers_sha256"] = evidence.sha256(headers)
    save(root, CAPTURE, capture)
    with pytest.raises(ValueError, match="header block"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "headers",
    [
        b"HTTP/1.1 200 OK\r\n\r\nContent-Type: text/html\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\ntrailing text",
    ],
)
def test_content_type_must_be_inside_terminated_header_block(bundle, headers):
    root, entry = bundle
    (root / "evidence-requests/fia/headers.txt").write_bytes(headers)
    capture = read(root, CAPTURE)
    capture["headers_sha256"] = evidence.sha256(headers)
    save(root, CAPTURE, capture)
    with pytest.raises(ValueError, match="header block"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "old,new",
    [
        (b"PRACTICE 1 CLASSIFICATION", b"PRACTICE 2 CLASSIFICATION"),
        (b"2020 - OFFICIAL", b"2020 - PROVISIONAL"),
        (b"SEASON 2020", b"SEASON 2021"),
        (b"Austrian Grand Prix", b"Styrian Grand Prix"),
        (b">20</td><td>240", b">0</td><td>240"),
        (b"Driver 20", b"Driver 19"),
        (b">20</td><td>Driver 20", b">19</td><td>Driver 20"),
        (b"1:05.001", b"1:99.001"),
        (b">TIME<", b">GAP<"),
        (FIA_URL.encode(), b"https://www.fia.com/wrong"),
        (b"</table>", b""),
    ],
)
def test_wrong_or_malformed_table_is_rejected(bundle, old, new):
    root, entry = bundle
    replace_html(root, html().replace(old, new))
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Austrian Grand Prix SEASON 2020 PRACTICE 1 CLASSIFICATION 2020 - OFFICIAL</html>",
        html().replace(b"</body>", html() + b"</body>"),
    ],
)
def test_navigation_or_duplicate_classification_is_rejected(bundle, body):
    root, entry = bundle
    replace_html(root, body)
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "old,new",
    [
        (b"<h1>Austrian Grand Prix</h1>", b"<h1>Styrian Grand Prix</h1>"),
        (b"<h2>SEASON 2020</h2>", b"<h2>SEASON 2021</h2>"),
    ],
)
def test_navigation_cannot_supply_actual_event_or_season(bundle, old, new):
    root, entry = bundle
    body = (
        html()
        .replace(old, new)
        .replace(b"</body>", b"<nav>Austrian Grand Prix SEASON 2020</nav></body>")
    )
    replace_html(root, body)
    with pytest.raises(ValueError, match="event or season"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize("region", ["nav", "head", "aside", "footer", "menu", "template"])
def test_noncontent_region_headings_cannot_supply_page_identity(bundle, region):
    root, entry = bundle
    headings = b"<h1>Austrian Grand Prix</h1><h2>SEASON 2020</h2>"
    noncontent = b"<" + region.encode() + b">" + headings + b"</" + region.encode() + b">"
    body = html().replace(
        headings, noncontent + b"<main><h3>Styrian Grand Prix SEASON 2021</h3></main>"
    )
    replace_html(root, body)
    with pytest.raises(ValueError, match="event or season"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize("depth", [60, 20_000])
@pytest.mark.parametrize("target", ["inventory.json", CAPTURE, "published"])
def test_json_nesting_bound_is_public_value_error(bundle, depth, target):
    root, entry = bundle
    if target == "published":
        target = f"completion-finalization/{IDENTITY}.json"
        save(root, target, module().build_independent_finalization(root, entry, CAPTURE))
        load = True
    else:
        load = False
    path = root / target
    body = path.read_bytes().rstrip()
    path.write_bytes(body[:-1] + b',"extra":' + b"[" * depth + b"0" + b"]" * depth + b"}")
    if target == "inventory.json":
        capture = read(root, CAPTURE)
        capture["discovery_sha256"] = evidence.sha256(path.read_bytes())
        save(root, CAPTURE, capture)
    with pytest.raises(ValueError, match="nesting|malformed"):
        if load:
            module().load_independent_finalizations(root, [entry])
        else:
            module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonfinite_json_constants_are_rejected(bundle, constant):
    root, entry = bundle
    path = root / CAPTURE
    body = path.read_bytes().rstrip()
    path.write_bytes(body[:-1] + b',"extra":' + constant + b"}")
    with pytest.raises(ValueError, match="constant"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize("token", [b"1e400", b"-1e400"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("load", [False, True])
def test_overflowing_json_numbers_are_rejected_by_public_build_and_load(
    bundle, token, nested, load
):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    path = root / CAPTURE
    body = path.read_bytes().rstrip()
    value = b'{"values":[' + token + b"]}" if nested else token
    path.write_bytes(body[:-1] + b',"extra":' + value + b"}")
    if load:
        # Bind the changed capture exactly, so stale dependencies cannot mask
        # acceptance of overflowing numbers by the public proof loader.
        proof["dependencies"][CAPTURE] = evidence.sha256(path.read_bytes())
        save(root, f"completion-finalization/{IDENTITY}.json", proof)
    with pytest.raises(ValueError, match="nonfinite"):
        if load:
            module().load_independent_finalizations(root, [entry])
        else:
            module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize("token", [b"1e308", b"-1e308", b"1e-400"])
def test_finite_exponent_numbers_remain_eligible(bundle, token):
    root, entry = bundle
    path = root / CAPTURE
    body = path.read_bytes().rstrip()
    path.write_bytes(body[:-1] + b',"extra":{"values":[' + token + b"]}}")
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    assert proof["state"] == "completed"
    save(root, f"completion-finalization/{IDENTITY}.json", proof)
    proofs, _ = module().load_independent_finalizations(root, [entry])
    assert proofs == {IDENTITY: proof}


def test_archive_observation_before_session_is_rejected(bundle):
    root, entry = bundle
    archive_relative = f"archive-finalization/{IDENTITY}.json"
    archive = read(root, archive_relative)
    capture = read(root, archive["capture_path"])
    archive["observed_at"] = capture["observed_at"] = "2019-01-01T00:00:00Z"
    save(root, archive["capture_path"], capture)
    archive["capture_sha256"] = evidence.sha256((root / archive["capture_path"]).read_bytes())
    save(root, archive_relative, archive)
    with pytest.raises(ValueError, match="timestamp"):
        module().build_independent_finalization(root, entry, CAPTURE)


def test_oversized_published_proof_is_rejected_before_loading(bundle):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    relative = f"completion-finalization/{IDENTITY}.json"
    save(root, relative, proof)
    path = root / relative
    path.write_bytes(path.read_bytes() + b" " * evidence.MAX_BODY_BYTES)
    with pytest.raises(ValueError, match="oversized"):
        module().load_independent_finalizations(root, [entry])


@pytest.mark.parametrize(
    "target",
    [
        "inventory.json",
        f"completion/{IDENTITY}.json",
        f"completion-derived/{IDENTITY}.json",
        f"archive-finalization/{IDENTITY}.json",
        "evidence-requests/archive/response.json",
        "evidence-requests/fia/response.html",
        "evidence-requests/fia/headers.txt",
        CAPTURE,
    ],
)
def test_published_proof_replays_every_dependency(bundle, target):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    save(root, f"completion-finalization/{IDENTITY}.json", proof)
    path = root / target
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        module().load_independent_finalizations(root, [entry])


@pytest.mark.parametrize(
    "target",
    [
        CAPTURE,
        "evidence-requests/fia/response.html",
        "evidence-requests/fia/headers.txt",
        "evidence-requests/archive/response.json",
    ],
)
def test_missing_evidence_fails_closed(bundle, target):
    root, entry = bundle
    (root / target).unlink()
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "old,new",
    [
        (b'"Started"', b'"Aborted"'),
        (b'"Finished"', b'"Mystery"'),
        (b"01:39:47.645", b"00:09:47.629"),
        (b'"Status":"Inactive"', b'"Status":"Inactive","Extra":true'),
        (b'"Status":"Inactive"', b'"Status":"Inactive","Status":"Inactive"'),
        (b"01:39:47.645", b"01:99:47.645"),
    ],
)
def test_independent_proof_never_excuses_other_wire_problems(bundle, old, new):
    root, entry = bundle
    derived = read(root, f"completion-derived/{IDENTITY}.json")
    body = bytes.fromhex(derived["wire"]["body_hex"]).replace(old, new)
    refresh_stream(root, entry, body)
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


def test_later_restart_or_different_suffix_is_rejected(bundle):
    root, entry = bundle
    derived = read(root, f"completion-derived/{IDENTITY}.json")
    body = bytes.fromhex(derived["wire"]["body_hex"])
    body += b'01:40:00.000{"Status":"Started"}\r\n'
    refresh_stream(root, entry, body)
    with pytest.raises(ValueError):
        module().build_independent_finalization(root, entry, CAPTURE)


def native(proof):
    return pd.DataFrame(
        {
            "Status": proof["statuses"],
            "Time": pd.to_timedelta(proof["times"]),
            "source_index": range(len(proof["statuses"])),
        }
    )


def test_exact_native_parity_is_separate_from_strict_chronology(bundle):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    frame = native(proof)
    with pytest.raises(ValueError, match="ambiguous"):
        evidence.native_status_observations(frame)
    observed = module().independent_native_observations(frame, proof, captured=True)
    assert observed["statuses"] == proof["statuses"]
    assert observed["times"] == proof["times"]
    assert observed["time_nanoseconds"][2] == 5987645000000
    assert observed["completion_integrity"] == "ambiguous"


@pytest.mark.parametrize(
    "mutation", ["drop", "add", "reorder", "time", "status", "index", "missing", "nat", "string"]
)
def test_native_missing_added_or_perturbed_records_are_rejected(bundle, mutation):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    frame = native(proof)
    if mutation == "drop":
        frame = frame.iloc[:-1]
    elif mutation == "add":
        frame = pd.concat([frame, frame.iloc[-1:]])
    elif mutation == "reorder":
        frame = frame.iloc[::-1]
    elif mutation == "time":
        frame.loc[2, "Time"] += pd.Timedelta(1, unit="ns")
    elif mutation == "status":
        frame.loc[2, "Status"] = "Aborted"
    elif mutation == "index":
        frame.loc[2, "source_index"] = 9
    elif mutation == "missing":
        frame = frame.drop(columns="Time")
    elif mutation == "nat":
        frame.loc[2, "Time"] = pd.NaT
    else:
        frame["Time"] = frame["Time"].astype(str)
    with pytest.raises(ValueError):
        module().independent_native_observations(frame, proof, captured=True)


def test_duplicate_json_and_unknown_published_identity_fail_closed(bundle):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    save(root, "completion-finalization/2020-01-practice-2.json", proof)
    with pytest.raises(ValueError):
        module().load_independent_finalizations(root, [entry])
    (root / CAPTURE).write_text('{"session_id":"x","session_id":"y"}')
    with pytest.raises(ValueError, match="duplicate"):
        module().build_independent_finalization(root, entry, CAPTURE)


@pytest.mark.parametrize(
    "problem",
    ["chronology-int", "chronology-float", "classification-season", "archive-status", "nanos"],
)
def test_published_proof_rejects_type_coercion_against_full_replay(bundle, problem):
    root, entry = bundle
    proof = module().build_independent_finalization(root, entry, CAPTURE)
    if problem == "chronology-int":
        proof["chronology_valid"] = 0
    elif problem == "chronology-float":
        proof["chronology_valid"] = 0.0
    elif problem == "classification-season":
        proof["classification"]["season"] = 2020.0
    elif problem == "archive-status":
        proof["archive"]["http_status"] = 200.0
    else:
        proof["time_nanoseconds"] = [float(value) for value in proof["time_nanoseconds"]]
    assert proof == module().build_independent_finalization(root, entry, CAPTURE)
    save(root, f"completion-finalization/{IDENTITY}.json", proof)
    with pytest.raises(ValueError, match="differs from current evidence"):
        module().load_independent_finalizations(root, [entry])
