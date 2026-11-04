"""Append-only, offline authority for sessions frozen as future at discovery.

The original inventory and completion observations are never rewritten. Each
published proof replays its entire captured request batch, then supplies a narrow
effective source view for formal reconciliation and native I/O.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from downforce_core.detailed.catalog import canonical_session_type
from downforce_core.detailed.evidence import (
    MAX_BODY_BYTES,
    TERMINAL_AUX_POLICY,
    _owned_path,
    _time_string,
    _typed_finite_json_equal,
    index_url,
    native_status_observations,
    parse_status_stream,
    sha256,
    status_completion,
    status_url,
)
from downforce_core.detailed.independent_finalization import _aware, _json, _millis, _parse_json
from downforce_core.domain.identifiers import validate_safe_identifier

SCHEMA = "temporal-completion-v1"
PROOF_DIRECTORY = "completion-temporal"
TEMPORAL_MARKERS = ("completion_temporal_sha256",)
RESOURCES = ("SessionStatus.jsonStream", "ArchiveStatus.json")
PLAN_FIELDS = {
    "session_id",
    "request_url",
    "timing_path",
    "timing_key",
    "resource",
    "source_completion_sha256",
    "scheduled_at",
    "scheduled_end_at",
}
RESPONSE_FIELDS = {
    "schema_version",
    "request_url",
    "actual_url",
    "transport_request_url",
    "http_status",
    "headers",
    "header_fields",
    "body_hex",
    "body_bytes",
    "body_sha256",
    "observed_at",
    "discovery_sha256",
    "completion_promotion",
}


def _expect(value: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    if any(
        key not in value or not _typed_finite_json_equal(value[key], item)
        for key, item in expected.items()
    ):
        raise ValueError(f"temporal {label} identity, type or checksum differs")


def _fields(value: Any, fields: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"temporal {label} fields differ")


def _positive_int(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("temporal provider identity must be a positive integer")
    return value


def _body(record: dict[str, Any], url: str, now: datetime) -> tuple[bytes, datetime]:
    _expect(
        record,
        {
            "request_url": url,
            "actual_url": url,
            "transport_request_url": url,
            "http_status": 200,
            "completion_promotion": False,
        },
        "response transport",
    )
    hex_value = record["body_hex"]
    if type(hex_value) is not str or not re.fullmatch(r"(?:[0-9a-f]{2})*", hex_value):
        raise ValueError("temporal body hex is malformed")
    body = bytes.fromhex(hex_value)
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("temporal body exceeds bounded size")
    _expect(record, {"body_bytes": len(body), "body_sha256": sha256(body)}, "body")
    headers = record["headers"]
    fields = record["header_fields"]
    if type(headers) is not dict or type(fields) is not list or not fields:
        raise ValueError("temporal response header evidence is missing")
    reconstructed: dict[str, str] = {}
    names: set[str] = set()
    for pair in fields:
        if (
            type(pair) is not list
            or len(pair) != 2
            or any(type(item) is not str for item in pair)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", pair[0])
            or "\r" in pair[1]
            or "\n" in pair[1]
            or pair[0].lower() in names
        ):
            raise ValueError("temporal response header fields are malformed or repeated")
        names.add(pair[0].lower())
        reconstructed[pair[0]] = pair[1]
    if not _typed_finite_json_equal(headers, reconstructed):
        raise ValueError("temporal response header representations differ")
    lengths = [value for name, value in reconstructed.items() if name.lower() == "content-length"]
    if lengths and lengths != [str(len(body))]:
        raise ValueError("temporal HTTP content length differs from body")
    return body, _aware(record["observed_at"], now)


def _index_dates(session: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    offset = session["GmtOffset"]
    match = re.fullmatch(r"([+-]?)(\d{2}):(\d{2}):(\d{2})", offset) if type(offset) is str else None
    if not match or int(match[2]) >= 24 or any(int(match[n]) >= 60 for n in (3, 4)):
        raise ValueError("temporal official GMT offset is malformed")
    delta = timedelta(hours=int(match[2]), minutes=int(match[3]), seconds=int(match[4]))
    if match[1] == "-":
        delta = -delta
    dates = []
    for field in ("StartDate", "EndDate"):
        raw = session[field]
        if type(raw) is not str:
            raise ValueError("temporal official date is missing")
        local = datetime.fromisoformat(raw)
        if local.tzinfo is not None:
            raise ValueError("temporal official local date unexpectedly carries a timezone")
        dates.append(local.replace(tzinfo=timezone(delta)).astimezone(UTC))
    if not dates[0] < dates[1] <= now:
        raise ValueError("temporal official session is future or its dates are inconsistent")
    return dates[0], dates[1]


def _revision_precision(value: Any) -> None:
    """Revision arithmetic must not silently truncate ISO timestamp precision."""
    if type(value) is str and re.search(r"[.,]\d{7,}", value):
        raise ValueError("temporal schedule revision date precision exceeds microseconds")


class _Replay:
    def __init__(self, root: Path):
        self.root = root
        self.now = datetime.now(UTC)
        self.dependencies: dict[str, str] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.discovery = self.document("inventory.json")
        self.provider = self.document("openf1-inventory.json")
        _expect(self.discovery, {"complete_inventory": True}, "discovery")
        if self.provider.get("failures") or type(self.provider.get("sessions")) is not list:
            raise ValueError("temporal provider source is incomplete")
        entries = self.discovery["sessions"]
        if type(entries) is not list or any(type(row) is not dict for row in entries):
            raise ValueError("temporal discovery entries are malformed")
        self.entries = {validate_safe_identifier(row["session_id"]): row for row in entries}
        if len(self.entries) != len(entries):
            raise ValueError("temporal discovery identities are repeated")
        self.provider_rows: dict[int, dict[str, Any]] = {}
        for row in self.provider["sessions"]:
            if type(row) is not dict:
                raise ValueError("temporal provider row is malformed")
            key = _positive_int(row.get("session_key"))
            if key in self.provider_rows and not _typed_finite_json_equal(
                self.provider_rows[key], row
            ):
                raise ValueError("temporal provider key has conflicting source records")
            self.provider_rows[key] = row

    def document(self, relative: str) -> dict[str, Any]:
        if type(relative) is not str:
            raise ValueError("temporal dependency path is missing")
        if relative not in self.documents:
            value, raw = _json(self.root, relative)
            self.documents[relative] = value
            self.dependencies[relative] = sha256(raw)
        return self.documents[relative]

    def directory_members(self, relative: str, expected: set[str]) -> None:
        directory = _owned_path(self.root, relative)
        if not directory.is_dir() or {path.name for path in directory.iterdir()} != expected:
            raise ValueError("temporal captured batch has missing or unrecorded members")
        for name in expected:
            path = _owned_path(self.root, f"{relative}/{name}")
            if not path.is_file():
                raise ValueError("temporal capture member is not a regular file")

    def index(
        self, capture_relative: str, expected_digest: str, year: int
    ) -> tuple[dict[str, Any], datetime]:
        capture_path = Path(capture_relative)
        if capture_path.name != "response.json" or not capture_relative.startswith(
            "evidence-requests/"
        ):
            raise ValueError("temporal index capture path is outside the captured schema")
        parent = capture_path.parent.as_posix()
        self.directory_members(parent, {"intent.json", "response.json", "outcome.json"})
        intent = self.document(parent + "/intent.json")
        response = self.document(capture_relative)
        outcome = self.document(parent + "/outcome.json")
        _fields(
            intent,
            {
                "schema_version",
                "request_url",
                "allow_redirects",
                "started_at",
                "discovery_sha256",
                "source_discovery_hex",
                "completion_promotion",
                "fastf1_version",
            },
            "index intent",
        )
        _fields(response, RESPONSE_FIELDS, "index response")
        _fields(
            outcome,
            {
                "schema_version",
                "request_url",
                "run_path",
                "finished_at",
                "responses",
                "http_status",
                "error",
                "completion_promotion",
            },
            "index outcome",
        )
        url = index_url(year)
        _expect(
            intent,
            {
                "schema_version": "temporal-index-capture-v1",
                "request_url": url,
                "allow_redirects": False,
                "discovery_sha256": self.dependencies["inventory.json"],
                "completion_promotion": False,
                "fastf1_version": self.discovery["fastf1_version"],
            },
            "index intent",
        )
        source_raw = _owned_path(self.root, "inventory.json").read_bytes()
        _expect(intent, {"source_discovery_hex": source_raw.hex()}, "index frozen discovery bytes")
        if self.dependencies[capture_relative] != expected_digest:
            raise ValueError("temporal index capture digest differs")
        _expect(
            response,
            {
                "schema_version": "temporal-index-response-v1",
                "discovery_sha256": self.dependencies["inventory.json"],
            },
            "index response",
        )
        _expect(
            outcome,
            {
                "schema_version": "temporal-index-outcome-v1",
                "request_url": url,
                "run_path": parent,
                "responses": 1,
                "http_status": 200,
                "error": None,
                "completion_promotion": False,
            },
            "index outcome",
        )
        body, observed = _body(response, url, self.now)
        if (
            not _aware(intent["started_at"], self.now)
            <= observed
            <= _aware(outcome["finished_at"], self.now)
        ):
            raise ValueError("temporal index capture chronology differs")
        index = _parse_json(body)
        if type(index) is not dict or type(index.get("Meetings")) is not list:
            raise ValueError("temporal official index is malformed")
        _expect(index, {"Year": year}, "index year")
        keys: set[int] = set()
        for meeting in index["Meetings"]:
            if type(meeting) is not dict or type(meeting.get("Sessions")) is not list:
                raise ValueError("temporal official meeting is malformed")
            number = meeting.get("Number")
            if number is not None and number != 0:
                _positive_int(number)
            for session in meeting["Sessions"]:
                if type(session) is not dict:
                    raise ValueError("temporal official session is malformed")
                key = _positive_int(session.get("Key"))
                if key in keys:
                    raise ValueError("temporal official session key is repeated")
                keys.add(key)
        return index, observed

    def provider_schedule(
        self,
        capture_relative: str,
        expected_digest: str,
        year: int,
        index_observed: datetime,
        session_started: datetime,
    ) -> dict[int, dict[str, Any]]:
        """Replay one public, source-bound metadata request; never request telemetry."""
        if (
            type(capture_relative) is not str
            or Path(capture_relative).name != "response.json"
            or not capture_relative.startswith("evidence-requests/")
        ):
            raise ValueError("temporal provider schedule capture path differs")
        parent = Path(capture_relative).parent.as_posix()
        self.directory_members(parent, {"intent.json", "response.json", "outcome.json"})
        intent = self.document(parent + "/intent.json")
        response = self.document(capture_relative)
        outcome = self.document(parent + "/outcome.json")
        _fields(
            intent,
            {
                "schema_version",
                "request_url",
                "allow_redirects",
                "started_at",
                "original_provider_sha256",
                "purpose",
                "completion_promotion",
            },
            "provider schedule intent",
        )
        _fields(
            response,
            RESPONSE_FIELDS - {"discovery_sha256"} | {"original_provider_sha256"},
            "provider schedule response",
        )
        _fields(
            outcome,
            {
                "schema_version",
                "request_url",
                "run_path",
                "finished_at",
                "responses",
                "http_status",
                "error",
                "completion_promotion",
            },
            "provider schedule outcome",
        )
        url = f"https://api.openf1.org/v1/sessions?year={year}"
        original_digest = self.dependencies["openf1-inventory.json"]
        _expect(
            intent,
            {
                "schema_version": "provider-schedule-intent-v1",
                "request_url": url,
                "allow_redirects": False,
                "original_provider_sha256": original_digest,
                "purpose": "verify same-key qualifying schedule revision; no completion promotion",
                "completion_promotion": False,
            },
            "provider schedule intent",
        )
        _expect(
            response,
            {
                "schema_version": "provider-schedule-response-v1",
                "original_provider_sha256": original_digest,
            },
            "provider schedule response",
        )
        _expect(
            outcome,
            {
                "schema_version": "provider-schedule-outcome-v1",
                "request_url": url,
                "run_path": parent,
                "responses": 1,
                "http_status": 200,
                "error": None,
                "completion_promotion": False,
            },
            "provider schedule outcome",
        )
        if self.dependencies[capture_relative] != expected_digest:
            raise ValueError("temporal provider schedule capture digest differs")
        body, observed = _body(response, url, self.now)
        began = _aware(intent["started_at"], self.now)
        ended = _aware(outcome["finished_at"], self.now)
        if not (began <= observed <= ended < session_started and index_observed <= observed):
            raise ValueError("temporal provider schedule capture chronology differs")
        rows = _parse_json(body)
        if type(rows) is not list or not rows:
            raise ValueError("temporal provider schedule body must contain session rows")
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            if type(row) is not dict:
                raise ValueError("temporal provider schedule row is malformed")
            key = _positive_int(row.get("session_key"))
            if key in result:
                raise ValueError("temporal provider schedule session key is repeated")
            result[key] = row
        return result

    def official(
        self,
        index: dict[str, Any],
        entry: dict[str, Any],
        schedule_capture: tuple[str, str, datetime, datetime] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], datetime, datetime]:
        for field in ("season", "round_number", "fastf1_slot"):
            _positive_int(entry.get(field))
        # Testing and unrelated meetings can reuse Number. Only an exact bound
        # championship round/name candidate supplies authority; never rename it.
        meetings = [
            row
            for row in index["Meetings"]
            if _typed_finite_json_equal(row.get("Number"), entry["round_number"])
            and row.get("Name") == entry["event_name"]
        ]
        if len(meetings) != 1:
            raise ValueError("temporal official meeting identity is not unique")
        meeting = meetings[0]
        _expect(
            meeting, {"Name": entry["event_name"], "Location": entry["circuit_name"]}, "meeting"
        )
        _expect(meeting["Country"], {"Name": entry["country"]}, "meeting country")
        matching = [
            row
            for row in meeting["Sessions"]
            if row.get("Name") == entry["provider_session_name"]
            and canonical_session_type(entry["season"], row["Name"]) == entry["session_type"]
        ]
        if len(matching) != 1 or meeting["Sessions"][entry["fastf1_slot"] - 1] is not matching[0]:
            raise ValueError("temporal official session name, type or slot differs")
        session = matching[0]
        if schedule_capture is not None:
            for value in (session["StartDate"], session["EndDate"], entry["scheduled_at"]):
                _revision_precision(value)
        start, end = _index_dates(session, self.now)
        if schedule_capture is None and start != _aware(entry["scheduled_at"], self.now):
            raise ValueError("temporal official start differs from original schedule")
        key = _positive_int(session["Key"])
        provider = self.provider_rows.get(key)
        if provider is None:
            raise ValueError("temporal official key lacks bound OpenF1 source")
        _expect(
            provider,
            {
                "session_key": key,
                "year": entry["season"],
                "session_name": entry["provider_session_name"],
                "session_type": session["Type"],
                "meeting_key": meeting["Key"],
                "location": meeting["Location"],
                "country_name": meeting["Country"]["Name"],
                "country_key": meeting["Country"]["Key"],
                "country_code": meeting["Country"]["Code"],
                "circuit_key": meeting["Circuit"]["Key"],
                "circuit_short_name": meeting["Circuit"]["ShortName"],
                "gmt_offset": session["GmtOffset"],
                "is_cancelled": False,
            },
            "official/OpenF1 provider",
        )
        if schedule_capture is not None:
            original = provider
            fresh_rows = self.provider_schedule(
                schedule_capture[0],
                schedule_capture[1],
                entry["season"],
                schedule_capture[2],
                schedule_capture[3],
            )
            fresh = fresh_rows.get(key)
            if fresh is None or set(fresh) != set(original):
                raise ValueError("temporal revised provider row identity or fields differ")
            if sum(row["session_key"] == key for row in self.provider["sessions"]) != 1:
                raise ValueError("temporal original provider session key is repeated")
            provider = fresh
            for row in (original, provider):
                for field in ("date_start", "date_end"):
                    _revision_precision(row[field])
            _expect(
                provider,
                {
                    name: value
                    for name, value in original.items()
                    if name not in {"date_start", "date_end"}
                },
                "revised provider frozen identity",
            )
            old_start = _aware(original["date_start"], self.now)
            old_end = _aware(original["date_end"], self.now)
            if (
                old_start != _aware(entry["scheduled_at"], self.now)
                or not old_start < old_end
                or start <= old_start
                or end - old_end != start - old_start
            ):
                raise ValueError("temporal revision is not a positive duration-preserving delay")
        if (
            _aware(provider["date_start"], self.now) != start
            or _aware(provider["date_end"], self.now) != end
        ):
            raise ValueError("temporal official/OpenF1 dates differ")
        # status_url performs exact contained relative-path validation and encoding.
        status_url(entry, {"timing_path": session["Path"]})
        return session, provider, start, end

    def batch(self, run_relative: str) -> dict[str, dict[str, Any]]:
        if type(run_relative) is not str or not run_relative.startswith("evidence-requests/"):
            raise ValueError("temporal session capture path differs")
        intent = self.document(run_relative + "/intent.json")
        outcome = self.document(run_relative + "/outcome.json")
        revised = intent.get("schema_version") == "temporal-session-capture-v2"
        _fields(
            intent,
            {
                "schema_version",
                "started_at",
                "index_capture_path",
                "index_capture_sha256",
                "discovery_sha256",
                "openf1_inventory_sha256",
                "planned_requests",
                "completion_promotion",
                "allow_redirects",
                "fastf1_version",
            }
            | (
                {"provider_schedule_capture_path", "provider_schedule_capture_sha256"}
                if revised
                else set()
            ),
            "session intent",
        )
        _fields(
            outcome,
            {
                "schema_version",
                "run_path",
                "finished_at",
                "planned_count",
                "responses",
                "error",
                "completion_promotion",
            },
            "session outcome",
        )
        _expect(
            intent,
            {
                "schema_version": "temporal-session-capture-v2"
                if revised
                else "temporal-session-capture-v1",
                "allow_redirects": False,
                "discovery_sha256": self.dependencies["inventory.json"],
                "openf1_inventory_sha256": self.dependencies["openf1-inventory.json"],
                "completion_promotion": False,
                "fastf1_version": self.discovery["fastf1_version"],
            },
            "session intent",
        )
        planned = intent["planned_requests"]
        if type(planned) is not list or not planned or len(planned) % 2:
            raise ValueError("temporal session batch must contain paired requests")
        count = len(planned)
        _expect(
            outcome,
            {
                "schema_version": "temporal-session-outcome-v1",
                "run_path": run_relative,
                "planned_count": count,
                "error": None,
                "completion_promotion": False,
            },
            "session outcome",
        )
        results = outcome["responses"]
        if type(results) is not list or len(results) != count:
            raise ValueError("temporal outcome must record all and only planned responses")
        self.directory_members(
            run_relative,
            {
                "intent.json",
                "outcome.json",
                *(f"response-{n:02d}.json" for n in range(1, count + 1)),
            },
        )
        year = self.entries[planned[0]["session_id"]]["season"]
        index, index_observed = self.index(
            intent["index_capture_path"], intent["index_capture_sha256"], year
        )
        started = _aware(intent["started_at"], self.now)
        finished = _aware(outcome["finished_at"], self.now)
        if not index_observed <= started <= finished:
            raise ValueError("temporal session capture predates its index or outcome")
        observed_previous = started
        batch: dict[str, dict[str, Any]] = {}
        for n, (request, result) in enumerate(zip(planned, results, strict=True), 1):
            _fields(request, PLAN_FIELDS, "planned request")
            _fields(
                result,
                {"path", "sha256", "session_id", "resource", "http_status"},
                "response outcome",
            )
            identity = validate_safe_identifier(request["session_id"])
            entry = self.entries[identity]
            _expect(
                entry,
                {"season": year, "completion": "future", "fastf1_supported": True},
                "frozen future entry",
            )
            if identity not in batch:
                if n % 2 != 1:
                    raise ValueError("temporal request pair order differs")
                schedule_capture = (
                    (
                        intent["provider_schedule_capture_path"],
                        intent["provider_schedule_capture_sha256"],
                        index_observed,
                        started,
                    )
                    if revised
                    else None
                )
                session, provider, start, end = self.official(index, entry, schedule_capture)
                source_relative = f"completion/{identity}.json"
                source = self.document(source_relative)
                _expect(
                    source,
                    {
                        "session_id": identity,
                        "state": "future",
                        "statuses": [],
                        "times": [],
                        "error": None,
                    },
                    "frozen future source",
                )
                discovered = _aware(self.discovery["discovered_at"], self.now)
                if (
                    not _aware(source["observed_at"], self.now)
                    < _aware(entry["scheduled_at"], self.now)
                    or not discovered
                    <= _aware(source["observed_at"], self.now)
                    < start
                    < end
                    < started
                ):
                    raise ValueError("temporal future observation and later capture dates differ")
                batch[identity] = {
                    "entry": entry,
                    "source": source,
                    "official": session,
                    "provider": provider,
                    "start": start,
                    "end": end,
                    "responses": {},
                }
                if revised:
                    original = self.provider_rows[session["Key"]]
                    batch[identity]["original_provider_session"] = original
                    batch[identity]["schedule_revision"] = {
                        "original_date_start": original["date_start"],
                        "original_date_end": original["date_end"],
                        "revised_date_start": provider["date_start"],
                        "revised_date_end": provider["date_end"],
                        "delay_seconds": (
                            start - _aware(original["date_start"], self.now)
                        ).total_seconds(),
                        "provider_schedule_capture_path": intent["provider_schedule_capture_path"],
                        "provider_schedule_capture_sha256": intent[
                            "provider_schedule_capture_sha256"
                        ],
                    }
            item = batch[identity]
            resource = request["resource"]
            if resource != RESOURCES[(n - 1) % 2] or resource in item["responses"]:
                raise ValueError("temporal planned request membership is repeated or incomplete")
            if n % 2 == 0 and planned[n - 2]["session_id"] != identity:
                raise ValueError("temporal request pair contains different sessions")
            expected_url = status_url(entry, {"timing_path": item["official"]["Path"]}).replace(
                RESOURCES[0], resource
            )
            _expect(
                request,
                {
                    "request_url": expected_url,
                    "timing_path": item["official"]["Path"],
                    "timing_key": item["official"]["Key"],
                    "source_completion_sha256": self.dependencies[f"completion/{identity}.json"],
                },
                "planned source identity",
            )
            for field, expected in (
                ("scheduled_at", item["start"]),
                ("scheduled_end_at", item["end"]),
            ):
                if _aware(request[field], self.now) != expected:
                    raise ValueError("temporal planned date differs from bound source")
            relative = f"{run_relative}/response-{n:02d}.json"
            captured = self.document(relative)
            _fields(
                captured,
                RESPONSE_FIELDS | PLAN_FIELDS | {"index_capture_path", "index_capture_sha256"},
                "session response",
            )
            _expect(
                result,
                {
                    "path": relative,
                    "sha256": self.dependencies[relative],
                    "session_id": identity,
                    "resource": resource,
                    "http_status": 200,
                },
                "response outcome",
            )
            _expect(
                captured,
                {
                    **{
                        key: value
                        for key, value in request.items()
                        if key not in {"scheduled_at", "scheduled_end_at"}
                    },
                    "schema_version": "temporal-session-response-v1",
                    "index_capture_path": intent["index_capture_path"],
                    "index_capture_sha256": intent["index_capture_sha256"],
                    "discovery_sha256": self.dependencies["inventory.json"],
                },
                "captured request",
            )
            for field in ("scheduled_at", "scheduled_end_at"):
                if _aware(captured[field], self.now) != _aware(request[field], self.now):
                    raise ValueError("temporal captured/planned dates differ")
            body, observed = _body(captured, expected_url, self.now)
            if not observed_previous <= observed <= finished or observed <= item["end"]:
                raise ValueError("temporal response chronology differs")
            observed_previous = observed
            item["responses"][resource] = (captured, body, relative)
        for item in batch.values():
            if set(item["responses"]) != set(RESOURCES):
                raise ValueError("temporal batch contains an unpaired session")
            parsed = parse_status_stream(
                item["responses"][RESOURCES[0]][1], terminal_policy=TERMINAL_AUX_POLICY
            )
            if status_completion(parsed, "past-unverified", supported=True) != "completed":
                raise ValueError("temporal batch stream lacks unambiguous terminal completion")
            archive = _parse_json(item["responses"][RESOURCES[1]][1])
            if not _typed_finite_json_equal(archive, {"Status": "Complete"}):
                raise ValueError("temporal batch archive is not exactly Complete")
        if revised:
            self.directory_members(
                run_relative,
                {
                    "intent.json",
                    "outcome.json",
                    *(f"response-{n:02d}.json" for n in range(1, count + 1)),
                },
            )
            for capture in (intent["index_capture_path"], intent["provider_schedule_capture_path"]):
                self.directory_members(
                    Path(capture).parent.as_posix(),
                    {"intent.json", "response.json", "outcome.json"},
                )
            for relative, digest in self.dependencies.items():
                if sha256(_json(self.root, relative)[1]) != digest:
                    raise ValueError("temporal schedule revision dependency changed during replay")
        return batch


def build_temporal_completion(
    root: Path, original_entry: dict[str, Any], run_relative: str
) -> dict[str, Any]:
    """Build one proof by strict replay of the complete offline request batch."""
    try:
        replay = _Replay(root)
        identity = validate_safe_identifier(original_entry["session_id"])
        if not _typed_finite_json_equal(replay.entries.get(identity), original_entry):
            raise ValueError("temporal source entry differs from immutable discovery")
        batch = replay.batch(run_relative)
        if identity not in batch:
            raise ValueError("temporal source entry was not a planned capture")
        item = batch[identity]
        captured, body, status_relative = item["responses"][RESOURCES[0]]
        archive_capture, archive_body, archive_relative = item["responses"][RESOURCES[1]]
        archive = _parse_json(archive_body)
        if not _typed_finite_json_equal(archive, {"Status": "Complete"}):
            raise ValueError("temporal archive is not exactly Complete")
        parsed = parse_status_stream(body, terminal_policy=TERMINAL_AUX_POLICY)
        # Archive finality must not waive malformed streams or a restarted latest cycle.
        if status_completion(parsed, "past-unverified", supported=True) != "completed":
            raise ValueError("temporal stream lacks unambiguous latest terminal completion")
        statuses = [record["status"] for record in parsed["records"]]
        times = [_time_string(record["time"]) for record in parsed["records"]]
        nanos = [_millis(record["time"]) * 1_000_000 for record in parsed["records"]]
        source = item["source"]
        effective = {
            **original_entry,
            "completion": "past-unverified",
            "timing_path": item["official"]["Path"],
            "timing_key": item["official"]["Key"],
        }
        parser = {key: value for key, value in parsed.items() if key != "source_bytes"}
        evidence = {
            **source,
            "schema_version": SCHEMA,
            "source_classification": "future",
            "source_observation": deepcopy(source),
            "source_completion_sha256": replay.dependencies[f"completion/{identity}.json"],
            "source_discovery_sha256": replay.dependencies["inventory.json"],
            "timing_path": effective["timing_path"],
            "timing_key": effective["timing_key"],
            "statuses": statuses,
            "times": times,
            "observed_at": captured["observed_at"],
            "state": "completed",
            "completion_integrity": "verified",
            "parser": parser,
            "wire": deepcopy(captured),
        }
        return {
            "schema_version": SCHEMA,
            "session_id": identity,
            "state": "completed",
            "source_classification": "future",
            "capture_run_path": run_relative,
            "status_capture_path": status_relative,
            "archive_capture_path": archive_relative,
            "dependencies": replay.dependencies,
            "effective_entry": effective,
            "evidence": evidence,
            "parser": parser,
            "archive": archive,
            "archive_capture": archive_capture,
            "official_session": item["official"],
            "provider_session": item["provider"],
            "time_nanoseconds": nanos,
            **(
                {key: item[key] for key in ("original_provider_session", "schedule_revision")}
                if "schedule_revision" in item
                else {}
            ),
        }
    except (
        OSError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise ValueError("missing or malformed temporal completion evidence") from exc


def load_temporal_completions(
    root: Path, original_entries: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Published proofs are authority only when their complete typed replay agrees."""
    entries = {validate_safe_identifier(row["session_id"]): row for row in original_entries}
    if len(entries) != len(original_entries):
        raise ValueError("temporal source inventory contains duplicate identities")
    directory = _owned_path(root, PROOF_DIRECTORY)
    proofs: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    if not directory.exists():
        return proofs, hashes
    for path in sorted(directory.iterdir()):
        identity = validate_safe_identifier(path.stem)
        if path.suffix != ".json" or identity not in entries:
            raise ValueError("temporal proof contains an unknown member or identity")
        proof, raw = _json(root, f"{PROOF_DIRECTORY}/{identity}.json")
        current = build_temporal_completion(root, entries[identity], proof["capture_run_path"])
        if not _typed_finite_json_equal(proof, current):
            raise ValueError("temporal proof is stale or differs from full typed replay")
        proofs[identity] = current
        hashes[identity] = sha256(raw)
    return proofs, hashes


