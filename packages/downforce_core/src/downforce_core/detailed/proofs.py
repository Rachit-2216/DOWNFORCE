"""Fail-closed artifact verification at the storage-study I/O boundary."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.enrichment_paths import (
    EVIDENCE_PATH,
    default_evidence_path,
    evidence_directory,
    evidence_path,
    reverified_evidence_path,
)
from downforce_core.detailed.normalization import normalize_table
from downforce_core.detailed.openf1 import (
    ENDPOINTS,
    MAX_RESPONSE_BYTES,
    validate_attempts,
    validate_endpoint,
)
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.status import (
    archive_peak_bytes,
    copy_peak_bytes,
    parquet_peak_bytes,
    provider_peak_bytes,
    revision_peak_bytes,
)
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _validate_measurement
from downforce_core.detailed.temporal_completion import PROOF_DIRECTORY, load_temporal_completions
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256


def _path(path: Path, root: Path) -> Path:
    checked = ensure_contained(path, root)
    cursor = path.absolute()
    while cursor != root.absolute():
        if cursor.is_junction():
            raise StorageIntegrityError("study proof must not use linked directories")
        cursor = cursor.parent
    return checked


def _json(path: Path, root: Path) -> dict[str, Any]:
    value = json.loads(_path(path, root).read_text())
    if not isinstance(value, dict):
        raise ValueError("study proof must be a JSON object")
    return value


def _equal(report: dict[str, Any], actual: dict[str, Any], label: str) -> None:
    for key, value in actual.items():
        if report.get(key) != value or type(report.get(key)) is not type(value):
            raise ValueError(f"{label} proof mismatch: {key}")


def _candidate(
    root: Path, report: dict[str, Any], entry: dict[str, Any], source_entry: dict[str, Any]
) -> dict[str, Any]:
    identity = validate_safe_identifier(report["session_id"])
    _equal(report, {key: entry[key] for key in ("session_id", "season", "session_type")}, "sample")
    primary_path = root / "preflight" / identity / "measurement.json"
    primary = _json(primary_path, root)
    tables = primary.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("authoritative source tables must be a JSON object")
    if any(not isinstance(descriptor, dict) for descriptor in tables.values()):
        raise ValueError("authoritative source table descriptors must be JSON objects")
    if any(
        primary["session"][key] != entry[key] for key in ("session_id", "season", "session_type")
    ):
        raise ValueError("authoritative source session identity changed")
    attempts = [
        path
        for path in primary_path.parent.glob("*/measurement.json")
        if _json(path, root) == primary
    ]
    if len(attempts) != 1:
        raise ValueError("ambiguous authoritative source attempt")
    attempt = _path(attempts[0].parent, root)
    _validate_measurement(
        attempt,
        primary,
        source_entry,
        archive_complete=(entry.get("archive_status") or {}).get("Status") == "Complete",
        root=root,
    )
    raw_root = _path(root / "study" / "raw-bundles", root)
    store = RawEvidenceStore(raw_root)
    bundle = report["raw_bundle_sha256"]
    raw = store.validate(bundle)
    with zipfile.ZipFile(_path(raw_root / f"{bundle}.zip", root)) as archive:
        if any(member.compress_type != zipfile.ZIP_DEFLATED for member in archive.infolist()):
            raise ValueError("raw archive codec does not match ZIP-DEFLATE proof")
    _equal(
        raw,
        {
            "session_id": identity,
            "provider": "fastf1",
            "provider_version": primary["fastf1_version"],
        },
        "raw manifest",
    )
    source = source_manifest(_path(attempt / "raw-cache", root))
    if {key: raw[key] for key in ("files", "directories")} != source:
        raise ValueError("raw bundle does not match authoritative source")
    candidate_root = _path(root / "study" / "candidates", root)
    _path(candidate_root / "canonical", root)
    candidate = DetailedStore(root, detailed_root=candidate_root)
    revision = report["candidate_revision"]
    canonical = candidate.validate(identity, revision=revision)
    metadata = canonical["metadata"]
    _equal(
        metadata,
        {
            "provider": "fastf1",
            "provider_version": primary["fastf1_version"],
            "raw_bundle_sha256": bundle,
            "session": primary["session"],
        },
        "candidate manifest",
    )
    pointer_path = _path(candidate.root / identity / "active.json", root)
    _equal(_json(pointer_path, root), {"revision": revision}, "candidate pointer")
    revision_root = _path(candidate.root / identity / "revisions" / revision, root)
    original_bytes = 0
    source_peak = canonical_peak = schema_bytes = 0
    expected_tables = set()
    for name, descriptor in primary["tables"].items():
        original_path = _path(attempt / f"{name}.parquet", attempt)
        if file_sha256(original_path) != descriptor["sha256"]:
            raise ValueError("authoritative source table checksum mismatch")
        with pq.ParquetFile(original_path) as reader:
            original = reader.read()
        _equal(
            descriptor,
            {
                "bytes": original_path.stat().st_size,
                "rows": original.num_rows,
                "schema": str(original.schema),
                "columns": original.column_names,
            },
            "source table",
        )
        key = name.replace("/", "-").replace("_", "-")
        if key in expected_tables:
            raise ValueError("ambiguous canonical table identity")
        expected_tables.add(key)
        normalized, details = normalize_table(name, original)
        source_peak += parquet_peak_bytes(original, source_bytes=original_path.stat().st_size)
        canonical_peak += parquet_peak_bytes(normalized, row_group_size=16384)
        schema_bytes += normalized.schema.serialize().size
        actual = candidate.read_table(identity, key, revision=revision)
        if (
            not actual.equals(normalized, check_metadata=True)
            or metadata["channels"].get(key) != details
        ):
            raise ValueError("canonical native-schema or semantic identity mismatch")
        original_bytes += original_path.stat().st_size
    if not expected_tables or set(canonical["tables"]) != expected_tables:
        raise ValueError("canonical table inventory mismatch")
    if {path.name for path in revision_root.iterdir()} != {
        "manifest.json",
        *(f"{key}.parquet" for key in expected_tables),
    }:
        raise ValueError("unmanifested candidate artifact")
    canonical_bytes = sum(record["bytes"] for record in canonical["tables"].values())
    manifest_bytes = _path(revision_root / "manifest.json", root).stat().st_size
    catalog_bytes = len(json.dumps(primary["session"], sort_keys=True).encode())
    metadata_bytes = (
        manifest_bytes + pointer_path.stat().st_size + attempts[0].stat().st_size + catalog_bytes
    )
    completion = root / "completion" / f"{identity}.json"
    if completion.exists():
        metadata_bytes += _path(completion, root).stat().st_size
    compression_path = root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json"
    compression = _json(compression_path, root)
    expanded_bytes = sum(record["bytes"] for record in raw["files"])
    archive_bytes = _path(raw_root / f"{bundle}.zip", root).stat().st_size
    upper_bound = expanded_bytes + archive_bytes
    if expanded_bytes <= 0:
        raise ValueError("empty raw evidence cannot establish compression metrics")
    _equal(
        compression,
        {
            "session_id": identity,
            "bundle_sha256": bundle,
            "round_trip_exact": True,
            "codec": "ZIP-DEFLATE",
            "level": 6,
            "original_bytes": expanded_bytes,
            "compressed_bytes": archive_bytes,
            "ratio": archive_bytes / expanded_bytes,
            "peak_temporary_bytes_upper_bound": upper_bound,
            "sampling_interval_ms": 20,
            "tool_version": "detailed-storage-study-v2",
        },
        "compression",
    )
    observed_peak = compression["observed_peak_temporary_bytes"]
    if type(observed_peak) is not int or not 0 <= observed_peak <= upper_bound:
        raise ValueError("invalid observed compression working peak")
    for field in ("pack_seconds_including_validation", "restore_seconds_including_validation"):
        seconds = compression[field]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid compression timing proof")
    historical_peak = max(observed_peak, upper_bound) + 2 * canonical_bytes
    reported_peak = report["temporary_peak_bytes"]
    # Legacy records stored the observed-only peak. Validate that formula but never
    # let a missed sampling interval reduce the deterministic safety floor.
    if type(reported_peak) is not int or reported_peak not in (
        observed_peak + 2 * canonical_bytes,
        historical_peak,
    ):
        raise ValueError("candidate measurement proof mismatch: temporary_peak_bytes")
    actual_values = {
        "raw_expanded_bytes": expanded_bytes,
        "raw_evidence_bytes": archive_bytes,
        "canonical_original_bytes": original_bytes,
        "canonical_bytes": canonical_bytes,
        "metadata_bytes": metadata_bytes,
        "catalog_entry_bytes": catalog_bytes,
        "revision_manifest_bytes": manifest_bytes,
        "raw_file_count": len(raw["files"]),
        "persistent_file_count": len(expected_tables) + 3,
        "observed_compression_temporary_peak_bytes": observed_peak,
        "round_trip_exact": True,
        "semantic_identity": True,
    }
    _equal(report, actual_values, "candidate measurement")
    # Recompute today's operation budget from verified artifacts. Historical
    # writer formulas remain strict and unchanged; they are not today's guard.
    conservative_metadata = (
        2 * (attempts[0].stat().st_size + len(json.dumps(raw, sort_keys=True).encode("utf-8")))
        + 65536
    )
    operation_peak = max(
        copy_peak_bytes(source) + archive_peak_bytes(source, metadata_bytes=conservative_metadata),
        provider_peak_bytes(source, native_peak_bytes=source_peak),
        revision_peak_bytes(
            [canonical_peak],
            metadata_bytes=len(
                json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                    "utf-8"
                )
            ),
            schema_bytes=schema_bytes,
        )
        + 65536 * max(0, len(expected_tables) - 1),
    )
    effective_peak = max(historical_peak, operation_peak)
    proof_metadata_bytes = (
        _path(root / "study" / "session-measurements" / f"{identity}.json", root).stat().st_size
        + _path(compression_path, root).stat().st_size
        + _path(primary_path, root).stat().st_size
    )
    temporal_cost: dict[str, Any] = {}
    temporal_files: set[str] = set()
    if source_entry.get("completion_temporal_sha256") is not None:
        discovery = _json(root / "inventory.json", root)
        temporal, hashes = load_temporal_completions(root, discovery["sessions"])
        if hashes.get(identity) != source_entry["completion_temporal_sha256"]:
            raise ValueError("current temporal metadata cost lacks the bound proof")
        temporal_files = {
            f"{PROOF_DIRECTORY}/{identity}.json",
            *temporal[identity]["dependencies"],
        }
        temporal_files.discard(f"completion/{identity}.json")  # Already in reported metadata.
        authority_bytes = sum(
            _path(root / relative, root).stat().st_size for relative in temporal_files
        )
        # Shared index/batch closure is charged conservatively per sampled session.
        # Historical report arithmetic and projection/scenario formulas stay intact.
        temporal_cost = {
            "temporal_authority_metadata_bytes": authority_bytes,
            "temporal_authority_file_count": len(temporal_files),
            "temporal_authority_cost_basis": "conservative-full-verified-closure-per-sample",
        }
        proof_metadata_bytes += authority_bytes
    return {
        **{key: value for key, value in report.items() if not key.startswith("openf1_")},
        "reported_metadata_bytes": metadata_bytes,
        "study_proof_metadata_bytes": proof_metadata_bytes,
        "metadata_bytes": metadata_bytes + proof_metadata_bytes,
        "reported_persistent_file_count": report["persistent_file_count"],
        "persistent_file_count": len(expected_tables)
        + 7
        + int(completion.exists())
        + len(temporal_files),
        "reported_temporary_peak_bytes": reported_peak,
        "historical_temporary_peak_bytes": historical_peak,
        "conservative_operation_peak_bytes": operation_peak,
        "temporary_peak_bytes": effective_peak,
        "peak_method": (
            "max validated historical source/archive+two-revision floor and current conservative "
            "copy/DEFLATE/expanded-Arrow/metadata/provider-cache operation budgets"
        ),
        **temporal_cost,
    }


def _enrichment(
    root: Path,
    entry: dict[str, Any],
    *,
    endpoints: tuple[str, ...] = ENDPOINTS,
    _evidence_path: str | None = None,
) -> dict[str, Any]:
    identity = entry["session_id"]
    candidate = _json(root / "study/session-measurements" / f"{identity}.json", root)
    relative = evidence_path(entry, candidate) if _evidence_path is None else _evidence_path
    directory = _path(evidence_directory(root, entry, relative), root)
    report = _json(directory / "measurement.json", root)
    if evidence_path(entry, report) != relative:
        raise ValueError("enrichment report differs from its bound evidence path")
    session_key = entry["openf1_session_key"]
    if type(session_key) is not int or session_key <= 0:
        raise ValueError("invalid reconciled enrichment provider key")
    _equal(
        report,
        {
            "session_id": identity,
            "session_key": session_key,
            "openf1_measured": True,
            "radio_audio_downloaded": False,
            "overlapping_car_position_duplicated": False,
        },
        "enrichment",
    )
    if set(report["endpoints"]) != set(endpoints):
        raise ValueError("incomplete enrichment endpoint proof")
    raw_bytes = canonical_bytes = 0
    expected_files = {"measurement.json"}
    _attempts, attempt_files, _ = validate_attempts(directory, entry, report.get("attempts", {}))
    if relative == reverified_evidence_path(entry):
        for endpoint in endpoints:
            if not any(name.startswith(f"{endpoint}-attempt-") for name in _attempts):
                raise ValueError("reverified evidence requires an indexed native HTTP attempt")
    if attempt_files:
        _equal(
            report,
            {"attempts_index_sha256": file_sha256(directory / "attempts.json")},
            "enrichment attempt index",
        )
    expected_files.update(attempt_files)
    wire_files = {name for name in attempt_files if name.endswith(".gz")}
    for endpoint, record in report["endpoints"].items():
        actual_record, _ = validate_endpoint(directory, endpoint, session_key)
        if actual_record != record:
            raise ValueError("enrichment endpoint record changed")
        endpoint_path = directory / f"{endpoint}.json"
        source_path = directory / f"{endpoint}-source.json"
        if _json(endpoint_path, directory) != record:
            raise ValueError("enrichment endpoint record changed")
        source = _json(source_path, directory)
        expected_files.update((endpoint_path.name, source_path.name))
        raw_path = _path(directory / record["raw_file"], directory)
        canonical_path = _path(directory / record["canonical_file"], directory)
        expected_files.update((raw_path.name, canonical_path.name))
        for path, field in ((raw_path, "raw_file"), (canonical_path, "canonical_file")):
            if file_sha256(path) != record[field + "_sha256"]:
                raise ValueError("enrichment artifact checksum mismatch")
        with gzip.open(raw_path, "rb") as handle:
            body = handle.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("oversized enrichment wire proof")
        digest = hashlib.sha256(body).hexdigest()
        http_status = record["http_status"]
        if type(http_status) is not int or http_status not in (200, 404):
            raise ValueError("unsupported enrichment HTTP proof status")
        url = f"https://api.openf1.org/v1/{endpoint}?session_key={session_key}"
        _equal(
            source,
            {
                "url": url,
                "response_sha256": digest,
                "raw_file": raw_path.name,
                "raw_file_sha256": record["raw_file_sha256"],
                "http_status": http_status,
            },
            "HTTP source",
        )
        rows = json.loads(body) if http_status == 200 else []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("invalid enrichment wire observations")
        if any(row.get("session_key") != session_key for row in rows):
            raise ValueError("enrichment wire session identity mismatch")
        # validate_endpoint owns and closes the reader and checks native schema,
        # values, schema metadata, byte identity and source metadata together.
        availability = (
            "unavailable"
            if http_status == 404
            else "available"
            if rows
            else "empty-provider-response"
        )
        _equal(
            record,
            {
                "url": url,
                "response_sha256": digest,
                "source_body_bytes": len(body),
                "raw_file": f"{endpoint}-{digest}.json.gz",
                "canonical_file": f"{endpoint}-{digest}.parquet",
                "raw_bytes": raw_path.stat().st_size,
                "canonical_bytes": canonical_path.stat().st_size,
                "rows": len(rows),
                "availability": availability,
            },
            "endpoint",
        )
        wire_files.add(raw_path.name)
        canonical_bytes += canonical_path.stat().st_size
    if {path.name for path in directory.iterdir()} != expected_files:
        raise ValueError("unmanifested enrichment artifacts")
    raw_bytes = sum((directory / name).stat().st_size for name in wire_files)
    metadata_bytes = sum(
        _path(directory / name, root).stat().st_size
        for name in expected_files
        if name.endswith(".json")
    )
    measurement_bytes = (directory / "measurement.json").stat().st_size
    # The original writer's raw total predates publication of its own measurement file.
    _equal(
        report,
        {
            "openf1_raw_bytes": raw_bytes + metadata_bytes - measurement_bytes,
            "openf1_canonical_bytes": canonical_bytes,
        },
        "enrichment measurement",
    )
    path_marker = {EVIDENCE_PATH: relative} if relative != default_evidence_path(entry) else {}
    return {
        **path_marker,
        "openf1_measured": True,
        "openf1_raw_bytes": raw_bytes + metadata_bytes,
        "openf1_canonical_bytes": canonical_bytes,
        "openf1_metadata_bytes": metadata_bytes,
        "openf1_wire_bytes": raw_bytes,
        "openf1_persistent_file_count": len(expected_files),
        "openf1_wire_exact": True,
        "openf1_native_schema_exact": True,
    }


def validated_study_measurements(paths: DetailedPaths) -> list[dict[str, Any]]:
    """Read only measured plan members, verifying all artifacts before projection."""
    root = paths.root
    try:
        inventory_path = _path(root / "reconciled-inventory.json", root)
        discovery = root / "inventory.json"
        if discovery.exists() or discovery.is_symlink() or discovery.is_junction():
            if _json(discovery, root).get("complete_inventory") is not True:
                raise ValueError("provider discovery is incomplete")
        from downforce_core.detailed.study_io import validated_frozen_plan

        catalog_digest = file_sha256(inventory_path)
        plan_digest = file_sha256(_path(root / "study" / "sample-plan.json", root))
        catalog, originals, plan = validated_frozen_plan(root)
        selected = plan["selected_sessions"]
        by_id = {entry["session_id"]: entry for entry in catalog["sessions"]}
        identities = [validate_safe_identifier(entry["session_id"]) for entry in selected]
        if not identities or len(identities) != len(set(identities)):
            raise ValueError("empty or duplicate frozen sample identities")
        for entry in selected:
            if entry != by_id.get(entry["session_id"]) or entry["state"] != "completed":
                raise ValueError("frozen selection does not match completed inventory")
        directory = _path(root / "study" / "session-measurements", root)
        filenames = {path.name for path in directory.iterdir()}
        if filenames != {f"{identity}.json" for identity in identities}:
            raise ValueError("all and only frozen selected measurements are required")
        samples = []
        for identity in sorted(identities):
            report = _json(directory / f"{identity}.json", root)
            entry = by_id[identity]
            sample = _candidate(root, report, entry, originals[identity])
            if entry["season"] >= 2023 and entry.get("openf1_session_key") is not None:
                sample.update(_enrichment(root, entry))
            elif report.get("openf1_measured"):
                raise ValueError("unsupported session claims enrichment proof")
            samples.append(sample)
        validated_frozen_plan(root)
        if (
            file_sha256(inventory_path) != catalog_digest
            or file_sha256(_path(root / "study" / "sample-plan.json", root)) != plan_digest
        ):
            raise ValueError("catalog or frozen plan changed during measurement validation")
        return samples
    except (
        OSError,
        EOFError,
        KeyError,
        TypeError,
        ValueError,
        pa.ArrowException,
        zlib.error,
    ) as exc:
        raise ValueError(f"storage study proof unavailable or invalid: {exc}") from exc
