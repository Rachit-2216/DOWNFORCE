"""DOWNFORCE-owned offline wire evidence; never constructs provider HTTP clients."""

from __future__ import annotations

import hashlib
import io
import json
import math
import pickle
import pickletools
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from downforce_core.detailed.catalog import discover_schedule
from downforce_core.detailed.reconciliation import classify_completion
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.layout import write_text_atomic

PARSER_POLICY_VERSION = "status-wire-v1"
TERMINAL_AUX_POLICY = "status-wire-terminal-aux-v1"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PICKLE_BYTES = 256 * 1024 * 1024
KNOWN_STATUSES = {"Inactive", "Started", "Finished", "Finalised", "Ends", "Aborted"}
OFFICIAL_BASE = "https://livetiming.formula1.com/static/"
CONFIGURED_MIRROR_BASE = "https://livetiming-mirror.fastf1.dev/static/"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _bounded_json_depth(value: Any) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 50:
            raise ValueError("JSON nesting exceeds bound")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _typed_finite_json_equal(left: Any, right: Any) -> bool:
    """JSON equality without bool/int, int/float or nonfinite aliases."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            _typed_finite_json_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _typed_finite_json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is float:
        return math.isfinite(left) and math.isfinite(right) and left == right
    return type(left) in (str, int, bool, type(None)) and left == right


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _auxiliary_terminal_progression(previous: Any, current: Any) -> bool:
    return (
        type(previous) is dict
        and type(current) is dict
        and previous.get("Status") == "Finalised"
        and current.get("Status") == "Ends"
        and _typed_finite_json_equal(
            {key: value for key, value in previous.items() if key != "Status"},
            {key: value for key, value in current.items() if key != "Status"},
        )
    )


def parse_status_stream(body: bytes, *, terminal_policy: str = "legacy") -> dict[str, Any]:
    """Preserve every line and the exact wire; report, never repair, bad records.

    The 12-byte elapsed timestamp is followed immediately by one JSON object.
    LF and CRLF are accepted; only the single trailing line separator is removed.
    Repeated identical records remain present, conflicting same-time objects do not
    acquire an invented order. `source_bytes` is deliberately separate from JSON.
    """
    if terminal_policy not in ("legacy", TERMINAL_AUX_POLICY):
        raise ValueError("unknown status terminal policy")
    policy_version = PARSER_POLICY_VERSION if terminal_policy == "legacy" else TERMINAL_AUX_POLICY
    if type(body) is not bytes:
        raise TypeError("status stream must be bytes")
    if len(body) > MAX_BODY_BYTES:
        return {
            "source_bytes": body,
            "body_sha256": sha256(body),
            "records": [],
            "issues": [{"record": None, "code": "body-too-large"}],
            "notes": [],
            "valid": False,
            "parser_policy_version": policy_version,
        }
    content = body.removeprefix(b"\xef\xbb\xbf")
    lines = content.split(b"\n") if content else []
    if lines and lines[-1] == b"" and content.endswith(b"\n"):
        lines.pop()
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    previous: int | None = None
    timestamps: dict[int, Any] = {}
    for number, source in enumerate(lines, 1):
        line = source.removesuffix(b"\r") if source.endswith(b"\r") else source
        record: dict[str, Any] = {
            "number": number,
            "source_hex": line.hex(),
            "time": None,
            "status": None,
            "value": None,
        }
        records.append(record)
        match = re.match(rb"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})", line)
        millis = None
        if match and all(int(match[n]) < 60 for n in (2, 3)) and int(match[1]) < 24:
            record["time"] = line[:12].decode("ascii")
            millis = ((int(match[1]) * 60 + int(match[2])) * 60 + int(match[3])) * 1000 + int(
                match[4]
            )
        else:
            issues.append({"record": number, "code": "invalid-timestamp"})
        try:
            value = json.loads(
                line[12:].decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
                parse_float=_finite_json_float if terminal_policy != "legacy" else None,
            )
            if not isinstance(value, dict) or type(value.get("Status")) is not str:
                raise ValueError("status record must be an object with a string Status")
            _bounded_json_depth(value)
            record.update(value=value, status=value["Status"])
            if value["Status"] not in KNOWN_STATUSES:
                issues.append({"record": number, "code": "unknown-status"})
        except (UnicodeDecodeError, ValueError, RecursionError):
            issues.append({"record": number, "code": "malformed-record"})
        if millis is not None:
            if previous is not None and millis < previous:
                issues.append({"record": number, "code": "nonmonotonic"})
            same = (
                _typed_finite_json_equal(timestamps.get(millis), record["value"])
                if terminal_policy != "legacy"
                else timestamps.get(millis) == record["value"]
            )
            if millis in timestamps and not same:
                terminal = (
                    _auxiliary_terminal_progression(timestamps[millis], record["value"])
                    if terminal_policy != "legacy"
                    else timestamps[millis] == {"Status": "Finalised"}
                    and record["value"] == {"Status": "Ends"}
                )
                if terminal:
                    notes.append({"record": number, "code": "coincident-terminal-progression"})
                else:
                    issues.append({"record": number, "code": "timestamp-conflict"})
            timestamps[millis] = record["value"]
            previous = millis
    if not records:
        issues.append({"record": None, "code": "empty-stream"})
    return {
        "source_bytes": body,
        "body_sha256": sha256(body),
        "records": records,
        "issues": issues,
        "notes": notes,
        "valid": not issues,
        "parser_policy_version": policy_version,
    }


def status_completion(
    parsed: dict[str, Any],
    scheduled_state: str,
    *,
    supported: bool | None,
    archive_complete: bool = False,
) -> str:
    if not parsed["valid"]:
        return "unverified"
    return classify_completion(
        scheduled_state,
        [r["status"] for r in parsed["records"]],
        supported=supported,
        archive_complete=archive_complete,
    )


class _PrimitiveUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        raise ValueError("pickle globals are forbidden")

    def persistent_load(self, pid: Any) -> Any:
        raise ValueError("pickle persistent references are forbidden")


def primitive_unpickle(blob: bytes) -> dict[str, Any]:
    if len(blob) > MAX_PICKLE_BYTES:
        raise ValueError("cache pickle exceeds bounded size")
    try:
        # A primed copyreg extension cache can bypass find_class entirely. Reject
        # every object-construction/execution opcode before any deserialization.
        executable = {
            "EXT1",
            "EXT2",
            "EXT4",
            "REDUCE",
            "NEWOBJ",
            "NEWOBJ_EX",
            "BUILD",
            "INST",
            "OBJ",
            "GLOBAL",
            "STACK_GLOBAL",
            "PERSID",
            "BINPERSID",
        }
        for opcode, _argument, _position in pickletools.genops(blob):
            if opcode.name in executable:
                raise ValueError("pickle globals or executable opcode are forbidden")
        stream = io.BytesIO(blob)
        value = _PrimitiveUnpickler(stream).load()
        if stream.read(1):
            raise ValueError("trailing pickle bytes")

        def primitive(item: Any, depth: int = 0) -> bool:
            if depth > 30:
                return False
            if type(item) in (str, bytes, int, float, bool, type(None)):
                return True
            if type(item) in (list, tuple):
                return all(primitive(x, depth + 1) for x in item)
            if type(item) is dict:
                return all(
                    primitive(k, depth + 1) and primitive(v, depth + 1) for k, v in item.items()
                )
            return False

        if type(value) is not dict or not primitive(value):
            raise ValueError("cache response is not a primitive dictionary")
        return value
    except (pickle.UnpicklingError, EOFError, RecursionError) as exc:
        raise ValueError("invalid primitive cache pickle") from exc


def read_cached_response(
    cache: Path, expected_url: str, *, max_body_bytes: int = MAX_BODY_BYTES
) -> dict[str, Any]:
    """Read saved primitive response dictionaries with SQLite mode=ro only."""
    if not cache.is_file():
        raise ValueError("missing exact cached source URL")
    matches: list[tuple[str, dict[str, Any], str]] = []
    with sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        for key, blob in db.execute("SELECT key, value FROM responses ORDER BY key"):
            response = primitive_unpickle(blob)
            if response.get("url") != expected_url:
                continue
            _validate_cached_response(response, expected_url, max_body_bytes)
            matches.append((str(key), response, sha256(blob)))
    return _cached_response_proof(cache, expected_url, matches)


def _validate_cached_response(
    response: dict[str, Any], expected_url: str, max_body_bytes: int
) -> None:
    body = response.get("_content")
    request = response.get("request")
    if type(body) is not bytes or len(body) > max_body_bytes:
        raise ValueError("cached body must be bounded bytes")
    if response.get("status_code") != 200:
        raise ValueError("cached source requires HTTP 200")
    if not isinstance(request, dict) or request.get("url") != expected_url:
        raise ValueError("cached request identity differs from expected URL")
    created = response.get("created_at")
    if not isinstance(created, str):
        raise ValueError("cached capture date is missing")
    datetime.fromisoformat(created.replace("Z", "+00:00"))
    if not isinstance(response.get("headers"), dict):
        raise ValueError("cached response headers are missing")


def _cached_response_proof(
    cache: Path, expected_url: str, matches: list[tuple[str, dict[str, Any], str]]
) -> dict[str, Any]:
    if not matches:
        raise ValueError("missing exact cached source URL")
    first = matches[0][1]
    if any(record != first for _, record, _ in matches[1:]):
        raise ValueError("conflicting cached responses for exact URL")
    return {
        "url": expected_url,
        "http_status": 200,
        "body_hex": first["_content"].hex(),
        "body_sha256": sha256(first["_content"]),
        "created_at": first["created_at"],
        "headers": first["headers"],
        "cache_path": str(cache.resolve()),
        "cache_keys": [key for key, _, _ in matches],
        "response_pickle_sha256": [digest for _, _, digest in matches],
    }


def _owned_path(root: Path, relative: str) -> Path:
    """Evidence references are contained regular paths, never linked aliases."""
    token = Path(relative)
    if token.is_absolute() or ".." in token.parts or ":" in relative or "\\" in relative:
        raise ValueError("unsafe evidence path")
    path = root / token
    cursor = path
    while True:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("linked evidence paths are forbidden")
        if cursor == root:
            break
        if root not in cursor.parents:
            raise ValueError("evidence path escaped root")
        cursor = cursor.parent
    return path


class _FallbackCacheReader:
    """Strict source snapshots scoped to one fallback build, never reused across calls."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshots: dict[
            str,
            tuple[
                dict[str, tuple[str, int, int, str] | None],
                dict[str, list[tuple[str, dict[str, Any], str]]],
                Exception | None,
            ],
        ] = {}

    def _fingerprint(self, relative: str) -> dict[str, tuple[str, int, int, str] | None]:
        members: dict[str, tuple[str, int, int, str] | None] = {}
        # SQLite may serve committed content from WAL, not just the main file.
        for suffix in ("", "-wal", "-shm", "-journal"):
            name = relative + suffix
            path = _owned_path(self.root, name)
            if not path.exists():
                members[name] = None
                continue
            before = path.stat()
            if not path.is_file():
                members[name] = (str(path.resolve()), before.st_dev, before.st_ino, "not-a-file")
                continue
            digest = sha256(path.read_bytes())
            after = path.stat()
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ValueError("cached source changed during fallback snapshot")
            members[name] = (str(path.resolve()), after.st_dev, after.st_ino, digest)
        return members

    def read(self, relative: str, expected_url: str) -> dict[str, Any]:
        cache = _owned_path(self.root, relative)
        if relative not in self.snapshots:
            members = self._fingerprint(relative)
            indexed: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
            scan_error: Exception | None = None
            if cache.is_file():
                with closing(
                    sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True)
                ) as db:
                    db.execute("PRAGMA query_only=ON")
                    for key, blob in db.execute("SELECT key, value FROM responses ORDER BY key"):
                        try:
                            response = primitive_unpickle(blob)
                        except Exception as exc:
                            # Preserve legacy error order: a matching earlier row's
                            # validation error takes precedence over this scan error.
                            scan_error = exc
                            break
                        url = response.get("url")
                        if type(url) is str:
                            indexed.setdefault(url, []).append((str(key), response, sha256(blob)))
            if self._fingerprint(relative) != members:
                raise ValueError("cached source changed during fallback snapshot")
            self.snapshots[relative] = (members, indexed, scan_error)
        _, indexed, scan_error = self.snapshots[relative]
        matches = indexed.get(expected_url, [])
        for _, response, _ in matches:
            _validate_cached_response(response, expected_url, MAX_BODY_BYTES)
        if scan_error is not None:
            raise scan_error
        return _cached_response_proof(cache, expected_url, matches)

    def verify_unchanged(self) -> None:
        for relative, (members, _, _) in self.snapshots.items():
            if self._fingerprint(relative) != members:
                raise ValueError("cached source changed during fallback replay")


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    if type(value) is not dict:
        raise ValueError("evidence document must be an object")
    return value


