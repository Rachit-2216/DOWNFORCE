"""Validate immutable production revisions before lifecycle-approved activation."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from downforce_core.detailed.lifecycle import Lifecycle
from downforce_core.detailed.normalization import normalize_table
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.status import guard_operation
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path, _read
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256


def _zero_car_evidence(driver: str, car: pa.Table, laps: pa.Table | None) -> dict[str, Any] | None:
    moving = ["Speed", "RPM", "Throttle", "nGear"]
    if (
        not car.num_rows
        or laps is None
        or not {"DriverNumber", "LapTime", "IsAccurate"}.issubset(laps.column_names)
    ):
        return None
    if not pa.types.is_duration(laps["LapTime"].type) or not pa.types.is_boolean(
        laps["IsAccurate"].type
    ):
        return None
    for name in moving:
        if name not in car.column_names or not (
            pa.types.is_integer(car[name].type) or pa.types.is_floating(car[name].type)
        ):
            return None
        if car[name].null_count or pc.all(pc.equal(car[name], 0)).as_py() is not True:
            return None
    clean = pc.and_(
        pc.equal(laps["DriverNumber"].cast(pa.string()), driver),
        pc.and_(
            pc.equal(laps["IsAccurate"], True),
            pc.greater(laps["LapTime"].cast(pa.duration("ns")).cast(pa.int64()), 0),
        ),
    )
    for name in ("PitInTime", "PitOutTime"):
        if name in laps.column_names:
            clean = pc.and_(clean, pc.is_null(laps[name]))
    count = pc.sum(pc.fill_null(clean, False).cast(pa.int64())).as_py()
    if not count:
        return None
    return {
        "state": "degraded",
        "usable": False,
        "reason": "constant-zero native moving channels despite clean timed lap activity",
        "all_zero_channels": moving,
        "native_rows": car.num_rows,
        "clean_timed_laps": count,
    }


def canonical_payload(
    entry: dict[str, Any],
    tables: dict[str, pa.Table],
    record: dict[str, Any],
    bundle: str,
) -> tuple[dict[str, pa.Table], dict[str, Any]]:
    normalized, channels = {}, {}
    availability = dict(record["availability"])
    degraded = []
    partitions = [
        name for name, table in tables.items() if name.startswith("car_data/") and table.num_rows
    ]
    for name, table in tables.items():
        key = name.replace("/", "-").replace("_", "-")
        if key in normalized:
            raise ValueError("native partition names collide in canonical publication")
        normalized[key], details = normalize_table(name, table)
        channels[key] = {
            **details,
            "availability": {"state": "available" if table.num_rows else "unavailable"},
        }
        if entry["season"] == 2026 and name.startswith("car_data/") and "DRS" in table.column_names:
            channels[key]["field_availability"] = {
                "drs": {
                    "state": "not-recorded",
                    "source": "provider-parser-default",
                    "provider_channel": "45",
                }
            }
        if name.startswith("car_data/"):
            evidence = _zero_car_evidence(name.split("/")[1], table, tables.get("laps"))
            if evidence is not None:
                channels[key]["availability"] = evidence
                availability[name] = evidence
                degraded.append(name)
    if degraded:
        availability["car_data"] = {
            "state": "telemetry-unsupported"
            if len(degraded) == len(partitions)
            else "partially-available",
            "reason": (
                "native car channels are unusable for moving telemetry despite clean timed laps"
            ),
            "degraded_drivers": [name.split("/")[1] for name in degraded],
        }
    quality = (
        "complete"
        if availability and all(v["state"] == "available" for v in availability.values())
        else "partial"
    )
    return normalized, {
        "session": entry,
        "provider": "fastf1",
        "provider_version": record["provider_version"],
        "raw_bundle_sha256": bundle,
        "channels": channels,
        "channel_availability": availability,
        "quality": quality,
    }


def publish_inactive(
    project: Path,
    paths: DetailedPaths,
    owned: DetailedPaths,
    session_id: str,
    tables: dict[str, pa.Table],
    metadata: dict[str, Any],
    check_authority: Callable[[], None],
) -> str:
    candidate = DetailedStore(project, detailed_root=owned.root / "candidates")
    revision = candidate.publish(session_id, tables, metadata)
    manifest = candidate.validate(session_id, revision=revision)
    check_authority()
    production = DetailedStore(project, detailed_root=paths.root)
    revisions = _path(paths.root, f"canonical/{session_id}/revisions")
    guard_operation(
        revisions,
        next_peak_bytes=2 * sum(d["bytes"] for d in manifest["tables"].values())
        + 2 * len(json.dumps(manifest).encode())
        + 65536,
        operation="inactive canonical revision copy",
    )
    revisions.mkdir(parents=True, exist_ok=True)
    target = _path(paths.root, f"canonical/{session_id}/revisions/{revision}")
    if not target.exists():
        source = _path(candidate.root, f"{session_id}/revisions/{revision}")
        temporary = _path(paths.root, f"canonical/{session_id}/revisions/.publishing-{uuid4().hex}")
        shutil.copytree(source, temporary)
        if {path.name for path in temporary.iterdir()} != {
            "manifest.json",
            *(f"{name}.parquet" for name in tables),
        }:
            raise ValueError("unmanifested production revision copy")
        for name, descriptor in manifest["tables"].items():
            if file_sha256(temporary / f"{name}.parquet") != descriptor["sha256"]:
                raise ValueError("production canonical copy checksum mismatch")
        if file_sha256(temporary / "manifest.json") != file_sha256(source / "manifest.json"):
            raise ValueError("production canonical manifest copy mismatch")
        check_authority()
        os.rename(temporary, target)
    production.validate(session_id, revision=revision)
    return revision


def activate(paths: DetailedPaths, session_id: str, revision: str) -> None:
    pointer = _path(paths.root, f"canonical/{session_id}/active.json")
    expected = {"revision": revision}
    if pointer.exists():
        if json.loads(pointer.read_text()) == expected:
            return
        raise ValueError("existing production activation differs from validated corpus job")
    guard_operation(pointer, next_peak_bytes=65536, operation="validated production activation")
    write_text_atomic(pointer, json.dumps(expected, sort_keys=True))


def failure_status(owned: DetailedPaths, session_id: str, error: Exception) -> dict[str, Any]:
    job = Lifecycle(owned, session_id).read()
    record_path = _path(owned.root, f"staging/{session_id}/native.json")
    native = _read(record_path) if record_path.exists() else {}
    return {
        "session_id": session_id,
        "state": "failed",
        "checkpoint": job["checkpoint"],
        "raw_identity": job.get("raw_identity"),
        "canonical_identity": job.get("canonical_identity"),
        "quality": "unvalidated",
        "availability": native.get("availability", {}),
        "error": f"{type(error).__name__}: {error}",
    }


def emit_progress(
    row: dict[str, Any],
    report: Callable[[dict[str, Any]], None] | None,
    *,
    attempted: bool,
) -> None:
    if report is not None:
        snapshot = deepcopy(row)
        snapshot.update(attempted=attempted, reused=not attempted and row["state"] == "complete")
        report(snapshot)
