"""Bounded public historical enrichment, separate from primary car/position traces."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from uuid import uuid4

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.contracts import parse_interval
from downforce_core.detailed.enrichment_paths import (
    EVIDENCE_PATH,
    default_evidence_path,
    evidence_directory,
    evidence_path,
    reverified_evidence_path,
)
from downforce_core.detailed.openf1_transport import (
    ENDPOINTS as ENDPOINTS,
)
from downforce_core.detailed.openf1_transport import (
    MAX_RESPONSE_BYTES as MAX_RESPONSE_BYTES,
)
from downforce_core.detailed.openf1_transport import (
    OpenF1Client as OpenF1Client,
)
from downforce_core.detailed.openf1_transport import (
    historical_url,
)
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.detailed.status import (
    CapacityStop,
    guard_operation,
    owned_temporary_directory,
    parquet_peak_bytes,
)
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256


def encode_response(body: bytes) -> bytes:
    return gzip.compress(body, compresslevel=6, mtime=0)


def enrichment_table(rows: list[dict[str, Any]]) -> pa.Table:
    keys = sorted({key for row in rows for key in row})
    arrays = {}
    for key in keys:
        values = [row.get(key) for row in rows]
        if key in ("interval", "gap_to_leader"):
            intervals = [parse_interval(value) for value in values]
            arrays[key + "_kind"] = pa.array([value.kind for value in intervals])
            arrays[key + "_seconds"] = pa.array(
                [value.value if value.kind == "seconds" else None for value in intervals],
                type=pa.float64(),
            )
            arrays[key + "_laps_down"] = pa.array(
                [
                    int(value.value)
                    if value.kind == "laps-down" and value.value is not None
                    else None
                    for value in intervals
                ],
                type=pa.int64(),
            )
            arrays[key + "_provider_json"] = pa.array([json.dumps(value) for value in values])
        else:
            arrays[key] = pa.array(values)
    return pa.table(arrays)


class EnrichmentClient(Protocol):
    def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]: ...


def _read(path: Path, root: Path) -> dict[str, Any]:
    _unlinked_path(path)
    value: dict[str, Any] = json.loads(ensure_contained(path, root).read_text())
    if not isinstance(value, dict):
        raise ValueError("enrichment metadata must be a JSON object")
    return value


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def _authority_now() -> datetime:
    return datetime.now(UTC)


def validated_historical_session(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    """Replay current canonical completion, selection, and native candidate proof."""
    from downforce_core.detailed.proofs import _candidate
    from downforce_core.detailed.study_io import validated_frozen_plan

    root = _unlinked_path(root)
    identity = validate_safe_identifier(session["session_id"])
    catalog, originals, plan = validated_frozen_plan(root)
    entries = [entry for entry in catalog["sessions"] if entry["session_id"] == identity]
    if len(entries) != 1 or not _same(entries[0], session):
        raise ValueError("enrichment session differs from current canonical identity")
    entry: dict[str, Any] = entries[0]
    if entry["state"] != "completed" or type(entry["season"]) is not int or entry["season"] < 2023:
        raise ValueError("enrichment measurement requires a completed supported-era session")
    key = entry.get("openf1_session_key")
    if type(key) is not int or key <= 0:
        raise ValueError("enrichment requires current reconciled provider identity")
    selected = plan.get("selected_sessions")
    if (
        plan.get("schema_version") != "detailed-sample-plan-v1"
        or plan.get("selected_before_download") is not True
        or not isinstance(selected, list)
        or len(
            [row for row in selected if isinstance(row, dict) and row.get("session_id") == identity]
        )
        != 1
        or not any(_same(row, entry) for row in selected)
    ):
        raise ValueError("enrichment requires unchanged frozen canonical selection")
    provider_rows = _read(root / "openf1-inventory.json", root).get("sessions")
    if not isinstance(provider_rows, list) or not all(
        isinstance(row, dict) for row in provider_rows
    ):
        raise ValueError("invalid provider source inventory")
    sources = [
        row
        for row in provider_rows
        if type(row.get("session_key")) is int and row["session_key"] == key
    ]
    mappings = [
        row
        for row in catalog["provider_entries"]
        if row.get("session_key") == key
        and row.get("canonical_session_id") == identity
        and row.get("reconciliation_state") == "completed"
    ]
    if len(sources) != 1 or len(mappings) != 1:
        raise ValueError("enrichment requires unique bound source and canonical provider mapping")
    provider = sources[0]
    if any(not _same(mappings[0].get(field), value) for field, value in provider.items()):
        raise ValueError("provider source differs from canonical bound mapping")
    try:
        start = datetime.fromisoformat(provider["date_start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(provider["date_end"].replace("Z", "+00:00"))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("bound historical provider start/end evidence is required") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("historical provider dates are naive or inconsistent")
    from downforce_core.detailed.enrichment_authority import _historical_end

    end = _historical_end(root, entry, provider)
    if start.year != entry["season"] or end.timestamp() + 1800 >= datetime.now(UTC).timestamp():
        raise ValueError("provider session is future or within the paid/live data window")
    scheduled = datetime.fromisoformat(entry["scheduled_at"])
    if abs((scheduled - start).total_seconds()) > 86400:
        raise ValueError("bound provider date conflicts with canonical schedule")
    report = _read(root / "study/session-measurements" / f"{identity}.json", root)
    _candidate(root, report, entry, originals[identity])
    return entry


def _save(path: Path, value: dict[str, Any], check: Any) -> None:
    check()
    _unlinked_path(path)
    encoded = json.dumps(value, indent=2, allow_nan=False)
    guard_operation(
        path,
        next_peak_bytes=2 * len(encoded.encode()) + 4096,
        operation="OpenF1 metadata publication",
    )
    write_text_atomic(path, encoded)


def _wire(directory: Path, endpoint: str, body: bytes, check: Any) -> dict[str, Any]:
    check()
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized enrichment wire evidence")
    digest = hashlib.sha256(body).hexdigest()
    raw = directory / f"{endpoint}-{digest}.json.gz"
    _unlinked_path(raw)
    guard_operation(raw, next_peak_bytes=2 * len(body) + 1024**2, operation="OpenF1 gzip evidence")
    compressed = encode_response(body)
    if gzip.decompress(compressed) != body:
        raise ValueError("HTTP evidence round trip changed response bytes")
    if raw.exists():
        if raw.read_bytes() != compressed:
            raise ValueError("existing raw response evidence is corrupt")
    else:
        guard_operation(
            raw, next_peak_bytes=len(compressed) + 8192, operation="OpenF1 wire publication"
        )
        with owned_temporary_directory(prefix=".source-", directory=directory) as temporary:
            staging = temporary / "response.gz"
            staging.write_bytes(compressed)
            check()
            os.rename(staging, raw)
    return {
        "response_sha256": digest,
        "raw_file": raw.name,
        "raw_file_sha256": file_sha256(raw),
        "raw_bytes": raw.stat().st_size,
        "source_body_bytes": len(body),
    }


def _capture_attempt(
    directory: Path,
    endpoint: str,
    session: dict[str, Any],
    body: bytes,
    provenance: dict[str, Any],
    check: Any,
) -> None:
    references, _, _ = validate_attempts(directory, session)
    record = {
        **provenance,
        **_wire(directory, endpoint, body, check),
        "session_id": session["session_id"],
        "session_key": session["openf1_session_key"],
        "endpoint": endpoint,
    }
    name = f"{endpoint}-attempt-{uuid4().hex}.json"
    _save(directory / name, record, check)
    references[name] = file_sha256(directory / name)
    _save(
        directory / "attempts.json",
        {"schema_version": "openf1-attempts-v1", "attempts": references},
        check,
    )


def _utc(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("enrichment provenance UTC time is required")
    parsed = datetime.fromisoformat(value)
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("enrichment provenance must use UTC")


def validate_attempts(
    directory: Path, session: dict[str, Any], references: dict[str, str] | None = None
) -> tuple[dict[str, str], set[str], int]:
    """Allow only explicitly hash-bound attempts and original bounded wire bodies."""
    paths = list(directory.glob("*-attempt-*.json"))
    index_path = directory / "attempts.json"
    index: dict[str, Any] = {"attempts": {}}
    if index_path.exists():
        index = _read(index_path, directory)
        if index.get("schema_version") != "openf1-attempts-v1" or set(index) != {
            "schema_version",
            "attempts",
        }:
            raise ValueError("enrichment attempt index schema differs")
    indexed = index.get("attempts")
    if not isinstance(indexed, dict) or set(indexed) != {path.name for path in paths}:
        raise ValueError("enrichment attempt manifest inventory differs")
    if references is not None and not _same(indexed, references):
        raise ValueError("enrichment attempt manifest inventory differs")
    bindings: dict[str, str] = {}
    names: set[str] = {"attempts.json"} if index_path.exists() else set()
    for path in paths:
        record = _read(path, directory)
        digest = file_sha256(path)
        if indexed[path.name] != digest:
            raise ValueError("enrichment attempt metadata checksum mismatch")
        endpoint = record.get("endpoint")
        if endpoint not in ENDPOINTS or not path.name.startswith(f"{endpoint}-attempt-"):
            raise ValueError("enrichment attempt endpoint identity differs")
        expected = {
            "session_id": session["session_id"],
            "session_key": session["openf1_session_key"],
            "url": historical_url(endpoint, session["openf1_session_key"]),
        }
        if any(not _same(record.get(key), value) for key, value in expected.items()):
            raise ValueError("enrichment attempt session or URL differs")
        if (
            not isinstance(record.get("actual_url"), str)
            or type(record.get("http_status")) is not int
            or not 100 <= record["http_status"] <= 599
        ):
            raise ValueError("invalid enrichment attempt response provenance")
        headers = record.get("headers")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise ValueError("invalid enrichment attempt headers")
        if sum(len(key.encode()) + len(value.encode()) for key, value in headers.items()) > 65536:
            raise ValueError("oversized enrichment attempt headers")
        fields = record.get("header_fields")
        if not isinstance(fields, list) or not all(
            isinstance(field, list)
            and len(field) == 2
            and all(isinstance(part, str) for part in field)
            for field in fields
        ):
            raise ValueError("invalid ordered enrichment response headers")
        encoded_headers = json.dumps(fields, ensure_ascii=False).encode()
        if (
            len(encoded_headers) > 65536
            or dict(fields) != headers
            or hashlib.sha256(encoded_headers).hexdigest() != record.get("header_fields_sha256")
        ):
            raise ValueError("enrichment original header identity differs")
        for flag in ("headers_truncated", "body_truncated"):
            if type(record.get(flag)) is not bool:
                raise ValueError("invalid bounded enrichment attempt flags")
        _utc(record.get("observed_at"))
        body = _validate_wire(directory, endpoint, record)
        if record["body_truncated"] and len(body) != MAX_RESPONSE_BYTES:
            raise ValueError("truncated enrichment prefix has inconsistent bound")
        bindings[path.name] = digest
        names.update((path.name, record["raw_file"]))
    return bindings, names, sum((directory / name).stat().st_size for name in names)


def _validate_wire(directory: Path, endpoint: str, record: dict[str, Any]) -> bytes:
    raw = ensure_contained(_unlinked_path(directory / record["raw_file"]), directory)
    if (
        raw.name != f"{endpoint}-{record['response_sha256']}.json.gz"
        or file_sha256(raw) != record["raw_file_sha256"]
    ):
        raise ValueError("cached enrichment wire evidence is corrupt")
    with gzip.open(raw, "rb") as handle:
        body = handle.read(MAX_RESPONSE_BYTES + 1)
    if (
        len(body) > MAX_RESPONSE_BYTES
        or hashlib.sha256(body).hexdigest() != record["response_sha256"]
    ):
        raise ValueError("cached response byte identity changed")
    for field, actual in (("raw_bytes", raw.stat().st_size), ("source_body_bytes", len(body))):
        if field in record and (type(record[field]) is not int or record[field] != actual):
            raise ValueError("cached wire byte metadata differs")
    return body


def _source(
    directory: Path, endpoint: str, session_key: int
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    source = _read(directory / f"{endpoint}-source.json", directory)
    if (
        source.get("url") != historical_url(endpoint, session_key)
        or type(source.get("http_status")) is not int
        or source["http_status"] not in (200, 404)
    ):
        raise ValueError("cached HTTP source URL/status identity differs")
    body = _validate_wire(directory, endpoint, source)
    references, _, _ = validate_attempts(
        directory, {"session_id": directory.name, "openf1_session_key": session_key}
    )
    endpoint_attempts = [
        _read(directory / name, directory)
        for name in references
        if name.startswith(f"{endpoint}-attempt-")
    ]
    if endpoint_attempts:
        fields = ("url", "response_sha256", "raw_file", "raw_file_sha256", "http_status")
        eligible = [
            record
            for record in endpoint_attempts
            if record["http_status"] in (200, 404)
            and record["actual_url"] == source["url"]
            and record["body_truncated"] is False
        ]
        identities = {tuple(record[field] for field in fields) for record in eligible}
        if len(identities) != 1 or tuple(source.get(field) for field in fields) not in identities:
            raise ValueError("cached HTTP source differs from its original captured attempt")
    rows = json.loads(body) if source["http_status"] == 200 else []
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
        or any(
            type(row.get("session_key")) is not int or row["session_key"] != session_key
            for row in rows
        )
    ):
        raise ValueError("unexpected OpenF1 response or mismatched session identity")
    return source, body, rows


def validate_endpoint(
    directory: Path, endpoint: str, session_key: int
) -> tuple[dict[str, Any], set[str]]:
    source, body, rows = _source(directory, endpoint, session_key)
    record = _read(directory / f"{endpoint}.json", directory)
    canonical = ensure_contained(_unlinked_path(directory / record["canonical_file"]), directory)
    expected = {
        **source,
        "source_body_bytes": len(body),
        "raw_bytes": (directory / source["raw_file"]).stat().st_size,
        "canonical_file": f"{endpoint}-{source['response_sha256']}.parquet",
        "canonical_file_sha256": file_sha256(canonical),
        "canonical_bytes": canonical.stat().st_size,
        "rows": len(rows),
        "availability": "unavailable"
        if source["http_status"] == 404
        else "available"
        if rows
        else "empty-provider-response",
    }
    if any(not _same(record.get(key), value) for key, value in expected.items()):
        raise ValueError("cached enrichment endpoint metadata differs")
    _utc(record.get("observed_at"))
    with pq.ParquetFile(canonical) as reader:
        if not reader.read().equals(enrichment_table(rows), check_metadata=True):
            raise ValueError("cached enrichment native-schema or semantic identity differs")
    return record, {
        source["raw_file"],
        canonical.name,
        f"{endpoint}.json",
        f"{endpoint}-source.json",
    }


def _recover_source(directory: Path, endpoint: str, session_key: int, check: Any) -> None:
    """Recover complete HTTP evidence if only its small source pointer was interrupted."""
    candidates = []
    for path in directory.glob(f"{endpoint}-attempt-*.json"):
        record = _read(path, directory)
        if (
            record["http_status"] in (200, 404)
            and record["actual_url"] == historical_url(endpoint, session_key)
            and record["body_truncated"] is False
        ):
            candidates.append(record)
    if not candidates:
        return
    identities = {(record["response_sha256"], record["http_status"]) for record in candidates}
    if len(identities) != 1:
        raise ValueError("interrupted HTTP source has conflicting original attempt evidence")
    record = candidates[0]
    _save(
        directory / f"{endpoint}-source.json",
        {
            key: record[key]
            for key in ("url", "response_sha256", "raw_file", "raw_file_sha256", "http_status")
        },
        check,
    )


def _partial_inventory(directory: Path, session: dict[str, Any]) -> None:
    """Reject unrelated or inconsistent interrupted artifacts before more work."""
    _, names, _ = validate_attempts(directory, session)
    key = session["openf1_session_key"]
    for endpoint in ENDPOINTS:
        record_path = directory / f"{endpoint}.json"
        source_path = directory / f"{endpoint}-source.json"
        if record_path.exists():
            _, endpoint_names = validate_endpoint(directory, endpoint, key)
            names.update(endpoint_names)
        elif source_path.exists():
            source, _, rows = _source(directory, endpoint, key)
            names.update((source_path.name, source["raw_file"]))
            canonical = _unlinked_path(
                directory / f"{endpoint}-{source['response_sha256']}.parquet"
            )
            if canonical.exists():
                with pq.ParquetFile(canonical) as reader:
                    if not reader.read().equals(enrichment_table(rows), check_metadata=True):
                        raise ValueError("interrupted enrichment native schema or values differ")
                names.add(canonical.name)
    if {path.name for path in directory.iterdir()} != names:
        raise ValueError("unmanifested enrichment partial artifacts")


@contextmanager
def _client_scope(
    root: Path, session: dict[str, Any], client: EnrichmentClient, directory: Path, check: Any
) -> Iterator[None]:
    if not isinstance(client, OpenF1Client):
        yield
        return
    if client.root != root.absolute():
        raise ValueError("OpenF1 client belongs to a different data root")
    client.check()

    def observe(body: bytes, provenance: dict[str, Any]) -> None:
        endpoint = provenance["url"].split("/v1/", 1)[1].split("?", 1)[0]
        _capture_attempt(directory, endpoint, session, body, provenance, check)

    if client.active:
        with client._historical_scope(session["openf1_session_key"], check, observe):
            yield
    else:
        with client, client._historical_scope(session["openf1_session_key"], check, observe):
            yield


def measure_enrichment(
    root: Path, session: dict[str, Any], client: EnrichmentClient
) -> dict[str, Any]:
    if isinstance(client, OpenF1Client):
        client.check()
    try:
        return _measure_operation(root, session, client)
    except CapacityStop as exc:
        if isinstance(client, OpenF1Client):
            client._failure = exc
        raise


def refresh_enrichment(root: Path, session: dict[str, Any], client: OpenF1Client) -> dict[str, Any]:
    """Explicit native recapture into a new owned leaf; never rewrite old evidence.

    Candidate publication is separate. The returned relative marker may be
    published only after this complete operation and its fresh final validation.
    """
    if not isinstance(client, OpenF1Client):
        raise ValueError("explicit enrichment refresh requires an owned OpenF1Client")
    client.check()
    try:
        return _measure_operation(
            root, session, client, _evidence_path=reverified_evidence_path(session)
        )
    except CapacityStop as exc:
        client._failure = exc
        raise


def _measure_operation(
    root: Path,
    session: dict[str, Any],
    client: EnrichmentClient,
    *,
    _evidence_path: str | None = None,
) -> dict[str, Any]:
    from downforce_core.detailed.enrichment_authority import _Admission

    root = _unlinked_path(root)
    identity = validate_safe_identifier(session["session_id"])
    candidate_path = root / "study/session-measurements" / f"{identity}.json"
    candidate = _read(candidate_path, root)
    relative = evidence_path(session, candidate) if _evidence_path is None else _evidence_path
    directory = evidence_directory(root, session, relative)
    if (
        _evidence_path is None
        and relative == reverified_evidence_path(session)
        and not _unlinked_path(directory / "measurement.json").is_file()
    ):
        raise ValueError("bound reverified enrichment proof is missing or incomplete")
    preservation = (
        (default_evidence_path(session),) if relative == reverified_evidence_path(session) else ()
    )
    with _Admission(root, session, preservation_inputs=preservation) as admission:
        if (
            _evidence_path is None
            and evidence_path(session, _read(candidate_path, root)) != relative
        ):
            raise ValueError("candidate enrichment selection changed during historical admission")
        if isinstance(client, OpenF1Client):
            client.check()

        def check() -> None:
            admission.check()

        guard_operation(directory, next_peak_bytes=4096, operation="OpenF1 measurement directory")
        directory.mkdir(parents=True, exist_ok=True)
        with _client_scope(root, session, client, directory, check):
            result = _measure_enrichment(root, session, client, check, directory, relative)
            if relative == reverified_evidence_path(session):
                from downforce_core.detailed.proofs import _enrichment

                _enrichment(root, session, endpoints=ENDPOINTS, _evidence_path=relative)
            admission.finish()
            return result


def _measure_enrichment(
    root: Path,
    session: dict[str, Any],
    client: EnrichmentClient,
    check: Any,
    directory: Path,
    relative: str,
) -> dict[str, Any]:
    identity = validate_safe_identifier(session["session_id"])
    if session["state"] != "completed" or session["season"] < 2023:
        raise ValueError("enrichment measurement requires a completed supported-era session")
    session_key = session["openf1_session_key"]
    if type(session_key) is not int:
        raise ValueError("enrichment requires reconciled provider identity")
    directory.mkdir(parents=True, exist_ok=True)
    ensure_contained(directory, root)
    measurement_path = directory / "measurement.json"
    if measurement_path.exists():
        from downforce_core.detailed.proofs import _enrichment

        _enrichment(root, session, endpoints=ENDPOINTS, _evidence_path=relative)
        check()
        return _read(measurement_path, directory)
    _partial_inventory(directory, session)
    records = {}
    for endpoint in ENDPOINTS:
        record_path = directory / f"{endpoint}.json"
        if record_path.exists():
            record, _ = validate_endpoint(directory, endpoint, session_key)
            records[endpoint] = record
            continue
        source_path = directory / f"{endpoint}-source.json"
        if not source_path.exists():
            _recover_source(directory, endpoint, session_key, check)
        http_status = 200
        if source_path.exists():
            source_record, body, _ = _source(directory, endpoint, session_key)
            url = source_record["url"]
            http_status = source_record["http_status"]
        else:
            check()
            guard_operation(
                directory,
                next_peak_bytes=2 * MAX_RESPONSE_BYTES + 1024**2,
                operation="OpenF1 enrichment provider request",
            )
            try:
                body, url = client.fetch(endpoint, session_key)
            except HTTPError as exc:
                if exc.code != 404:
                    raise  # permission/authentication failures are explicit stop conditions
                if isinstance(client, OpenF1Client):
                    client.check()  # Only a clean captured/closed 404 is availability evidence.
                body, url, http_status = exc.read(MAX_RESPONSE_BYTES + 1), exc.url, 404
                exc.close()
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized unavailable-endpoint response") from exc
        if url != historical_url(endpoint, session_key):
            raise ValueError("OpenF1 response URL identity differs")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("oversized enrichment response")
        rows = json.loads(body) if http_status == 200 else []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("unexpected OpenF1 response")
        if any(
            type(row.get("session_key")) is not int or row["session_key"] != session_key
            for row in rows
        ):
            raise ValueError("enrichment response has mismatched session identity")
        wire = _wire(directory, endpoint, body, check)
        digest = wire["response_sha256"]
        raw = directory / wire["raw_file"]
        if not source_path.exists():
            _save(
                source_path,
                {
                    "url": url,
                    "response_sha256": digest,
                    "raw_file": raw.name,
                    "raw_file_sha256": file_sha256(raw),
                    "http_status": http_status,
                },
                check,
            )
        table = enrichment_table(rows)
        canonical = directory / f"{endpoint}-{digest}.parquet"
        _unlinked_path(canonical)
        if not canonical.exists():
            check()
            guard_operation(
                canonical,
                next_peak_bytes=parquet_peak_bytes(
                    table, source_bytes=len(body), row_group_size=16384
                ),
                operation="OpenF1 Parquet publication",
            )
            with owned_temporary_directory(prefix=".columnar-", directory=directory) as temporary:
                staged = temporary / "response.parquet"
                pq.write_table(
                    table,
                    staged,
                    compression="zstd",
                    compression_level=6,
                    row_group_size=16384,
                    use_dictionary=False,
                    use_compliant_nested_type=False,
                )
                # Own and close verification handles before Windows publication;
                # anonymous dataset readers can outlive their read_table call.
                with pq.ParquetFile(staged) as reader:
                    if not reader.read().equals(table, check_metadata=True):
                        raise ValueError("enrichment staging changed observed values")
                check()
                os.rename(staged, canonical)
        with pq.ParquetFile(canonical) as reader:
            if not reader.read().equals(table, check_metadata=True):
                raise ValueError("enrichment columnar round trip changed values")
        record = {
            "url": url,
            "response_sha256": digest,
            "source_body_bytes": len(body),
            "http_status": http_status,
            "availability": "unavailable"
            if http_status == 404
            else "available"
            if rows
            else "empty-provider-response",
            "raw_file": raw.name,
            "raw_file_sha256": file_sha256(raw),
            "raw_bytes": raw.stat().st_size,
            "canonical_file": canonical.name,
            "canonical_file_sha256": file_sha256(canonical),
            "canonical_bytes": canonical.stat().st_size,
            "rows": len(rows),
            "observed_at": datetime.now(UTC).isoformat(),
        }
        _save(record_path, record, check)
        records[endpoint] = record
        print(f"OpenF1 {identity} {endpoint}: {len(rows)} observations", flush=True)
    attempts, attempt_files, _ = validate_attempts(directory, session)
    wire_files = {record["raw_file"] for record in records.values()} | {
        name for name in attempt_files if name.endswith(".gz")
    }
    allowed = attempt_files | {
        name
        for endpoint, record in records.items()
        for name in (
            f"{endpoint}.json",
            f"{endpoint}-source.json",
            record["raw_file"],
            record["canonical_file"],
        )
    }
    if {path.name for path in directory.iterdir()} != allowed:
        raise ValueError("unmanifested enrichment artifacts")
    metadata = sum((directory / name).stat().st_size for name in allowed if name.endswith(".json"))
    report = {
        "session_id": identity,
        "session_key": session_key,
        "endpoints": records,
        "openf1_measured": True,
        "radio_audio_downloaded": False,
        "overlapping_car_position_duplicated": False,
        "openf1_raw_bytes": sum((directory / name).stat().st_size for name in wire_files)
        + metadata,
        "openf1_canonical_bytes": sum(record["canonical_bytes"] for record in records.values()),
    }
    if relative == reverified_evidence_path(session):
        report[EVIDENCE_PATH] = relative
    if attempts:
        report["attempts"] = attempts
        report["attempts_index_sha256"] = file_sha256(directory / "attempts.json")
    _save(measurement_path, report, check)
    return report