def schedule_url(year: int) -> str:
    return f"https://raw.githubusercontent.com/theOehrly/f1schedule/master/schedule_{year}.json"


def index_url(year: int) -> str:
    return f"https://livetiming.formula1.com/static/{year}/Index.json"


def status_url(entry: dict[str, Any], source: dict[str, Any]) -> str:
    relative = source.get("timing_path")
    if (
        not isinstance(relative, str)
        or not relative.startswith(f"{entry['season']}/")
        or not relative.endswith("/")
        or ".." in relative
        or ":" in relative
        or "\\" in relative
        or "?" in relative
        or "#" in relative
    ):
        raise ValueError("unsafe or missing timing identity path")
    # This is transport encoding, not a source path substitution: requests stores
    # PreparedRequest URLs with UTF-8 percent-encoded non-ASCII path characters.
    return OFFICIAL_BASE + quote(relative, safe="/%") + "SessionStatus.jsonStream"


def _status_urls(entry: dict[str, Any], source: dict[str, Any]) -> tuple[str, str]:
    primary = status_url(entry, source)
    return primary, CONFIGURED_MIRROR_BASE + primary.removeprefix(OFFICIAL_BASE)


def _time_string(value: str) -> str:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})", value)
    if match is None:
        raise ValueError("invalid proof timestamp")
    return str(
        timedelta(
            hours=int(match[1]),
            minutes=int(match[2]),
            seconds=int(match[3]),
            milliseconds=int(match[4]),
        )
    )


