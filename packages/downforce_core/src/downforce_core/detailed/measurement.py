"""Actual immutable canonical candidate measurements without touching old samples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.normalization import normalize_table
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.status import guard_operation
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256


def measure_candidate(
    project: Path,
    root: Path,
    measurement: dict[str, Any],
    compression: dict[str, Any],
) -> dict[str, Any]:
    if not compression.get("round_trip_exact"):
        raise ValueError("raw compression must pass exact restoration first")
    identity = measurement["session"]["session_id"]
    attempts = [
        path
        for path in (root / "preflight" / identity).glob("*/measurement.json")
        if json.loads(path.read_text()) == measurement
    ]
    if len(attempts) != 1:
        raise ValueError("ambiguous measured source attempt")
    directory = attempts[0].parent
    source = source_manifest(directory / "raw-cache")
    raw_store = RawEvidenceStore(root / "study" / "raw-bundles")
    bundle = compression["bundle_sha256"]
    manifest = raw_store.validate(bundle)
    if {key: manifest[key] for key in ("files", "directories")} != source:
        raise ValueError("raw compression evidence does not match authoritative sample")
    candidate = DetailedStore(project, detailed_root=root / "study" / "candidates")
    tables: dict[str, pa.Table] = {}
    channels = {}
    original_bytes = 0
    for name, descriptor in sorted(measurement["tables"].items()):
        path = ensure_contained(directory / f"{name}.parquet", directory)
        if file_sha256(path) != descriptor["sha256"]:
            raise ValueError("measured source table checksum changed")
        if type(descriptor["bytes"]) is not int or descriptor["bytes"] != path.stat().st_size:
            raise ValueError("invalid measured source table byte estimate")
        with pq.ParquetFile(path) as reader:
            original = reader.read()
        normalized, details = normalize_table(name, original)
        key = name.replace("/", "-").replace("_", "-")
        tables[key] = normalized
        channels[key] = details
        original_bytes += path.stat().st_size
    revision = candidate.publish(
        identity,
        tables,
        {
            "purpose": "storage-study canonical candidate; not production catalog activation",
            "session": measurement["session"],
            "provider": "fastf1",
            "provider_version": measurement["fastf1_version"],
            "raw_bundle_sha256": bundle,
            "channels": channels,
        },
    )
    canonical = candidate.validate(identity, revision=revision)
    revision_directory = candidate.root / identity / "revisions" / revision
    canonical_bytes = sum(table["bytes"] for table in canonical["tables"].values())
    pointer = candidate.root / identity / "active.json"
    catalog_entry_bytes = len(json.dumps(measurement["session"], sort_keys=True).encode())
    metadata_bytes = (revision_directory / "manifest.json").stat().st_size + pointer.stat().st_size
    metadata_bytes += attempts[0].stat().st_size + catalog_entry_bytes
    completion = root / "completion" / f"{identity}.json"
    if completion.exists():
        metadata_bytes += completion.stat().st_size
    expanded_bytes = sum(file["bytes"] for file in source["files"])
    archive_bytes = (raw_store.root / f"{bundle}.zip").stat().st_size
    peak_floor = max(compression["observed_peak_temporary_bytes"], expanded_bytes + archive_bytes)
    result = {
        "session_id": identity,
        "season": measurement["session"]["season"],
        "session_type": measurement["session"]["session_type"],
        "raw_expanded_bytes": expanded_bytes,
        "raw_evidence_bytes": archive_bytes,
        "canonical_original_bytes": original_bytes,
        "canonical_bytes": canonical_bytes,
        "metadata_bytes": metadata_bytes,
        "catalog_entry_bytes": catalog_entry_bytes,
        "revision_manifest_bytes": (revision_directory / "manifest.json").stat().st_size,
        "observed_compression_temporary_peak_bytes": compression["observed_peak_temporary_bytes"],
        "temporary_peak_bytes": peak_floor + 2 * canonical_bytes,
        "peak_method": (
            "maximum of observed archive-copy peak and source-plus-archive bytes; "
            "plus upper envelope for current+new canonical revision"
        ),
        "raw_file_count": len(source["files"]),
        "persistent_file_count": len(tables) + 3,
        "round_trip_exact": True,
        "semantic_identity": True,
        "raw_bundle_sha256": bundle,
        "candidate_revision": revision,
    }
    destination = root / "study" / "session-measurements" / f"{identity}.json"
    encoded = json.dumps(result, indent=2, allow_nan=False)
    guard_operation(
        destination,
        next_peak_bytes=2 * len(encoded.encode("utf-8")) + 4096,
        operation="candidate measurement report",
    )
    write_text_atomic(destination, encoded)
    return result
