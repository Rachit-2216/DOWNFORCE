"""Production additive historical OpenF1 corpus, independent of frozen samples."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.corpus_gate import CorpusAuthorityStop, CorpusGate, select_sessions
from downforce_core.detailed.corpus_openf1_authority import ProductionAuthority
from downforce_core.detailed.corpus_openf1_transport import (
    CorpusOpenF1Client as CorpusOpenF1Client,
)
from downforce_core.detailed.corpus_openf1_transport import HourlyBudgetStop as HourlyBudgetStop
from downforce_core.detailed.openf1 import (
    _capture_attempt,
    _read,
    _recover_source,
    _same,
    _save,
    _source,
    _utc,
    _validate_wire,
    validate_attempts,
)
from downforce_core.detailed.openf1_transport import ENDPOINTS
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.detailed.status import (
    CapacityStop,
    guard_operation,
    owned_temporary_directory,
    parquet_peak_bytes,
)
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256

_ENCODING = b"downforce-openf1-lossless-v1"


class OptionalEndpointFailure(ValueError):
    """An authenticated optional provider response contained no usable rows."""


def _provider_values(value: Any) -> None:
    """Reject only malformed provider values after wire and identity validation."""
    if isinstance(value, float) and not math.isfinite(value):
        raise OptionalEndpointFailure("original optional provider JSON contains nonfinite values")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OptionalEndpointFailure(
                "original optional provider JSON has illformed Unicode"
            ) from exc
    elif isinstance(value, dict):
        for key, cell in value.items():
            _provider_values(key)
            _provider_values(cell)
    elif isinstance(value, list):
        for cell in value:
            _provider_values(cell)


def _provider_source(
    directory: Path, endpoint: str, key: int
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    try:
        source, body, rows = _source(directory, endpoint, key)
        _provider_values(rows)
        return source, body, rows
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OptionalEndpointFailure("malformed original optional provider JSON") from exc
    except ValueError as exc:
        if str(exc) != "unexpected OpenF1 response or mismatched session identity":
            raise
        source = _read(directory / f"{endpoint}-source.json", directory)
        rows = json.loads(_validate_wire(directory, endpoint, source))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise OptionalEndpointFailure("optional provider response has no row array") from exc
        raise CorpusAuthorityStop(
            "provider response conflicts with canonical session identity"
        ) from exc


def lossless_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Explicit JSON fallback plus independent presence masks for every source field.

    Even apparently homogeneous nested numbers may exceed Arrow integers or mix
    types in later rows. JSON cells avoid coercion; metadata labels this encoding.
    Masks live in schema metadata so no invented column can collide with providers.
    """
    keys = sorted({key for row in rows for key in row})
    presence = {key: [key in row for row in rows] for key in keys}
    arrays = {
        key: pa.array(
            [
                json.dumps(row[key], ensure_ascii=False, allow_nan=False) if key in row else None
                for row in rows
            ],
            type=pa.string(),
        )
        for key in keys
    }
    return pa.table(arrays).replace_schema_metadata(
        {
            b"encoding": _ENCODING,
            b"presence": json.dumps(presence, sort_keys=True, allow_nan=False).encode(),
            b"rows": str(len(rows)).encode(),
        }
    )


def restore_rows(table: pa.Table) -> list[dict[str, Any]]:
    metadata = table.schema.metadata or {}
    if metadata.get(b"encoding") != _ENCODING:
        raise ValueError("unknown production OpenF1 field encoding")
    presence = json.loads(metadata[b"presence"])
    count = int(metadata[b"rows"])
    if count != table.num_rows or set(presence) != set(table.column_names):
        raise ValueError("lossless presence schema differs")
    rows: list[dict[str, Any]] = [{} for _ in range(count)]
    for key in table.column_names:
        mask = presence[key]
        if (
            not isinstance(mask, list)
            or len(mask) != count
            or any(type(v) is not bool for v in mask)
        ):
            raise ValueError("invalid provider field presence mask")
        for index, encoded in enumerate(table[key].to_pylist()):
            if mask[index]:
                if not isinstance(encoded, str):
                    raise ValueError("present provider cell lost its original JSON")
                rows[index][key] = json.loads(encoded)
            elif encoded is not None:
                raise ValueError("absent provider cell gained a value")
    return rows


def _validate_endpoint(directory: Path, endpoint: str, key: int) -> tuple[dict[str, Any], set[str]]:
    source, body, rows = _provider_source(directory, endpoint, key)
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
    if any(not _same(record.get(name), value) for name, value in expected.items()):
        raise ValueError("production endpoint metadata differs from original wire")
    _utc(record.get("observed_at"))
    with pq.ParquetFile(canonical) as reader:
        restored = reader.read()
    if not restored.equals(lossless_table(rows), check_metadata=True) or not _same(
        restore_rows(restored), rows
    ):
        raise ValueError("production endpoint schema/presence/values changed")
    return record, {
        source["raw_file"],
        canonical.name,
        f"{endpoint}.json",
        f"{endpoint}-source.json",
    }