def derive_completion(
    entry: dict[str, Any], source_bytes: bytes, wire: dict[str, Any]
) -> dict[str, Any]:
    source = json.loads(source_bytes, object_pairs_hook=_unique_object)
    identity = entry["session_id"]
    validate_safe_identifier(identity)
    if source.get("session_id") != identity or wire.get("url") not in _status_urls(entry, source):
        raise ValueError("completion source identity differs")
    body = bytes.fromhex(wire["body_hex"])
    if wire.get("http_status") != 200 or sha256(body) != wire.get("body_sha256"):
        raise ValueError("completion wire checksum or HTTP status differs")
    parsed = parse_status_stream(body)
    records = parsed["records"]
    values = [r["status"] for r in records]
    times = [_time_string(r["time"]) if r["time"] else None for r in records]
    # Validate old captured records, not just a new boolean. The only permitted
    # legacy loss is FastF1's documented CRLF[:-1] last-record truncation.
    candidates = [(values, times)]
    if not body.endswith(b"\r\n") and b"\r\n" in body:
        candidates.append((values[:-1], times[:-1]))
    matched = [
        n
        for n, (statuses, timestamps) in enumerate(candidates)
        if source.get("statuses") == statuses
        and (not source.get("times") or source["times"] == timestamps)
    ]
    if not matched:
        raise ValueError("captured completion sequence conflicts with source wire")
    parser = {k: v for k, v in parsed.items() if k != "source_bytes"}
    integrity = (
        "verified"
        if parsed["valid"]
        else (
            "ambiguous"
            if any(i["code"] in {"timestamp-conflict", "nonmonotonic"} for i in parsed["issues"])
            else "invalid"
        )
    )
    return {
        **source,
        "schema_version": "derived-completion-v1",
        "source_classification": source["state"],
        "source_completion_sha256": sha256(source_bytes),
        "wire": wire,
        "parser": parser,
        "legacy_comparison": "exact" if 0 in matched else "missing-nonnewline-final-record",
        "completion_integrity": integrity,
        "statuses": values,
        "times": times,
        "state": status_completion(
            parsed, entry["completion"], supported=entry.get("fastf1_supported")
        ),
    }


