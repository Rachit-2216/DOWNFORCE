"""Offline, reviewed Austrian FP1 finality; never a status-chronology repair.

This exception is identity- and shape-scoped. Publication is separate from both
the original completion and its strict derived interpretation. Every load replays
the captured evidence; no provider HTTP client is constructed here.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from downforce_core.detailed.evidence import (
    MAX_BODY_BYTES,
    _bounded_json_depth,
    _owned_path,
    _unique_object,
    load_derived_completion,
    parse_status_stream,
    sha256,
    status_url,
)

SESSION_ID = "2020-01-practice-1"
SCHEMA = "independent-completion-finalization-v1"
FIA_URL = (
    "https://www.fia.com/events/fia-formula-one-world-championship/"
    "season-2020/austrian-grand-prix/session-classifications"
)
BASIS = "independent-official-classification-and-archive"
EXPECTED_STATUSES = ["Inactive", "Started"] + ["Finished", "Inactive", "Started"] * 4
NONCONTENT_TAGS = {"head", "nav", "aside", "footer", "menu", "template", "script", "style"}


def _aware(value: Any, now: datetime) -> datetime:
    if type(value) is not str:
        raise ValueError("missing evidence timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed > now:
        raise ValueError("evidence timestamp is naive or future")
    return parsed


def _bytes(root: Path, relative: str, limit: int = MAX_BODY_BYTES) -> bytes:
    path = _owned_path(root, relative)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("missing or oversized evidence file")
    return path.read_bytes()


def _reject_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _parse_json(raw: bytes) -> Any:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError as exc:
        raise ValueError("malformed JSON nesting exceeds parser bound") from exc
    _bounded_json_depth(value)
    return value


def _json(root: Path, relative: str) -> tuple[dict[str, Any], bytes]:
    raw = _bytes(root, relative)
    value = _parse_json(raw)
    if type(value) is not dict:
        raise ValueError("evidence document must be an object")
    return value, raw


def _millis(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})", value)
    if not match or int(match[1]) >= 24 or any(int(match[n]) >= 60 for n in (2, 3)):
        raise ValueError("invalid wire timestamp")
    return ((int(match[1]) * 60 + int(match[2])) * 60 + int(match[3])) * 1000 + int(match[4])


def _reviewed_stream(wire: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
    body = bytes.fromhex(wire["body_hex"])
    if wire.get("http_status") != 200 or sha256(body) != wire.get("body_sha256"):
        raise ValueError("independent finality wire checksum or HTTP status differs")
    parsed = parse_status_stream(body)
    records = parsed["records"]
    if (
        [r["status"] for r in records] != EXPECTED_STATUSES
        or any(r["value"] != {"Status": r["status"]} for r in records)
        or parsed["issues"] != [{"record": n, "code": "timestamp-conflict"} for n in range(4, 15)]
        or parsed["notes"]
        or parsed["valid"]
    ):
        raise ValueError("status ambiguity is outside the reviewed coincident boundary shape")
    millis = [_millis(r["time"]) for r in records]
    if not millis[0] < millis[1] < millis[2] or any(n != millis[2] for n in millis[2:]):
        raise ValueError("reviewed stream requires a positive interval and no later events")
    return {k: v for k, v in parsed.items() if k != "source_bytes"}, millis


class _ClassificationHTML(HTMLParser):
    """Extract actual table cells, not metadata or navigation descriptions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonicals: list[str] = []
        self.tables: list[list[list[str]]] = []
        self.events: list[str] = []
        self.seasons: list[str] = []
        self.headings: list[tuple[str, str, int, list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").split():
            self.canonicals.append(attributes.get("href") or "")
        if tag in NONCONTENT_TAGS:
            self.ignored.append(tag)
            return
        if self.ignored:
            return
        self.headings = [
            (kind, heading_tag, depth + int(tag == heading_tag), parts)
            for kind, heading_tag, depth, parts in self.headings
        ]
        if tag == "h1":
            self.headings.append(("event", tag, 1, []))
        elif tag == "h2" or "season-title" in (attributes.get("class") or "").split():
            self.headings.append(("season", tag, 1, []))
        if tag == "table":
            if self.table is not None:
                raise ValueError("nested classification table")
            self.table = []
        elif tag == "tr" and self.table is not None:
            if self.row is not None:
                raise ValueError("unclosed classification row")
            self.row = []
        elif tag in {"td", "th"} and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ValueError("malformed classification cell")
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in NONCONTENT_TAGS:
            if not self.ignored or self.ignored[-1] != tag:
                raise ValueError("malformed non-content classification region")
            self.ignored.pop()
            return
        if self.ignored:
            return
        remaining = []
        for kind, heading_tag, depth, parts in self.headings:
            depth -= int(tag == heading_tag)
            if depth:
                remaining.append((kind, heading_tag, depth, parts))
            else:
                text = " ".join("".join(parts).split())
                if kind == "event":
                    self.events.append(text)
                elif heading_tag != "h2" or text.startswith("SEASON"):
                    self.seasons.append(text)
        self.headings = remaining
        if tag in {"td", "th"} and self.table is not None:
            if self.row is None or self.cell is None:
                raise ValueError("unmatched classification cell")
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ValueError("malformed classification row")
            self.table.append(self.row)
            self.row = None
        elif tag == "table":
            if self.table is None or self.row is not None or self.cell is not None:
                raise ValueError("malformed classification table")
            self.tables.append(self.table)
            self.table = None

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            for _, _, _, parts in self.headings:
                parts.append(data)
            if self.cell is not None:
                self.cell.append(data)


def _classification(body: bytes) -> dict[str, Any]:
    parser = _ClassificationHTML()
    parser.feed(body.decode("utf-8-sig"))
    parser.close()
    if parser.ignored or parser.table is not None or parser.canonicals != [FIA_URL]:
        raise ValueError("unclosed classification table or canonical URL differs")
    if (
        parser.headings
        or parser.events != ["Austrian Grand Prix"]
        or parser.seasons != ["SEASON 2020"]
    ):
        raise ValueError("classification event or season differs")
    title = "PRACTICE 1 CLASSIFICATION 2020 - OFFICIAL"
    matching = [table for table in parser.tables if table and table[0] == [title]]
    if len(matching) != 1:
        raise ValueError("requires exactly one official practice 1 classification table")
    table = matching[0]
    if len(table) != 22 or table[1] != ["POS", "DRIVER", "TEAM", "LAPS", "KM/H", "TIME"]:
        raise ValueError("classification rows or required columns differ")
    drivers: set[str] = set()
    rows = []
    for rank, cells in enumerate(table[2:], 1):
        if len(cells) != 6:
            raise ValueError("classification column count differs")
        position, driver, team, laps, speed, time = cells
        time_match = re.fullmatch(r"(\d+):(\d{2})\.(\d{3})", time)
        if (
            position != str(rank)
            or not driver
            or driver.casefold() in drivers
            or not team
            or not re.fullmatch(r"[1-9]\d*", laps)
            or not re.fullmatch(r"[1-9]\d*\.\d+", speed)
            or not time_match
            or int(time_match[2]) >= 60
            or int(time_match[1]) * 60_000 + int(time_match[2]) * 1000 + int(time_match[3]) <= 0
        ):
            raise ValueError("classification rank, driver, activity or time is incoherent")
        drivers.add(driver.casefold())
        rows.append(
            dict(zip(["position", "driver", "team", "laps", "km_h", "time"], cells, strict=True))
        )
    return {
        "title": title,
        "event_name": "Austrian Grand Prix",
        "season": 2020,
        "classified_drivers": len(rows),
        "rows": rows,
    }


def _archive(
    root: Path, entry: dict[str, Any], source: dict[str, Any], source_digest: str, now: datetime
) -> tuple[dict[str, Any], dict[str, str]]:
    relative = f"archive-finalization/{SESSION_ID}.json"
    archive, raw = _json(root, relative)
    expected = status_url(entry, source).replace("SessionStatus.jsonStream", "ArchiveStatus.json")
    captured_relative = archive["capture_path"]
    captured, capture_bytes = _json(root, captured_relative)
    if sha256(capture_bytes) != archive.get("capture_sha256"):
        raise ValueError("referenced archive capture checksum differs")
    if (
        archive.get("schema_version") != "captured-archive-finalization-v1"
        or captured.get("schema_version") != "official-response-evidence-v1"
    ):
        raise ValueError("archive capture schema differs")
    for record in (archive, captured):
        if (
            record.get("session_id") != SESSION_ID
            or record.get("source_completion_sha256") != source_digest
            or record.get("http_status") != 200
        ):
            raise ValueError("archive source identity or HTTP status differs")
        _aware(record["observed_at"], now)
        body = bytes.fromhex(record["body_hex"])
        if len(body) > MAX_BODY_BYTES or sha256(body) != record.get("body_sha256"):
            raise ValueError("archive body checksum differs")
        if _parse_json(body) != {"Status": "Complete"}:
            raise ValueError("archive is not exactly Complete")
    if (
        archive.get("url") != expected
        or any(
            captured.get(key) != expected
            for key in ("request_url", "actual_url", "prepared_request_url")
        )
        or captured.get("resource") != "ArchiveStatus.json"
        or captured.get("source_completion_path") != f"completion/{SESSION_ID}.json"
        or captured.get("body_available") is not True
        or captured.get("body_bytes") != len(bytes.fromhex(captured["body_hex"]))
        or any(
            archive[key] != captured[key]
            for key in ("body_hex", "body_sha256", "observed_at", "http_status")
        )
    ):
        raise ValueError("archive finalization differs from exact captured response")
    return archive, {relative: sha256(raw), captured_relative: sha256(capture_bytes)}


def build_independent_finalization(
    root: Path, entry: dict[str, Any], capture_relative: str
) -> dict[str, Any]:
    """Build finality from complete offline replay, without publishing or repairing."""
    try:
        return _build(root, entry, capture_relative)
    except (OSError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("missing or malformed independent-finality evidence") from exc


def _build(root: Path, entry: dict[str, Any], capture_relative: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    expected = {
        "session_id": SESSION_ID,
        "event_id": "2020-01",
        "season": 2020,
        "round_number": 1,
        "event_name": "Austrian Grand Prix",
        "session_type": "practice-1",
        "provider_session_name": "Practice 1",
        "fastf1_slot": 1,
        "fastf1_supported": True,
        "completion": "past-unverified",
    }
    if any(
        entry.get(key) != value or type(entry.get(key)) is not type(value)
        for key, value in expected.items()
    ):
        raise ValueError("independent finality is outside the reviewed session identity")
    scheduled = _aware(entry["scheduled_at"], now)
    discovery, discovery_bytes = _json(root, "inventory.json")
    if discovery.get("complete_inventory") is not True:
        raise ValueError("independent finality requires complete discovery")
    entries = discovery["sessions"]
    if len({row["session_id"] for row in entries}) != len(entries) or [
        row for row in entries if row.get("session_id") == SESSION_ID
    ] != [entry]:
        raise ValueError("independent finality entry differs from discovery")
    source_relative = f"completion/{SESSION_ID}.json"
    source, source_bytes = _json(root, source_relative)
    source_digest = sha256(source_bytes)
    derived_relative = f"completion-derived/{SESSION_ID}.json"
    _, derived_bytes = _json(root, derived_relative)
    derived = load_derived_completion(root, entry)
    if (
        source.get("state") != "unverified"
        or derived.get("state") != "unverified"
        or (
            derived.get("completion_integrity") != "ambiguous"
            or derived.get("legacy_comparison") != "exact"
        )
    ):
        raise ValueError("strict completion is not the reviewed unverified ambiguity")
    source_observed = _aware(source["observed_at"], now)
    wire_created = _aware(derived["wire"]["created_at"], now)
    parsed, millis = _reviewed_stream(derived["wire"])
    if parsed != derived["parser"]:
        raise ValueError("strict parser differs from current wire")
    capture, capture_bytes = _json(root, capture_relative)
    if (
        capture.get("schema_version") != "direct-official-classification-capture-v1"
        or capture.get("session_id") != SESSION_ID
        or any(capture.get(key) != FIA_URL for key in ("request_url", "actual_url"))
        or any(
            type(capture.get(key)) is not int or capture[key] != value
            for key, value in (("http_status", 200), ("redirects", 0), ("curl_exit", 0))
        )
        or capture.get("completion_promotion") is not False
        or capture.get("source_completion_sha256") != source_digest
        or capture.get("discovery_sha256") != sha256(discovery_bytes)
    ):
        raise ValueError("classification capture URL, transport, identity or source digest differs")
    started = _aware(capture["started_at"], now)
    observed = _aware(capture["observed_at"], now)
    if not scheduled < started <= observed:
        raise ValueError("classification capture timestamps are not ordered after session")
    parent = Path(capture_relative).parent
    body_relative = (parent / capture["body_file"]).as_posix()
    headers_relative = (parent / capture["headers_file"]).as_posix()
    body = _bytes(root, body_relative)
    headers = _bytes(root, headers_relative, 64 * 1024)
    if (
        capture.get("body_bytes") != len(body)
        or capture.get("body_sha256") != sha256(body)
        or capture.get("headers_sha256") != sha256(headers)
    ):
        raise ValueError("classification body or headers checksum differs")
    header_text = headers.decode("utf-8")
    header_block, separator, trailing = header_text.partition("\r\n\r\n")
    header_lines = header_block.split("\r\n")
    content_types = [
        line.partition(":")[2].strip()
        for line in header_lines[1:]
        if line.partition(":")[0].lower() == "content-type"
    ]
    if (
        not separator
        or trailing
        or not re.fullmatch(r"HTTP/(?:1\.[01]|2|3) 200(?:[ \t]+[^\r\n]*)?", header_lines[0])
        or any(
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[^\r\n]*", line)
            for line in header_lines[1:]
        )
        or len(content_types) != 1
        or not re.fullmatch(r"text/html(?:;[^\r\n]*)?", content_types[0], re.IGNORECASE)
    ):
        raise ValueError("classification requires a single HTTP 200 HTML header block")
    classification = _classification(body)
    archive, archive_digests = _archive(root, entry, source, source_digest, now)
    archive_observed = _aware(archive["observed_at"], now)
    if not scheduled < wire_created <= source_observed <= archive_observed <= started <= observed:
        raise ValueError("independent evidence timestamps are not ordered after session")
    dependencies = {
        "inventory.json": sha256(discovery_bytes),
        source_relative: source_digest,
        derived_relative: sha256(derived_bytes),
        capture_relative: sha256(capture_bytes),
        body_relative: sha256(body),
        headers_relative: sha256(headers),
        **archive_digests,
    }
    return {
        "schema_version": SCHEMA,
        "session_id": SESSION_ID,
        "state": "completed",
        "basis": BASIS,
        "completion_integrity": "ambiguous",
        "chronology_valid": False,
        "source_classification": "unverified",
        "capture_path": capture_relative,
        "discovery_sha256": sha256(discovery_bytes),
        "source_completion_sha256": source_digest,
        "dependencies": dependencies,
        "wire": derived["wire"],
        "parser": parsed,
        "statuses": derived["statuses"],
        "times": derived["times"],
        "time_nanoseconds": [value * 1_000_000 for value in millis],
        "elapsed_before_boundary_seconds": (millis[2] - millis[1]) / 1000,
        "classification": classification,
        "archive": archive,
    }


def load_independent_finalizations(
    root: Path, entries: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Load separately published proofs only after deterministic full replay."""
    rows = {entry["session_id"]: entry for entry in entries}
    if len(rows) != len(entries):
        raise ValueError("duplicate independent-finality inventory identities")
    proofs: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for path in sorted(_owned_path(root, "completion-finalization").glob("*.json")):
        identity = path.stem
        if identity != SESSION_ID or identity not in rows:
            raise ValueError("unknown independent-finality session identity")
        proof, proof_bytes = _json(root, f"completion-finalization/{identity}.json")
        capture_relative = proof.get("capture_path")
        if type(capture_relative) is not str:
            raise ValueError("missing independent-finality capture path")
        current = build_independent_finalization(root, rows[identity], capture_relative)
        # Replayed JSON fields must match types, not Python's False/0 or int/float equality.
        if json.dumps(current, sort_keys=True, allow_nan=False) != json.dumps(
            proof, sort_keys=True, allow_nan=False
        ):
            raise ValueError("independent finalization is stale or differs from current evidence")
        proofs[identity] = current
        hashes[identity] = sha256(proof_bytes)
    return proofs, hashes


def independent_native_observations(
    frame: Any, proof: dict[str, Any], *, captured: bool = False
) -> dict[str, Any]:
    """Exact native parity with a replayed finality proof; chronology stays ambiguous.

    Callers must obtain the proof from build/load above. This function validates
    its wire and ordered native parity; it cannot replay filesystem dependencies
    because its deliberately locked interface has no evidence root.
    """
    import pandas as pd  # type: ignore[import-untyped]

    if (
        proof.get("schema_version") != SCHEMA
        or proof.get("session_id") != SESSION_ID
        or proof.get("state") != "completed"
        or proof.get("basis") != BASIS
        or proof.get("completion_integrity") != "ambiguous"
        or proof.get("chronology_valid") is not False
    ):
        raise ValueError("native observations require the reviewed independent finality proof")
    parsed, millis = _reviewed_stream(proof["wire"])
    nanos = [value * 1_000_000 for value in millis]
    times = [str(timedelta(milliseconds=value)) for value in millis]
    if (
        proof.get("parser") != parsed
        or proof.get("statuses") != EXPECTED_STATUSES
        or proof.get("times") != times
        or proof.get("time_nanoseconds") != nanos
    ):
        raise ValueError("independent native proof differs from its bound wire")
    if not {"Time", "Status"}.issubset(frame.columns):
        raise ValueError("native completion timestamp or Status column is missing")
    if captured and (
        "source_index" not in frame.columns
        or frame["source_index"].tolist() != list(range(len(frame)))
    ):
        raise ValueError("native completion source record order differs")
    values = frame["Time"].tolist()
    if (
        frame["Status"].tolist() != EXPECTED_STATUSES
        or any(
            not isinstance(value, pd.Timedelta) or pd.isna(value) or value.value < 0
            for value in values
        )
        or [value.value for value in values] != nanos
    ):
        raise ValueError("native completion ordered status or nanosecond parity differs")
    return {
        "statuses": frame["Status"].tolist(),
        "times": times,
        "time_nanoseconds": nanos,
        "completion_integrity": "ambiguous",
        "chronology_valid": False,
        "basis": BASIS,
    }