def _inventory(directory: Path, authority: ProductionAuthority) -> tuple[dict[str, Any], set[str]]:
    references, names, _ = validate_attempts(directory, authority.entry)
    for name in references:
        attempt = _read(directory / name, directory)
        if attempt["http_status"] in (401, 403):
            raise PermissionError(
                f"persisted OpenF1 permission denial: HTTP {attempt['http_status']}"
            )
        if (
            attempt["actual_url"] != attempt["url"]
            or attempt["body_truncated"]
            or attempt["headers_truncated"]
        ):
            raise CorpusAuthorityStop("persisted transport identity or bounded capture differs")
    binding = _read(directory / "binding.json", directory)
    authority.validate_binding(binding)
    names.add("binding.json")
    records = {}
    for endpoint in ENDPOINTS:
        if (directory / f"{endpoint}.json").exists():
            record, members = _validate_endpoint(directory, endpoint, authority.key)
            records[endpoint] = record
            names.update(members)
        elif (directory / f"{endpoint}-source.json").exists():
            try:
                source, _, rows = _provider_source(directory, endpoint, authority.key)
            except OptionalEndpointFailure:
                if not (directory / f"{endpoint}-failure.json").exists():
                    raise
                source = _read(directory / f"{endpoint}-source.json", directory)
                _validate_wire(directory, endpoint, source)
                rows = []
            names.update((f"{endpoint}-source.json", source["raw_file"]))
            canonical = _unlinked_path(
                directory / f"{endpoint}-{source['response_sha256']}.parquet"
            )
            if canonical.exists():
                with pq.ParquetFile(canonical) as reader:
                    if not reader.read().equals(lossless_table(rows), check_metadata=True):
                        raise ValueError("interrupted production endpoint values changed")
                names.add(canonical.name)
        failure = directory / f"{endpoint}-failure.json"
        if failure.exists():
            failure_record = _read(failure, directory)
            if (
                failure_record.get("endpoint") != endpoint
                or failure_record.get("session_id") != authority.identity
            ):
                raise ValueError("optional endpoint failure identity differs")
            names.add(failure.name)
    if (directory / "manifest.json").exists():
        names.add("manifest.json")
    if {path.name for path in directory.iterdir()} != names:
        raise ValueError("unmanifested production OpenF1 artifacts")
    return records, names


def _publish_endpoint(
    directory: Path, endpoint: str, authority: ProductionAuthority
) -> dict[str, Any]:
    source, body, rows = _provider_source(directory, endpoint, authority.key)
    table = lossless_table(rows)
    canonical = _unlinked_path(directory / f"{endpoint}-{source['response_sha256']}.parquet")
    if not canonical.exists():
        authority.check()
        guard_operation(
            canonical,
            next_peak_bytes=parquet_peak_bytes(table, source_bytes=len(body)),
            operation="production OpenF1 Parquet",
        )
        with owned_temporary_directory(prefix=".columnar-", directory=directory) as temporary:
            staged = temporary / "response.parquet"
            pq.write_table(
                table, staged, compression="zstd", compression_level=6, use_dictionary=False
            )
            with pq.ParquetFile(staged) as reader:
                if not reader.read().equals(table, check_metadata=True):
                    raise ValueError("production columnar staging changed fields")
            authority.check()
            os.rename(staged, canonical)
    references, _, _ = validate_attempts(directory, authority.entry)
    attempts = [
        _read(directory / name, directory)
        for name in references
        if name.startswith(endpoint + "-attempt-")
    ]
    eligible = [record for record in attempts if record["http_status"] in (200, 404)]
    if not eligible:
        raise ValueError("production endpoint lacks authentic transport observation")
    record = {
        **source,
        "source_body_bytes": len(body),
        "raw_bytes": (directory / source["raw_file"]).stat().st_size,
        "canonical_file": canonical.name,
        "canonical_file_sha256": file_sha256(canonical),
        "canonical_bytes": canonical.stat().st_size,
        "rows": len(rows),
        "availability": "unavailable"
        if source["http_status"] == 404
        else "available"
        if rows
        else "empty-provider-response",
        "observed_at": eligible[0]["observed_at"],
    }
    _save(directory / f"{endpoint}.json", record, authority.check)
    _validate_endpoint(directory, endpoint, authority.key)
    return record