def _schedule_rows(body: bytes) -> list[dict[str, Any]]:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("schedule body exceeds bounded size")
    data = json.loads(body, object_pairs_hook=_unique_object)
    if type(data) is not dict or not data or any(type(col) is not dict for col in data.values()):
        raise ValueError("schedule must be the captured dict-of-dicts backend")
    keys = list(next(iter(data.values())))
    if any(list(col) != keys for col in data.values()):
        raise ValueError("schedule columns have conflicting row identities")
    required = {
        "round_number",
        "country",
        "location",
        "event_name",
        "event_date",
        "gmt_offset",
        "f1_api_support",
        *(f"session{n}" for n in range(1, 6)),
        *(f"session{n}_date" for n in range(1, 6)),
    }
    if not required.issubset(data):
        raise ValueError("schedule backend fields are missing")
    # Match installed FastF1 3.8.3 _get_schedule_ff1: column value insertion
    # order, local tz replacement, UTC conversion and UpperCamelCase names.
    columns = {key: list(col.values()) for key, col in data.items()}
    rows = []
    for i in range(len(keys)):
        row = {
            "".join(s.capitalize() for s in key.split("_")): values[i]
            for key, values in columns.items()
            if key != "gmt_offset"
        }
        offset = columns["gmt_offset"][i]
        if offset is None:
            delta = timedelta(0)
        else:
            match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset)
            if not match or int(match[3]) >= 60:
                raise ValueError("invalid schedule GMT offset")
            delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
            if match[1] == "-":
                delta = -delta
        tz = timezone(delta)
        row["EventDate"] = datetime.fromisoformat(columns["event_date"][i]).replace(
            hour=0, minute=0, second=0
        )
        for n in range(1, 6):
            raw = columns[f"session{n}_date"][i]
            local = datetime.fromisoformat(raw).replace(tzinfo=tz) if raw is not None else None
            row[f"Session{n}Date"] = local
            row[f"Session{n}DateUtc"] = (
                local.astimezone(UTC).replace(tzinfo=None) if local else None
            )
        rows.append(row)
    return rows


