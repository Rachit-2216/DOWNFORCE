"""Bounded production ingestion; frozen studies are evidence, never mutable jobs."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import fastf1  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]

from downforce_core.detailed.corpus_cleanup import (
    CleanupStop,
    register_verified_offline,
    release_owned_session,
    resume_released,
)
from downforce_core.detailed.corpus_gate import CorpusAuthorityStop, CorpusGate
from downforce_core.detailed.corpus_gate import select_sessions as select_sessions
from downforce_core.detailed.corpus_native import (
    capture,
    exact_native,
    persist_native,
    provider_loader,
    stop_latch,
)
from downforce_core.detailed.corpus_publication import (
    activate,
    emit_progress,
    failure_status,
    publish_inactive,
)
from downforce_core.detailed.corpus_publication import canonical_payload as canonical_payload
from downforce_core.detailed.lifecycle import Lifecycle
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import fastf1_guard
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.status import CapacityStop, copy_peak_bytes, guard_operation
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path, _read
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256

Loader = Callable[[dict[str, Any], Path, bool], Any]


def _save(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    guard_operation(
        path, next_peak_bytes=2 * len(encoded.encode()) + 4096, operation="corpus checkpoint"
    )
    write_text_atomic(path, encoded)


def _availability(tables: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    result = dict(recorded)
    for name in (
        "results",
        "laps",
        "session_status",
        "track_status",
        "weather_data",
        "race_control_messages",
        "corners",
        "car_data",
        "pos_data",
    ):
        members = [d for key, d in tables.items() if key == name or key.startswith(name + "/")]
        if any(member["rows"] > 0 for member in members):
            result[name] = {"state": "available"}
        elif name not in result or result[name].get("state") == "available":
            result[name] = {"state": "unavailable", "reason": "no native provider rows"}
    if result["car_data"]["state"] != "available":
        result["car_data"] = {"state": "telemetry-unsupported", "reason": "no native car samples"}
    return result


def _tables(directory: Path, descriptors: dict[str, Any]) -> dict[str, pa.Table]:
    tables = {}
    for name, descriptor in descriptors.items():
        path = _path(directory, f"{name}.parquet")
        if file_sha256(path) != descriptor["sha256"]:
            raise ValueError("captured native table checksum changed")
        with pq.ParquetFile(path) as reader:
            table = reader.read()
        if table.num_rows != descriptor["rows"] or str(table.schema) != descriptor["schema"]:
            raise ValueError("captured native schema changed")
        tables[name] = table
    for name in ("results", "laps"):
        if name not in tables or tables[name].num_rows == 0:
            raise ValueError(f"required native table unavailable: {name}")
    return tables


def _frozen(gate: CorpusGate, entry: dict[str, Any]) -> dict[str, Any] | None:
    identity, root = entry["session_id"], gate.paths.root
    report = gate.measurements.get(identity)
    if report is None:
        return None
    primary = _read(_path(root, f"preflight/{identity}/measurement.json"))
    attempts = [
        path.parent
        for path in _path(root, f"preflight/{identity}").glob("*/measurement.json")
        if _read(_path(root, path.relative_to(root).as_posix())) == primary
    ]
    if len(attempts) != 1:
        raise ValueError("ambiguous frozen native source")
    receipt_path = _path(root, f"study/provider-restore/{identity}.json")
    receipt = _read(receipt_path)
    expected = {name for name in primary["tables"] if name != "corners"}
    if (
        receipt.get("session_id") != identity
        or receipt.get("offline") is not True
        or receipt.get("native_tables_exact") is not True
        or set(receipt.get("tables_checked", [])) != expected
        or receipt.get("bundle_sha256") != report["raw_bundle_sha256"]
    ):
        raise ValueError("frozen provider restore receipt is not bound to exact native source")
    return {
        "session": entry,
        "source_directory": str(attempts[0]),
        "raw_source": str(attempts[0] / "raw-cache"),
        "tables": primary["tables"],
        "provider_version": primary["fastf1_version"],
        "availability": _availability(primary["tables"], primary.get("channel_availability", {})),
        "frozen_bundle": report["raw_bundle_sha256"],
        "restore_receipt_sha256": file_sha256(receipt_path),
    }


def _raw(paths: DetailedPaths, record: dict[str, Any], raw: RawEvidenceStore) -> str:
    identity = record["session"]["session_id"]
    source = _path(paths.root, Path(record["raw_source"]).relative_to(paths.root).as_posix())
    bundle = record.get("frozen_bundle")
    if bundle is None:
        return raw.pack(
            source,
            session_id=identity,
            provider="fastf1",
            provider_version=record["provider_version"],
            level=6,
        )
    original = RawEvidenceStore(_path(paths.root, "study/raw-bundles"))
    manifest = original.validate(bundle)
    if {key: manifest[key] for key in ("files", "directories")} != source_manifest(source):
        raise ValueError("frozen raw source no longer matches immutable bundle")
    guard_operation(
        raw.root, next_peak_bytes=copy_peak_bytes(manifest), operation="raw bundle copy"
    )
    raw.root.mkdir(parents=True, exist_ok=True)
    target = _path(raw.root, f"{bundle}.zip")
    if not target.exists():
        guard_operation(
            target, next_peak_bytes=copy_peak_bytes(manifest), operation="raw bundle copy"
        )
        raw.root.mkdir(parents=True, exist_ok=True)
        temporary = _path(raw.root, f"{bundle}.copy")
        if temporary.exists() and file_sha256(temporary) != bundle:
            raise ValueError("incomplete owned raw bundle copy requires review")
        if not temporary.exists():
            shutil.copyfile(original.root / f"{bundle}.zip", temporary)
        if file_sha256(temporary) != bundle:
            raise ValueError("raw archive copy checksum changed")
        os.rename(temporary, target)
    raw.validate(bundle)
    return str(bundle)


def _session(
    project: Path,
    gate: CorpusGate,
    owned: DetailedPaths,
    entry: dict[str, Any],
    load: Loader,
    check_permission: Callable[[], None],
    on_attempt: Callable[[], None],
) -> dict[str, Any]:
    gate.check(entry)
    lifecycle = Lifecycle(owned, entry["session_id"])
    state = lifecycle.resume()
    directory = _path(owned.root, f"staging/{entry['session_id']}")
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / "native.json"
    try:
        record = _read(record_path) if record_path.exists() else _frozen(gate, entry)
        if record is None:
            if state == "DISCOVERED":
                lifecycle.advance("DOWNLOADING")
            gate.check(entry)
            source = gate.sources[entry["session_id"]]
            completion = _read(_path(gate.paths.root, f"completion/{entry['session_id']}.json"))
            loader_entry = {
                **entry,
                **source,
                "timing_path": source.get("timing_path", completion["timing_path"]),
            }
            # A failed download never overwrites a prior partially captured attempt.
            attempt = directory / f"attempt-{uuid4().hex}"
            cache = attempt / "raw-cache"
            cache.mkdir(parents=True)
            fastf1.Cache.enable_cache(str(cache))
            fastf1.Cache.offline_mode(False)
            on_attempt()
            with stop_latch() as check_stop:
                native = load(loader_entry, cache, False)
                check_permission()
                check_stop()
                if native.api_path != "/static/" + loader_entry["timing_path"]:
                    raise ValueError(
                        "loaded native session identity differs from captured timing path"
                    )
                # Optional getter metadata must already be cached. No acquisition
                # is allowed while deriving the exact offline-restorable capture.
                fastf1.Cache.offline_mode(True)
                with stop_latch(offline=True) as check_offline:
                    native_tables, available = capture(native, check_offline)
                check_permission()
                check_stop()
            descriptors = persist_native(attempt, native_tables)
            record = {
                "session": entry,
                "loader_entry": loader_entry,
                "source_directory": str(attempt),
                "raw_source": str(cache),
                "tables": descriptors,
                "provider_version": fastf1.__version__,
                "availability": _availability(descriptors, available),
            }
            _save(record_path, record)
            lifecycle.advance("SOURCE_CACHED")
            state = lifecycle.read()["checkpoint"]
        if record["session"] != entry:
            raise ValueError("production checkpoint session differs from current canonical entry")
        if "frozen_bundle" in record and _frozen(gate, entry) != record:
            raise ValueError("frozen source proof changed since corpus checkpoint")
        if state == "COMPLETE":
            released = resume_released(project, gate.paths, entry["session_id"])
            if released is not None:
                return released
        if state in ("DISCOVERED", "DOWNLOADING"):
            if state == "DISCOVERED":
                lifecycle.advance("DOWNLOADING")
            _save(record_path, record)
            lifecycle.advance("SOURCE_CACHED")
        source_directory = _path(
            gate.paths.root,
            Path(record["source_directory"]).relative_to(gate.paths.root).as_posix(),
        )
        tables = _tables(source_directory, record["tables"])
        raw = RawEvidenceStore(gate.paths.raw)
        state = lifecycle.read()["checkpoint"]
        if state == "SOURCE_CACHED":
            gate.check(entry)
            bundle = _raw(gate.paths, record, raw)
            lifecycle.advance("RAW_EVIDENCE_PUBLISHED", identity=bundle)
        bundle = lifecycle.read()["raw_identity"]
        raw_manifest = raw.validate(bundle)
        if (
            raw_manifest["session_id"] != entry["session_id"]
            or raw_manifest["provider_version"] != record["provider_version"]
        ):
            raise ValueError("production raw manifest differs from native capture")
        restored = directory / f"restore-{bundle}"
        if not restored.exists():
            gate.check(entry, peak=copy_peak_bytes(raw_manifest))
            raw.restore(bundle, restored)
        if source_manifest(restored) != {
            key: raw_manifest[key] for key in ("files", "directories")
        }:
            raise ValueError("retained restored raw source changed")
        if "frozen_bundle" not in record:
            receipt = directory / f"provider-restore-{bundle}.json"
            ownership = directory / f"offline-owner-{bundle}.json"
            expected = {
                "bundle_sha256": bundle,
                "tables": record["tables"],
                "provider_version": record["provider_version"],
                "native_tables_exact": True,
            }
            if receipt.exists():
                observed = _read(receipt)
                if "offline_owner_sha256" in observed:
                    expected["offline_owner_sha256"] = file_sha256(ownership)
                if observed != expected:
                    raise ValueError("production provider restoration receipt changed")
            else:
                gate.check(entry)
                working = directory / f"offline-{uuid4().hex}"
                guard_operation(
                    working,
                    next_peak_bytes=4 * copy_peak_bytes(raw_manifest),
                    operation="offline provider working copy",
                )
                shutil.copytree(restored, working)
                with stop_latch(offline=True) as check_offline:
                    native = load(record["loader_entry"], working, True)
                    check_offline()
                    if native.api_path != "/static/" + record["loader_entry"]["timing_path"]:
                        raise ValueError("restored native session identity differs")
                    restored_tables, _ = capture(native, check_offline)
                    exact_native(tables, restored_tables)
                check_permission()
                expected["offline_owner_sha256"] = register_verified_offline(
                    gate.paths, entry["session_id"], bundle, working
                )
                _save(receipt, expected)
        normalized, metadata = canonical_payload(entry, tables, record, bundle)
        availability, quality = metadata["channel_availability"], metadata["quality"]
        store = DetailedStore(project, detailed_root=gate.paths.root)
        state = lifecycle.read()["checkpoint"]
        if state == "RAW_EVIDENCE_PUBLISHED":
            lifecycle.advance("NORMALIZING")
        if lifecycle.read()["checkpoint"] == "NORMALIZING":
            gate.check(entry)
            check_permission()
            revision = publish_inactive(
                project,
                gate.paths,
                owned,
                entry["session_id"],
                normalized,
                metadata,
                lambda: gate.check(entry),
            )
            lifecycle.advance("CANONICAL_PUBLISHED", identity=revision)
        revision = lifecycle.read()["canonical_identity"]
        manifest = store.validate(entry["session_id"], revision=revision)
        if manifest["metadata"] != metadata or set(manifest["tables"]) != set(normalized):
            raise ValueError("production canonical metadata differs from native evidence")
        for name, table in normalized.items():
            if not store.read_table(entry["session_id"], name, revision=revision).equals(
                table, check_metadata=True
            ):
                raise ValueError("production canonical native schema mismatch")
        gate.check(entry)
        if lifecycle.read()["checkpoint"] == "CANONICAL_PUBLISHED":
            lifecycle.advance("VALIDATED", verified=True)
        activate(gate.paths, entry["session_id"], revision)
        if lifecycle.read()["checkpoint"] == "VALIDATED":
            lifecycle.advance("COMPLETE")  # Deliberately retain staging/cache; no CACHE_RELEASED.
        return {
            "session_id": entry["session_id"],
            "state": "complete",
            "raw_identity": bundle,
            "canonical_identity": revision,
            "quality": quality,
            "availability": availability,
        }
    except BaseException as exc:
        if lifecycle.read()["state"] != "COMPLETE":
            lifecycle.fail(f"{type(exc).__name__}: {exc}")
        raise


def run_corpus(
    project: Path,
    *,
    detailed_root: Path | None = None,
    season: int | None = None,
    from_season: int | None = None,
    session_ids: set[str] | None = None,
    session_types: set[str] | None = None,
    limit: int | None = None,
    load_session: Loader | None = None,
    report: Callable[[dict[str, Any]], None] | None = None,
    release_cache: bool = False,
) -> dict[str, Any]:
    paths = DetailedPaths(project, override=detailed_root)
    gate = CorpusGate(paths)
    selected = select_sessions(
        gate.catalog["sessions"],
        season=season,
        from_season=from_season,
        session_ids=session_ids,
        session_types=session_types,
    )
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    work = selected if limit is None else selected[:limit]
    result: dict[str, Any] = {
        "selected": len(selected),
        "scheduled": len(work),
        "attempted": 0,
        "reused": 0,
        "failed": 0,
        "skipped": len(selected) - len(work),
        "sessions": [],
        "provider": "fastf1",
        "openf1": "not-ingested",
    }
    owned = DetailedPaths(project, override=paths.root / "corpus")
    with fastf1_guard(paths.root) as check_permission:
        load = load_session or provider_loader(paths.root)
        for entry in work:
            attempted = False

            def attempt() -> None:
                nonlocal attempted
                attempted = True
                result["attempted"] += 1

            try:
                row = _session(project, gate, owned, entry, load, check_permission, attempt)
                check_permission()
                if release_cache:
                    row["cache_release_state"] = release_owned_session(
                        project, paths, entry["session_id"]
                    )["state"]
                result["reused"] += int(not attempted)
                result["sessions"].append(row)
            except (
                PermissionError,
                CapacityStop,
                RateLimitExceededError,
                CorpusAuthorityStop,
                CleanupStop,
            ):
                raise
            except Exception as exc:
                check_permission()
                result["failed"] += 1
                result["sessions"].append(failure_status(owned, entry["session_id"], exc))
            emit_progress(result["sessions"][-1], report, attempted=attempted)
    result["scope_complete"] = bool(selected) and not result["failed"] and not result["skipped"]
    result["catalog_completed"] = len(select_sessions(gate.catalog["sessions"]))
    result["complete"] = result["scope_complete"] and len(selected) == result["catalog_completed"]
    return result