def effective_source_view(
    original_entries: list[dict[str, Any]],
    proofs: dict[str, dict[str, Any]],
    hashes: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Use only loader-replayed proofs; original fields besides completion stay frozen."""
    entries = {row["session_id"]: deepcopy(row) for row in original_entries}
    if (
        len(entries) != len(original_entries)
        or set(proofs) != set(hashes)
        or not set(proofs).issubset(entries)
    ):
        raise ValueError("temporal effective source identity/hash map differs")
    for identity, proof in proofs.items():
        effective = proof["effective_entry"]
        expected = {
            **entries[identity],
            "completion": "past-unverified",
            "timing_path": proof["official_session"]["Path"],
            "timing_key": proof["official_session"]["Key"],
        }
        if entries[identity]["completion"] != "future" or not _typed_finite_json_equal(
            effective, expected
        ):
            raise ValueError("temporal effective source changed immutable discovery fields")
        if type(hashes[identity]) is not str or not re.fullmatch(r"[0-9a-f]{64}", hashes[identity]):
            raise ValueError("temporal effective source proof hash is malformed")
        entries[identity] = {**deepcopy(effective), "completion_temporal_sha256": hashes[identity]}
    return entries


def temporal_native_observations(
    frame: Any, proof: dict[str, Any], *, captured: bool = False
) -> dict[str, Any]:
    """Require ordered native nanosecond parity with a loader-replayed wire proof."""
    _expect(
        proof,
        {"schema_version": SCHEMA, "state": "completed", "source_classification": "future"},
        "native proof",
    )
    wire = proof["evidence"]["wire"]
    body = bytes.fromhex(wire["body_hex"])
    if sha256(body) != wire.get("body_sha256"):
        raise ValueError("temporal native wire checksum differs")
    parsed = parse_status_stream(body, terminal_policy=TERMINAL_AUX_POLICY)
    parser = {key: value for key, value in parsed.items() if key != "source_bytes"}
    if not parsed["valid"] or not _typed_finite_json_equal(parser, proof["parser"]):
        raise ValueError("temporal native parser differs from wire")
    observations = native_status_observations(frame, captured=captured)
    if not _typed_finite_json_equal(
        observations,
        {
            "statuses": proof["evidence"]["statuses"],
            "times": proof["evidence"]["times"],
            "time_nanoseconds": proof["time_nanoseconds"],
        },
    ):
        raise ValueError("temporal native ordered statuses or nanosecond timestamps differ")
    return observations