def schedule_sessions(body: bytes, year: int, *, now: datetime) -> list[dict[str, Any]]:
    return [row.to_dict() for row in discover_schedule(year, _schedule_rows(body), now=now)]


def schedule_timing_paths(body: bytes, year: int) -> dict[str, str]:
    paths = {}
    for row in _schedule_rows(body):
        sessions = discover_schedule(year, [row], now=datetime(year + 1, 1, 1, tzinfo=UTC))
        for session in sessions:
            local = row[f"Session{session.fastf1_slot}Date"]
            if local is None:
                raise ValueError("schedule timing date is missing")
            value = (
                f"{year}/{row['EventDate']:%Y-%m-%d} {row['EventName']}/"
                f"{local:%Y-%m-%d} {session.provider_session_name}/"
            )
            paths[session.session_id] = value.replace(" ", "_")
    return paths


def _wire_from_root(
    root: Path, relative: str, url: str, reader: _FallbackCacheReader | None = None
) -> dict[str, Any]:
    wire = (
        read_cached_response(_owned_path(root, relative), url)
        if reader is None
        else reader.read(relative, url)
    )
    return {**wire, "cache_path": relative}


def _authoritative_sample(root: Path, entry: dict[str, Any]) -> tuple[Path, dict[str, Any], Path]:
    identity = entry["session_id"]
    directory = _owned_path(root, f"preflight/{identity}")
    published_path = _owned_path(root, f"preflight/{identity}/measurement.json")
    if not published_path.exists():
        raise ValueError("missing exact cached source URL and authoritative sample")
    value = _document(published_path)
    if (
        value.get("session") != entry
        or not value.get("completion_confirmed")
        or value.get("failures")
    ):
        raise ValueError("authoritative native measurement identity differs")
    matches = [
        path.parent
        for path in directory.glob("*/measurement.json")
        if _document(_owned_path(root, path.relative_to(root).as_posix())) == value
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous authoritative native sample attempt")
    return matches[0], value, published_path


def _sample_cache(root: Path, entry: dict[str, Any]) -> str:
    attempt, _, _ = _authoritative_sample(root, entry)
    return (attempt / "raw-cache/fastf1_http_cache.sqlite").relative_to(root).as_posix()


def native_completion_source(
    root: Path, entry: dict[str, Any], source_bytes: bytes
) -> dict[str, Any]:
    """Supplemental native timestamp/sequence corroboration for a captured wire."""
    import pandas as pd  # type: ignore[import-untyped]

    attempt, measurement, published_path = _authoritative_sample(root, entry)
    table_relative = (attempt / "session_status.parquet").relative_to(root).as_posix()
    table = _owned_path(root, table_relative)
    descriptor = measurement.get("tables", {}).get("session_status", {})
    if sha256(table.read_bytes()) != descriptor.get(
        "sha256"
    ) or table.stat().st_size != descriptor.get("bytes"):
        raise ValueError("native completion table checksum differs from published measurement")
    frame = pd.read_parquet(table)
    if (
        not {"Status", "Time", "source_index"}.issubset(frame.columns)
        or len(frame) != descriptor.get("rows")
        or frame["source_index"].tolist() != list(range(len(frame)))
    ):
        raise ValueError("native completion table schema or record order differs")
    observed = native_status_observations(frame, captured=True)
    statuses = observed["statuses"]
    nanos = observed["time_nanoseconds"]
    times = observed["times"]
    source = json.loads(source_bytes)
    if (
        source.get("session_id") != entry["session_id"]
        or source.get("statuses") != statuses
        or measurement.get("session_statuses") != statuses
        or (source.get("times") and source["times"] != times)
    ):
        raise ValueError("native completion sequence or identity differs")
    if (
        classify_completion(entry["completion"], statuses, supported=entry.get("fastf1_supported"))
        != "completed"
    ):
        raise ValueError("native completion is unverified")
    attempt_relative = (attempt / "measurement.json").relative_to(root).as_posix()
    return {
        "native_path": table_relative,
        "native_sha256": descriptor["sha256"],
        "native_bytes": descriptor["bytes"],
        "statuses": statuses,
        "times": times,
        "time_nanoseconds": nanos,
        "published_measurement_path": published_path.relative_to(root).as_posix(),
        "published_measurement_sha256": sha256(published_path.read_bytes()),
        "authoritative_attempt_measurement_path": attempt_relative,
        "authoritative_attempt_measurement_sha256": sha256(
            _owned_path(root, attempt_relative).read_bytes()
        ),
    }


def native_status_observations(frame: Any, *, captured: bool = False) -> dict[str, Any]:
    """Validate native chronology without sorting, rounding or fixing source values."""
    import pandas as pd

    if not {"Time", "Status"}.issubset(frame.columns):
        raise ValueError("native completion timestamp or Status column is missing")
    if captured and (
        "source_index" not in frame.columns
        or frame["source_index"].tolist() != list(range(len(frame)))
    ):
        raise ValueError("native completion source record order differs")
    statuses = frame["Status"].tolist()
    time_values = frame["Time"].tolist()
    if any(type(status) is not str or status not in KNOWN_STATUSES for status in statuses):
        raise ValueError("native completion has unknown status")
    nanos = []
    for value in time_values:
        if not isinstance(value, pd.Timedelta) or pd.isna(value) or value.value < 0:
            raise ValueError("native completion has invalid timestamp")
        nanos.append(value.value)
    for i in range(1, len(nanos)):
        if nanos[i] < nanos[i - 1] or (
            nanos[i] == nanos[i - 1]
            and statuses[i] != statuses[i - 1]
            and statuses[i - 1 : i + 1] != ["Finalised", "Ends"]
        ):
            raise ValueError("native completion timestamps are ambiguous")
    times = [str(value.to_pytimedelta()) for value in time_values]
    return {"statuses": statuses, "times": times, "time_nanoseconds": nanos}


def completion_wire(root: Path, entry: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return _completion_wire(root, entry, source, None)


def _completion_wire(
    root: Path,
    entry: dict[str, Any],
    source: dict[str, Any],
    reader: _FallbackCacheReader | None,
) -> dict[str, Any]:
    matches = []
    urls = _status_urls(entry, source)

    def collect(relative: str) -> None:
        for url in urls:
            try:
                wire = _wire_from_root(root, relative, url, reader)
            except ValueError as exc:
                if str(exc) != "missing exact cached source URL":
                    raise
                continue
            wire["source_origin"] = (
                "official-f1" if url.startswith(OFFICIAL_BASE) else "fastf1-configured-mirror"
            )
            matches.append(wire)

    collect("reconciliation-cache/fastf1_http_cache.sqlite")
    # When the original sample owns another cache, inspect it too: contradictory
    # captures must not be hidden by preferring one host or cache directory.
    if _owned_path(root, f"preflight/{entry['session_id']}/measurement.json").exists():
        collect(_sample_cache(root, entry))
    if not matches:
        raise ValueError("missing exact cached source URL")

    def observations(wire: dict[str, Any]) -> Any:
        parsed = parse_status_stream(bytes.fromhex(wire["body_hex"]))
        return (
            parsed["valid"],
            parsed["issues"],
            [(r["time"], r["value"]) for r in parsed["records"]],
        )

    if any(observations(wire) != observations(matches[0]) for wire in matches[1:]):
        raise ValueError("conflicting captured status observations across exact allowed URLs")
    first = matches[0]
    return {**first, "consistent_captures": matches[1:]}


def build_index_fallback(root: Path, year: int = 2022) -> dict[str, Any]:
    """Prove 2022 identities and completion without pretending to have Index.json."""
    if year != 2022:
        raise ValueError("only the explicitly audited 2022 fallback is supported")
    discovery_path = _owned_path(root, "inventory.json")
    discovery = _document(discovery_path)
    if not discovery.get("complete_inventory"):
        raise ValueError("discovery inventory is incomplete")
    clock = datetime.fromisoformat(discovery["discovered_at"])
    failures = _document(_owned_path(root, "reconciled-inventory.json")).get("index_failures", [])
    if not any(failure.get("year") == year for failure in failures):
        raise ValueError("historical index failure is missing")
    historical = {}
    for path in _owned_path(root, "runtime").glob("official-index-retry-*.json"):
        relative = path.relative_to(root).as_posix()
        document = _document(_owned_path(root, relative))
        if (
            document.get("url") == index_url(year)
            and document.get("state") == "failed"
            and "403" in str(document.get("error", ""))
        ):
            historical[relative] = sha256(path.read_bytes())
    if not historical:
        raise ValueError("captured historical HTTP 403 evidence is required")
    reader = _FallbackCacheReader(root)
    schedule = _wire_from_root(
        root, "discovery-cache/fastf1_http_cache.sqlite", schedule_url(year), reader
    )
    body = bytes.fromhex(schedule["body_hex"])
    discovered = [row for row in discovery["sessions"] if row["season"] == year]
    transformed = schedule_sessions(body, year, now=clock)
    if len(discovered) != 110 or len(transformed) != 110:
        raise ValueError("2022 fallback must prove exactly 110 sessions")
    if discovered != transformed:
        raise ValueError(
            "cached schedule discovery identity, slots, event, support or dates differ"
        )
    paths = schedule_timing_paths(body, year)
    sources = {}
    for entry in discovered:
        identity = entry["session_id"]
        validate_safe_identifier(identity)
        source_path = _owned_path(root, f"completion/{identity}.json")
        source_bytes = source_path.read_bytes()
        source = json.loads(source_bytes)
        if source.get("timing_path") != paths[identity]:
            raise ValueError("cached completion timing identity differs from schedule")
        wire = _completion_wire(root, entry, source, reader)
        derived = derive_completion(entry, source_bytes, wire)
        if derived["completion_integrity"] != "verified" or derived["state"] != "completed":
            raise ValueError(f"completion source is ambiguous or unverified: {identity}")
        sources[identity] = {
            "source_completion_sha256": sha256(source_bytes),
            "wire": wire,
            "parser": derived["parser"],
            "legacy_comparison": derived["legacy_comparison"],
        }
        if not source.get("times"):
            native = native_completion_source(root, entry, source_bytes)
            wire_nanos = [
                (
                    (int(r["time"][:2]) * 3600 + int(r["time"][3:5]) * 60 + int(r["time"][6:8]))
                    * 1000
                    + int(r["time"][9:12])
                )
                * 1_000_000
                for r in derived["parser"]["records"]
            ]
            if native["time_nanoseconds"] != wire_nanos:
                raise ValueError("native completion timestamps differ from captured wire")
            sources[identity]["native"] = native
    reader.verify_unchanged()
    return {
        "schema_version": "historical-index-fallback-v1",
        "year": year,
        "resolution": "historical-index-unavailable/session-identity-and-completion-verified",
        "capabilities": {
            "official_index": "historically-unavailable",
            "session_identity": "verified",
            "session_completion": "verified",
        },
        "discovery_sha256": sha256(discovery_path.read_bytes()),
        "index_failures": failures,
        "historical_403_records": historical,
        "schedule": schedule,
        "matched_session_ids": [row["session_id"] for row in discovered],
        "parser_policy_version": PARSER_POLICY_VERSION,
        "completion_sources": sources,
    }


def validate_index_fallback(root: Path, proof: dict[str, Any]) -> dict[str, Any]:
    year = proof.get("year")
    if type(year) is not int:
        raise ValueError("historical index fallback report year identity differs")
    current = build_index_fallback(root, year)
    if proof != current:
        raise ValueError("historical index fallback is stale or differs from current evidence")
    return current


def publish_proof(root: Path, relative: str, value: dict[str, Any]) -> bool:
    """Publish derived evidence once; incompatible existing proof is never replaced."""
    path = _owned_path(root, relative)
    if path.exists():
        if _document(path) != value:
            raise ValueError("existing derived proof differs; refusing to overwrite")
        return False
    write_text_atomic(path, json.dumps(value, indent=2, allow_nan=False) + "\n")
    return True


def publish_derived_completion(root: Path, proof: dict[str, Any]) -> bool:
    identity = proof["session_id"]
    validate_safe_identifier(identity)
    return publish_proof(root, f"completion-derived/{identity}.json", proof)


def load_derived_completion(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    return _load_derived_completion(root, entry, None)


def _load_derived_completion(
    root: Path, entry: dict[str, Any], reader: _FallbackCacheReader | None
) -> dict[str, Any]:
    identity = entry["session_id"]
    validate_safe_identifier(identity)
    proof = _document(_owned_path(root, f"completion-derived/{identity}.json"))
    source_bytes = _owned_path(root, f"completion/{identity}.json").read_bytes()
    current = derive_completion(
        entry, source_bytes, _completion_wire(root, entry, json.loads(source_bytes), reader)
    )
    if current != proof:
        raise ValueError("derived completion is stale or differs from current source evidence")
    return current


def load_derived_corrections(
    root: Path, entries: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    reader = _FallbackCacheReader(root)
    rows = {entry["session_id"]: entry for entry in entries}
    corrections = {}
    hashes = {}
    for path in _owned_path(root, "completion-derived").glob("*.json"):
        identity = path.stem
        if identity not in rows:
            raise ValueError("derived completion contains an unknown session identity")
        corrections[identity] = _load_derived_completion(root, rows[identity], reader)
        hashes[identity] = sha256(path.read_bytes())
    reader.verify_unchanged()
    return corrections, hashes


def index_failure_resolutions(root: Path, catalog: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """History stays intact; blocking state is recomputed from fully replayed proofs."""
    blocking = []
    resolutions = []
    reports = catalog.get("index_fallback_reports", {})
    for failure in catalog.get("index_failures", []):
        year = failure.get("year")
        descriptor = reports.get(str(year))
        if descriptor is None:
            blocking.append(failure)
            resolutions.append(
                {
                    "year": year,
                    "resolution": "unresolved",
                    "capabilities": {
                        "official_index": "unavailable",
                        "session_identity": "unverified",
                        "session_completion": "unverified",
                    },
                }
            )
            continue
        path = _owned_path(root, descriptor["path"])
        if not path.is_file() or sha256(path.read_bytes()) != descriptor.get("sha256"):
            raise ValueError("historical index fallback proof checksum differs or is missing")
        proof = _document(path)
        if proof.get("year") != year or proof.get("index_failures") != catalog.get(
            "index_failures"
        ):
            raise ValueError("historical index fallback report identity differs")
        validated = validate_index_fallback(root, proof)
        resolutions.append(
            {
                "year": year,
                "resolution": validated["resolution"],
                "capabilities": validated["capabilities"],
                "proof_sha256": descriptor["sha256"],
            }
        )
    return blocking, resolutions


def load_archive_status(root: Path, entry: dict[str, Any]) -> dict[str, Any] | None:
    identity = entry["session_id"]
    validate_safe_identifier(identity)
    path = _owned_path(root, f"archive-finalization/{identity}.json")
    if not path.exists():
        return None
    record = _document(path)
    original_path = _owned_path(root, f"completion/{identity}.json")
    source_bytes = original_path.read_bytes()
    expected = status_url(entry, json.loads(source_bytes)).replace(
        "SessionStatus.jsonStream", "ArchiveStatus.json"
    )
    if record.get("session_id") != identity or record.get("url") != expected:
        raise ValueError("captured archive evidence identity differs")
    if record.get("source_completion_sha256") is not None and record[
        "source_completion_sha256"
    ] != sha256(source_bytes):
        raise ValueError("captured archive evidence source completion differs")
    observed = datetime.fromisoformat(record["observed_at"])
    if observed.tzinfo is None:
        raise ValueError("captured archive observation timestamp is naive")
    body = bytes.fromhex(record["body_hex"])
    if len(body) > MAX_BODY_BYTES or sha256(body) != record.get("body_sha256"):
        raise ValueError("captured archive evidence checksum differs")
    if record.get("http_status") == 404:
        return None
    if record.get("http_status") != 200:
        raise ValueError("captured archive completion requires HTTP 200")
    value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if type(value) is not dict:
        raise ValueError("captured archive evidence must be an object")
    return value