def _session(
    project: Path, gate: CorpusGate, entry: dict[str, Any], client: CorpusOpenF1Client
) -> tuple[dict[str, Any], bool]:
    try:
        authority = ProductionAuthority(project, gate, entry)
    except (PermissionError, CapacityStop, CorpusAuthorityStop):
        raise
    except Exception as exc:
        raise CorpusAuthorityStop(
            "current native or provider admission authority is invalid"
        ) from exc
    directory = _unlinked_path(gate.paths.root / "openf1" / authority.identity)
    if not directory.exists():
        authority.check()
        guard_operation(directory, next_peak_bytes=65536, operation="production OpenF1 leaf")
        directory.mkdir(parents=True)
        _save(directory / "binding.json", authority.binding, authority.check)
    try:
        records, _ = _inventory(directory, authority)
    except PermissionError:
        raise
    except Exception as exc:
        raise CorpusAuthorityStop(
            "production resume artifacts or native authority are corrupt"
        ) from exc
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return _complete_manifest(directory, authority, records), True

    def observe(body: bytes, provenance: dict[str, Any]) -> None:
        endpoint = provenance["url"].split("/v1/", 1)[1].split("?", 1)[0]
        # Preserve the actual wire before checking post-send native pins. These
        # immutable forensic attempts never activate an enrichment manifest.
        _capture_attempt(directory, endpoint, entry, body, provenance, lambda: gate.check(entry))
        if provenance["headers_truncated"]:
            raise CorpusAuthorityStop("bounded original response headers were truncated")
        authority.check()

    failures = {}
    with client._historical_scope(authority.key, authority.check, observe):
        for endpoint in ENDPOINTS:
            authority.check()
            if endpoint in records:
                continue
            failure_path = directory / f"{endpoint}-failure.json"
            if failure_path.exists():
                failures[endpoint] = _read(failure_path, directory)
                continue
            try:
                _recover_source(directory, endpoint, authority.key, authority.check)
                if not (directory / f"{endpoint}-source.json").exists():
                    try:
                        client.fetch(endpoint, authority.key)
                    except HTTPError as exc:
                        if exc.code != 404:
                            raise
                        client.check()
                        exc.close()
                    _recover_source(directory, endpoint, authority.key, authority.check)
                records[endpoint] = _publish_endpoint(directory, endpoint, authority)
            except (
                HTTPError,
                URLError,
                TimeoutError,
                ConnectionError,
                OptionalEndpointFailure,
            ) as exc:
                client.check()
                authority.check()
                failure = {
                    "session_id": authority.identity,
                    "endpoint": endpoint,
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                _save(failure_path, failure, authority.check)
                failures[endpoint] = failure
    authority.check()
    references, _, _ = validate_attempts(directory, entry)
    result = {
        "schema_version": "production-openf1-v1",
        "session_id": authority.identity,
        "session_key": authority.key,
        "binding": authority.binding,
        "endpoints": records,
        "failures": failures,
        "state": "complete" if not failures else "partial",
        "attempts": references,
        "attempts_index_sha256": file_sha256(directory / "attempts.json")
        if (directory / "attempts.json").exists()
        else None,
        "radio_audio_downloaded": False,
        "overlapping_car_position_duplicated": False,
    }
    if not failures:
        _save(manifest_path, result, authority.check)
        _inventory(directory, authority)
        _complete_manifest(directory, authority, records)
    return result, False


def _complete_manifest(
    directory: Path, authority: ProductionAuthority, records: dict[str, Any]
) -> dict[str, Any]:
    try:
        return _complete_manifest_proof(directory, authority, records)
    except (PermissionError, CapacityStop, CorpusAuthorityStop):
        raise
    except Exception as exc:
        raise CorpusAuthorityStop("production complete manifest authority is corrupt") from exc


def _complete_manifest_proof(
    directory: Path, authority: ProductionAuthority, records: dict[str, Any]
) -> dict[str, Any]:
    manifest = _read(directory / "manifest.json", directory)
    expected = {
        "schema_version": "production-openf1-v1",
        "session_id": authority.identity,
        "session_key": authority.key,
        "binding": authority.binding,
        "endpoints": records,
        "failures": {},
        "state": "complete",
        "radio_audio_downloaded": False,
        "overlapping_car_position_duplicated": False,
        "attempts_index_sha256": file_sha256(directory / "attempts.json"),
    }
    if (
        set(records) != set(ENDPOINTS)
        or not isinstance(manifest.get("attempts"), dict)
        or not manifest["attempts"]
        or set(manifest) != {*expected, "attempts"}
        or any(not _same(manifest.get(name), value) for name, value in expected.items())
    ):
        raise CorpusAuthorityStop("production complete manifest differs from validated artifacts")
    validate_attempts(directory, authority.entry, manifest["attempts"])
    for endpoint in ENDPOINTS:
        if not any(name.startswith(endpoint + "-attempt-") for name in manifest["attempts"]):
            raise CorpusAuthorityStop("production manifest lacks an original endpoint attempt")
    authority.check()
    return manifest


def run_openf1_corpus(
    project: Path,
    *,
    detailed_root: Path | None = None,
    season: int | None = None,
    from_season: int | None = None,
    session_ids: set[str] | None = None,
    session_types: set[str] | None = None,
    limit: int | None = None,
    client: CorpusOpenF1Client | None = None,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    paths = DetailedPaths(project, override=detailed_root)
    gate = CorpusGate(paths)  # Genuine full public proof ONCE at admission.
    catalog = [
        row
        for row in gate.catalog["sessions"]
        if row.get("state") == "completed"
        and type(row.get("season")) is int
        and row["season"] >= 2023
        and type(row.get("openf1_session_key")) is int
        and row["openf1_session_key"] > 0
    ]
    selected = select_sessions(
        catalog,
        season=season,
        from_season=from_season,
        session_ids=session_ids,
        session_types=session_types,
    )
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    work = selected if limit is None else selected[:limit]
    owned_client = client or CorpusOpenF1Client(paths.root)
    if (
        not isinstance(owned_client, CorpusOpenF1Client)
        or owned_client.root != paths.root.absolute()
        or owned_client.active
    ):
        raise ValueError("production requires its own inactive bound hourly client")
    result: dict[str, Any] = {
        "selected": len(selected),
        "scheduled": len(work),
        "skipped": len(selected) - len(work),
        "catalog_mapped_completed": len(catalog),
        "attempted": 0,
        "reused": 0,
        "failed": 0,
        "sessions": [],
        "enrichment_completed": 0,
        "corpus_complete": False,
        "radio_audio_downloaded": False,
        "overlapping_car_position_duplicated": False,
        "scope": {
            "season": season,
            "from_season": from_season,
            "session_ids": sorted(session_ids) if session_ids is not None else None,
            "session_types": sorted(session_types) if session_types is not None else None,
            "limit": limit,
        },
    }
    progress_path = _unlinked_path(paths.root / "runtime/corpus-openf1-progress.json")
    with owned_client:
        for entry in work:
            try:
                row, reused = _session(project, gate, entry, owned_client)
                result["reused"] += int(reused)
                result["attempted"] += int(not reused)
                result["failed"] += int(row["state"] != "complete")
            except BaseException as exc:
                if isinstance(
                    exc, (PermissionError, CapacityStop, CorpusAuthorityStop, HourlyBudgetStop)
                ) or not isinstance(exc, Exception):
                    owned_client._failure = exc
                    result["stopped"] = {
                        "session_id": entry["session_id"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    _save(progress_path, result, lambda: None)
                    raise
                owned_client.check()
                gate.check(entry)
                row = {
                    "session_id": entry["session_id"],
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                result["failed"] += 1
            result["sessions"].append(row)
            result["enrichment_completed"] += int(row["state"] == "complete")
            _save(progress_path, result, lambda entry=entry: gate.check(entry))
            if report is not None:
                report(json.loads(json.dumps(row, allow_nan=False)))
    # Repeat the expensive public proof only at the whole-workflow boundary.
    final_gate = CorpusGate(paths)
    if final_gate.bindings != gate.bindings:
        raise CorpusAuthorityStop("public storage authority changed at workflow completion")
    store = DetailedStore(project, detailed_root=paths.root)
    result["native_completed"] = 0
    for entry in select_sessions(final_gate.catalog["sessions"]):
        if (paths.canonical / entry["session_id"] / "active.json").exists():
            manifest = store.validate(entry["session_id"])
            if not _same(manifest["metadata"].get("session"), entry):
                raise CorpusAuthorityStop("current native revision canonical identity differs")
            result["native_completed"] += 1
    result["enrichment_completed"] = 0
    for entry in catalog:
        if (paths.root / "openf1" / entry["session_id"] / "manifest.json").exists():
            authority = ProductionAuthority(project, final_gate, entry)
            directory = paths.root / "openf1" / entry["session_id"]
            records, _ = _inventory(directory, authority)
            _complete_manifest(directory, authority, records)
            result["enrichment_completed"] += 1
    result["scope_complete"] = bool(work) and not result["failed"] and len(work) == len(selected)
    result["mapped_subset_complete"] = bool(catalog) and result["enrichment_completed"] == len(
        catalog
    )
    _save(progress_path, result, final_gate.check)
    return result
