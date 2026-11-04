"""Acquire only a published representative sample; never activate a bulk corpus."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import fastf1  # type: ignore[import-untyped]
from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]

from downforce_core.detailed.evidence import (
    KNOWN_STATUSES,
    _typed_finite_json_equal,
    index_failure_resolutions,
    load_archive_status,
    load_derived_corrections,
    native_status_observations,
)
from downforce_core.detailed.independent_finalization import (
    BASIS,
    independent_native_observations,
    load_independent_finalizations,
)
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import fastf1_guard
from downforce_core.detailed.raw_evidence import source_manifest
from downforce_core.detailed.reconciliation import classify_completion, finalize_inventory
from downforce_core.detailed.sampling import capture_native_tables
from downforce_core.detailed.status import CapacityStop, guard_operation
from downforce_core.detailed.study_io import (
    load_reconciled_inventory,
    publish_sample_plan,
    validated_frozen_plan,
)
from downforce_core.detailed.temporal_completion import (
    SCHEMA as TEMPORAL_SCHEMA,
)
from downforce_core.detailed.temporal_completion import (
    TEMPORAL_MARKERS,
    effective_source_view,
    load_temporal_completions,
    temporal_native_observations,
)
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256

GIB = 1024**3
HEADROOM_BYTES = 50 * GIB
NEXT_PEAK_BYTES = 2 * GIB
JOURNAL_VERSION = "detailed-study-acquisition-v1"
IDENTITY_FIELDS = (
    "session_id",
    "season",
    "round_number",
    "fastf1_slot",
    "session_type",
    "event_name",
    "scheduled_at",
    "fastf1_supported",
)
FINALITY_MARKERS = ("completion_basis", "chronology_valid", "completion_finalization_sha256")
NATIVE_PROOF_MARKERS = (*FINALITY_MARKERS, *TEMPORAL_MARKERS)


def _path(root: Path, relative: str) -> Path:
    path = root / relative
    cursor = path.absolute()
    while True:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("study path must not contain linked directories")
        if cursor == root:
            break
        if root not in cursor.parents:
            raise ValueError("study path escaped detailed root")
        cursor = cursor.parent
    return ensure_contained(path, root, must_exist=False)


def _read(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("study document must be an object")
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def _publish_once(path: Path, value: dict[str, Any]) -> bool:
    if path.exists():
        if _read(path) != value:
            raise ValueError("existing sample measurement differs; it must not be overwritten")
        return False
    _save(path, value)
    return True


def ready_inventory(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Fail closed on stale discovery, unproved completion, or unresolved providers."""
    catalog = load_reconciled_inventory(root)
    if catalog.get("classification_version") != "formal-completion-v2":
        raise ValueError("formal completion reconciliation is required")
    discovery_path = _path(root, "inventory.json")
    discovery = _read(discovery_path)
    if not discovery.get("complete_inventory"):
        raise ValueError("discovery must be complete")
    if catalog.get("discovery_sha256") != file_sha256(discovery_path):
        raise ValueError("formal reconciliation belongs to different discovery")
    originals = {entry["session_id"]: entry for entry in discovery["sessions"]}
    if len(originals) != len(discovery["sessions"]):
        raise ValueError("duplicate discovery session identities")
    rows = {entry["session_id"]: entry for entry in catalog["sessions"]}
    if len(rows) != len(catalog["sessions"]):
        raise ValueError("duplicate canonical session identities")
    if set(rows) != set(originals):
        raise ValueError("formal reconciliation must cover all discovery identities")
    source_hashes = catalog.get("completion_evidence_sha256")
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(originals):
        raise ValueError(
            "original completion source checksum map must cover all and only discovery identities"
        )
    for identity in originals:
        validate_safe_identifier(identity)
        path = _path(root, f"completion/{identity}.json")
        if not path.is_file() or source_hashes[identity] != file_sha256(path):
            raise ValueError("original completion source is missing or its checksum changed")
        if _read(path).get("session_id") != identity:
            raise ValueError("original completion source session identity differs")
    provider_path = _path(root, "openf1-inventory.json")
    if not provider_path.is_file() or catalog.get("openf1_inventory_sha256") != file_sha256(
        provider_path
    ):
        raise ValueError("OpenF1 source inventory is missing or its checksum changed")
    if _read(provider_path).get("failures"):
        raise ValueError("OpenF1 source inventory has unresolved fetch failures")
    corrections, correction_hashes = load_derived_corrections(root, discovery["sessions"])
    if correction_hashes != catalog.get("completion_derived_sha256", {}):
        raise ValueError("derived completion proof checksum binding differs or is missing")
    proofs, proof_hashes = load_independent_finalizations(root, discovery["sessions"])
    if proof_hashes != catalog.get("completion_finalization_sha256", {}):
        raise ValueError("independent finality checksum map differs or is missing")
    temporal, temporal_hashes = load_temporal_completions(root, discovery["sessions"])
    if not _typed_finite_json_equal(temporal_hashes, catalog.get("completion_temporal_sha256", {})):
        raise ValueError("temporal completion checksum map differs or is missing")
    if set(temporal) & (set(proofs) | set(corrections)):
        raise ValueError("temporal and historical completion authorities overlap")
    effective = effective_source_view(discovery["sessions"], temporal, temporal_hashes)
    if proofs or temporal:
        evidence = {
            identity: _read(_path(root, f"completion/{identity}.json")) for identity in originals
        }
        evidence.update(corrections)
        evidence.update({identity: proof["evidence"] for identity, proof in temporal.items()})
        archives = {
            identity: replayed_archive
            for identity in originals
            if (replayed_archive := load_archive_status(root, originals[identity])) is not None
        }
        archives.update({identity: proof["archive"] for identity, proof in temporal.items()})
        assessment = finalize_inventory(
            {**discovery, "sessions": list(effective.values())},
            evidence,
            _read(provider_path)["sessions"],
            now=datetime.now(UTC),
            archives=archives,
            independent_finalizations=proofs,
        )
        assessed = {row["session_id"]: row for row in assessment["sessions"]}
        for identity in proofs.keys() | temporal.keys():
            # JSON equality must distinguish False/0 and integer/float fields,
            # including nested records, when claiming exact replay parity.
            if json.dumps(rows[identity], sort_keys=True, allow_nan=False) != json.dumps(
                assessed[identity], sort_keys=True, allow_nan=False
            ):
                if identity in proofs:
                    raise ValueError(
                        "canonical inventory differs from replayed independent finality"
                    )
                raise ValueError("canonical inventory differs from replayed temporal completion")
        for proof in temporal.values():
            key = proof["provider_session"]["session_key"]
            recorded = [row for row in catalog["provider_entries"] if row.get("session_key") == key]
            replayed = [row for row in assessment["provider_entries"] if row["session_key"] == key]
            if not _typed_finite_json_equal(recorded, replayed):
                raise ValueError("canonical provider mapping differs from temporal source replay")
    for identity, correction in corrections.items():
        if any(
            rows[identity].get(key) != value
            for key, value in correction.items()
            if key not in {"state", "archive_status", "classification_version"}
        ):
            raise ValueError("canonical inventory differs from validated derived completion")
    allowed = {"completed", "future", "cancelled", "unsupported", "abandoned-no-data"}
    for identity, entry in rows.items():
        validate_safe_identifier(identity)
        if identity not in proofs and any(
            key in entry for key in (*FINALITY_MARKERS, "basis", "completion_warning")
        ):
            raise ValueError("canonical independent finality markers lack a replayed proof")
        if identity not in temporal and any(key in entry for key in TEMPORAL_MARKERS):
            raise ValueError("canonical temporal markers lack a replayed proof")
        if (
            identity not in temporal
            and originals[identity].get("completion") == "future"
            and entry.get("state") != "future"
        ):
            raise ValueError("original future session lacks replayed temporal authority")
        if entry.get("state") not in allowed:
            raise ValueError("canonical inventory has unverified sessions")
        if any(entry.get(key) != originals[identity].get(key) for key in IDENTITY_FIELDS):
            raise ValueError("formal reconciliation changed discovery identity")
        if entry["state"] == "future":
            scheduled = datetime.fromisoformat(entry["scheduled_at"])
            if scheduled.tzinfo is None or scheduled < datetime.now(UTC):
                raise ValueError("canonical future exclusion is stale or unverified")
        if entry["state"] == "completed":
            if any(
                type(status) is not str or status not in KNOWN_STATUSES
                for status in entry.get("statuses", [])
            ):
                raise ValueError("completed session lacks formal known-status evidence")
            if identity not in proofs and entry.get("completion_integrity") in {
                "invalid",
                "ambiguous",
            }:
                raise ValueError("completed session has unverified derived completion integrity")
            scheduled = datetime.fromisoformat(entry["scheduled_at"])
            if scheduled.tzinfo is None or scheduled >= datetime.now(UTC):
                raise ValueError("completed session has future or naive scheduled time")
            archive: dict[str, Any] = entry.get("archive_status") or {}
            if archive and identity not in temporal:
                path = _path(root, f"archive-finalization/{identity}.json")
                if (
                    not path.is_file()
                    or catalog.get("archive_evidence_sha256", {}).get(identity) != file_sha256(path)
                    or load_archive_status(root, originals[identity]) != archive
                ):
                    raise ValueError("archive completion proof binding differs")
            formal = classify_completion(
                entry["completion"],
                entry.get("statuses", []),
                supported=entry.get("fastf1_supported"),
                cancelled=bool(entry.get("cancelled")),
                archive_complete=archive.get("Status") == "Complete",
            )
            if (
                entry.get("classification_version") != "formal-completion-v2"
                or formal != "completed"
            ):
                raise ValueError("completed session lacks formal terminal or archive proof")
    blocking, resolutions = index_failure_resolutions(root, catalog)
    if (
        "blocking_index_failures" in catalog
        and catalog["blocking_index_failures"] != blocking
        or "index_failure_resolutions" in catalog
        and catalog["index_failure_resolutions"] != resolutions
    ):
        raise ValueError("index failure resolution is stale")
    if catalog.get("unmatched_unverified") or blocking:
        raise ValueError("provider inventory has unresolved sessions or index failures")
    if not isinstance(catalog.get("provider_entries"), list):
        raise ValueError("provider reconciliation records are required")
    provider_keys = set()
    mapped = set()
    for provider in catalog["provider_entries"]:
        key = provider.get("session_key")
        if type(key) is not int or key in provider_keys:
            raise ValueError("duplicate or invalid provider identity")
        provider_keys.add(key)
        identity = provider.get("canonical_session_id")
        state = provider.get("reconciliation_state")
        if identity is None:
            if state not in {"testing", "future", "cancelled"}:
                raise ValueError("past provider session is not matched or formally excluded")
            if state == "future":
                scheduled = datetime.fromisoformat(
                    str(provider.get("date_start", "")).replace("Z", "+00:00")
                )
                if scheduled.tzinfo is None or scheduled < datetime.now(UTC):
                    raise ValueError("provider future exclusion is stale or unverified")
            elif state == "cancelled" and not provider.get("is_cancelled"):
                raise ValueError("provider cancellation requires explicit source evidence")
            elif state == "testing" and not str(
                provider.get("session_name", "")
            ).strip().lower().startswith("day "):
                raise ValueError("provider testing exclusion differs from source session name")
        elif identity not in rows or identity in mapped or state != rows[identity]["state"]:
            raise ValueError("provider mapping must match one canonical identity and state")
        else:
            mapped.add(identity)
    return catalog, effective


def _plan(root: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    _path(root, "study/sample-plan.json")
    publish_sample_plan(root)
    current, _, plan = validated_frozen_plan(root)
    if plan.get("seed") != 2216 or not _typed_finite_json_equal(current, catalog):
        raise ValueError("frozen plan does not match the completed inventory")
    selected = plan["selected_sessions"]
    identities = [entry["session_id"] for entry in selected]
    if not identities or len(identities) != len(set(identities)):
        raise ValueError("frozen plan contains empty or duplicate sessions")
    rows = {entry["session_id"]: entry for entry in catalog["sessions"]}
    for entry in selected:
        if entry != rows.get(entry["session_id"]) or entry.get("state") != "completed":
            raise ValueError("frozen plan includes a session not in completed inventory")
    existing = {path.parent.name for path in (root / "preflight").glob("*/measurement.json")}
    if not existing.issubset(identities):
        raise ValueError("existing samples must be pinned in the frozen plan")
    return plan


def _space(root: Path, free: Callable[[Path], int], next_peak: int) -> None:
    guard_operation(
        root, next_peak_bytes=next_peak, operation="study next-session operation", free_bytes=free
    )


def _validate_measurement(
    directory: Path,
    value: dict[str, Any],
    original: dict[str, Any],
    *,
    archive_complete: bool = False,
    root: Path | None = None,
) -> None:
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    failures = value.get("failures", [])
    optional_unavailable = all(
        failure == {"channel": "corners", "error_type": "KeyError"} for failure in failures
    )
    if (
        not value.get("completion_confirmed")
        or any(value.get("session", {}).get(key) != original.get(key) for key in IDENTITY_FIELDS)
        or not optional_unavailable
        or not value.get("tables")
    ):
        raise ValueError("existing sample is not a completed native measurement")
    for name, descriptor in value["tables"].items():
        table = _path(directory, f"{name}.parquet")
        if (
            file_sha256(table) != descriptor["sha256"]
            or table.stat().st_size != descriptor["bytes"]
        ):
            raise ValueError("existing native sample checksum changed")
        if name in {
            "results",
            "laps",
            "session_status",
            "track_status",
            "car_data",
        } or name.startswith("car_data/"):
            if pq.read_metadata(table).num_rows != descriptor.get("rows"):
                raise ValueError("existing native sample row count differs")
    tables = value["tables"]
    for name in ("results", "laps", "session_status", "track_status"):
        if name not in tables or tables[name].get("rows", 0) < 1:
            raise ValueError("required native sample channel unavailable")
    if (
        sum(
            table.get("rows", 0)
            for name, table in tables.items()
            if name == "car_data" or name.startswith("car_data/")
        )
        < 1
    ):
        raise ValueError("required native car telemetry unavailable")
    native = pq.read_table(_path(directory, "session_status.parquet"))
    if "Status" not in native.column_names or native.num_rows != tables["session_status"]["rows"]:
        raise ValueError("native completion table schema or row count differs")
    proof, markers = _finality_binding(root, original) if root is not None else (None, {})
    if any(
        (key in value) != (key in markers)
        or type(value.get(key)) is not type(markers.get(key))
        or value.get(key) != markers.get(key)
        for key in NATIVE_PROOF_MARKERS
    ):
        raise ValueError("native measurement independent finality metadata differs")
    if "completion_temporal_sha256" in markers and not _typed_finite_json_equal(
        value.get("session"), original
    ):
        raise ValueError("native temporal measurement effective source entry differs")
    statuses = _native_observations(native.to_pandas(), proof, captured=True)["statuses"]
    if (proof is not None or value.get("session_statuses") is not None) and value.get(
        "session_statuses"
    ) != statuses:
        raise ValueError("native completion differs from measurement metadata")
    if proof is None and (
        classify_completion(
            original["completion"],
            statuses,
            supported=original.get("fastf1_supported"),
            archive_complete=archive_complete,
        )
        != "completed"
    ):
        raise ValueError("existing native sample lacks formal completion proof")


def _finality_binding(
    root: Path, original: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Recheck readiness and replay current dependencies at each native I/O gate."""
    catalog, originals, _ = validated_frozen_plan(root)
    identity = original["session_id"]
    if originals.get(identity) != original:
        raise ValueError("native measurement belongs to different discovery")
    discovery = _read(_path(root, "inventory.json"))
    frozen = discovery["sessions"]
    temporal, temporal_hashes = load_temporal_completions(root, frozen)
    if not _typed_finite_json_equal(temporal_hashes, catalog.get("completion_temporal_sha256", {})):
        raise ValueError("temporal authority changed during native readiness replay")
    if identity in temporal:
        return temporal[identity], {"completion_temporal_sha256": temporal_hashes[identity]}
    proofs, hashes = load_independent_finalizations(root, frozen)
    if hashes != catalog.get("completion_finalization_sha256", {}):
        raise ValueError("independent finality changed during native readiness replay")
    proof = proofs.get(identity)
    return proof, (
        {
            "completion_basis": BASIS,
            "chronology_valid": False,
            "completion_finalization_sha256": hashes[identity],
        }
        if proof is not None
        else {}
    )


def _native_observations(
    frame: Any, proof: dict[str, Any] | None, *, captured: bool = False
) -> dict[str, Any]:
    if proof is not None and proof.get("schema_version") == TEMPORAL_SCHEMA:
        return temporal_native_observations(frame, proof, captured=captured)
    if proof is not None:
        return independent_native_observations(frame, proof, captured=captured)
    return native_status_observations(frame, captured=captured)


def _existing(
    root: Path, original: dict[str, Any], *, archive_complete: bool = False
) -> dict[str, Any] | None:
    directory = _path(root, f"preflight/{original['session_id']}")
    path = _path(root, f"preflight/{original['session_id']}/measurement.json")
    if not path.exists():
        return None
    value = _read(path)
    matches = [
        attempt
        for attempt in directory.glob("*/measurement.json")
        if _read(_path(root, attempt.relative_to(root).as_posix())) == value
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous authoritative native sample attempt")
    _validate_measurement(
        matches[0].parent, value, original, archive_complete=archive_complete, root=root
    )
    return value


def _load_fastf1(entry: dict[str, Any], cache: Path) -> Any:
    fastf1.Cache.enable_cache(str(cache))
    fastf1.Cache.offline_mode(False)
    session = fastf1.get_session(entry["season"], entry["round_number"], entry["fastf1_slot"])
    if (
        entry.get("completion_temporal_sha256") is not None
        and session.api_path != "/static/" + entry["timing_path"]
    ):
        raise ValueError("native provider timing path differs from replayed temporal authority")
    session.load(laps=True, telemetry=True, weather=True, messages=True)
    return session


def run_study(
    project: Path,
    *,
    detailed_root: Path | None = None,
    prepare_only: bool = False,
    load_session: Callable[[dict[str, Any], Path], Any] | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    report: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Resume frozen-plan acquisition using only explicitly owned attempt caches.

    The injectable provider loader returns native session tables, not measurements.
    Journals and failure records never change formal completion evidence. Interrupts
    propagate after the owned attempt is persisted, making the next run resumable.
    """
    root = DetailedPaths(project, override=detailed_root).root
    catalog, originals = ready_inventory(root)
    free = free_bytes or (lambda path: shutil.disk_usage(path).free)
    if not prepare_only:
        _space(root, free, NEXT_PEAK_BYTES)
    plan = _plan(root, catalog)
    plan_path = _path(root, "study/sample-plan.json")
    report(f"Frozen plan: {len(plan['selected_sessions'])} sessions; seed {plan['seed']}")
    for entry in plan["selected_sessions"]:
        report(f"{entry['session_id']} | {entry['event_name']} | {entry['session_type']}")
    summary: dict[str, Any] = {
        "status": "prepared" if prepare_only else "complete",
        "selected": len(plan["selected_sessions"]),
        "attempted": 0,
        "reused": 0,
        "failed": 0,
        "new_writes": 0,
        "failures": [],
    }
    if prepare_only:
        return summary
    loader = load_session or _load_fastf1
    journal_path = _path(root, "study/acquisition-journal.json")
    digest = file_sha256(plan_path)
    journal: dict[str, Any] = (
        _read(journal_path)
        if journal_path.exists()
        else {
            "schema_version": JOURNAL_VERSION,
            "plan_sha256": digest,
            "sessions": {},
        }
    )
    if journal.get("schema_version") != JOURNAL_VERSION or journal.get("plan_sha256") != digest:
        raise ValueError("acquisition journal does not belong to the frozen plan")
    selected_ids = {entry["session_id"] for entry in plan["selected_sessions"]}
    if not set(journal["sessions"]).issubset(selected_ids):
        raise ValueError("acquisition journal includes sessions outside the frozen plan")
    canonical_rows = {entry["session_id"]: entry for entry in catalog["sessions"]}
    existing = {
        identity: _existing(
            root,
            originals[identity],
            archive_complete=(canonical_rows[identity].get("archive_status") or {}).get("Status")
            == "Complete",
        )
        for identity in selected_ids
    }
    largest_known = max(
        (
            sum(table["bytes"] for table in sample["tables"].values())
            for sample in existing.values()
            if sample
        ),
        default=NEXT_PEAK_BYTES,
    )
    known_peak = max(
        NEXT_PEAK_BYTES,
        max(
            (
                sample.get("raw_bytes", 0) * 2
                + sum(table["bytes"] for table in sample["tables"].values())
                for sample in existing.values()
                if sample
            ),
            default=0,
        ),
    )
    # Preflight all saved attempts, including later selected captures, before an
    # earlier retry/new load. Unvalidated recorded size hints never lower reserves.
    retained_raw_max = 0
    for identity, saved in journal["sessions"].items():
        if existing[identity] is not None:
            continue
        relative = saved.get("attempt", "")
        if (
            saved.get("owner") != JOURNAL_VERSION
            or not isinstance(relative, str)
            or not re.fullmatch(rf"preflight/{re.escape(identity)}/study-[0-9a-f]{{32}}", relative)
        ):
            raise ValueError("saved study attempt path or ownership is invalid")
        directory = _path(root, relative)
        if not directory.is_dir():
            raise ValueError("owned study attempt is missing; cannot substitute a new cache")
        cache = _path(root, relative + "/raw-cache")
        raw = source_manifest(cache) if cache.is_dir() else {"files": []}
        raw_bytes = sum(source["bytes"] for source in raw["files"])
        retained_raw_max = max(retained_raw_max, raw_bytes)
        if saved.get("state") == "captured":
            measured = saved["measurement"]
            _validate_measurement(
                directory,
                measured,
                originals[identity],
                archive_complete=(canonical_rows[identity].get("archive_status") or {}).get(
                    "Status"
                )
                == "Complete",
                root=root,
            )
            if raw != saved.get("raw_manifest"):
                raise ValueError("captured raw cache changed before publication")
            largest_known = max(
                largest_known, sum(table["bytes"] for table in measured["tables"].values())
            )
    if journal["sessions"]:
        known_peak = max(known_peak, retained_raw_max * 2 + largest_known)
    with fastf1_guard(root) as permission_check:
        for entry in plan["selected_sessions"]:
            identity = entry["session_id"]
            original = originals[identity]
            if existing[identity] is not None:
                summary["reused"] += 1
                continue
            _space(root, free, known_peak)
            record = journal["sessions"].get(identity)
            if record is None:
                token = "study-" + uuid4().hex
                relative = f"preflight/{identity}/{token}"
                directory = _path(root, relative)
                directory.mkdir(parents=True, exist_ok=False)
                record = {"attempt": relative, "owner": JOURNAL_VERSION, "state": "created"}
                journal["sessions"][identity] = record
                _save(journal_path, journal)
            else:
                relative = record.get("attempt", "")
                if (
                    record.get("owner") != JOURNAL_VERSION
                    or not isinstance(relative, str)
                    or not re.fullmatch(
                        rf"preflight/{re.escape(identity)}/study-[0-9a-f]{{32}}", relative
                    )
                ):
                    raise ValueError("saved study attempt path or ownership is invalid")
                directory = _path(root, relative)
                if not directory.is_dir():
                    raise ValueError(
                        "owned study attempt is missing; cannot substitute a new cache"
                    )
            cache = _path(root, relative + "/raw-cache")
            cache.mkdir(exist_ok=True)
            try:
                if record["state"] == "captured":
                    measurement = record["measurement"]
                    _validate_measurement(
                        directory,
                        measurement,
                        original,
                        archive_complete=(entry.get("archive_status") or {}).get("Status")
                        == "Complete",
                        root=root,
                    )
                    if source_manifest(cache) != record["raw_manifest"]:
                        raise ValueError("captured raw cache changed before publication")
                else:
                    retained_raw_bytes = sum(
                        source["bytes"] for source in source_manifest(cache)["files"]
                    )
                    if retained_raw_bytes:
                        _space(root, free, max(known_peak, retained_raw_bytes * 2 + largest_known))
                    record.update(state="loading")
                    _save(journal_path, journal)
                    summary["attempted"] += 1
                    start = perf_counter()
                    session = loader(original, cache)
                    permission_check()
                    proof, finality_markers = _finality_binding(root, original)
                    statuses = _native_observations(session.session_status, proof)["statuses"]
                    loaded_completion = classify_completion(
                        original["completion"],
                        statuses,
                        supported=original.get("fastf1_supported"),
                        archive_complete=(entry.get("archive_status") or {}).get("Status")
                        == "Complete",
                    )
                    if proof is None and loaded_completion != "completed":
                        raise ValueError(
                            "loaded native session lacks completed terminal or archive proof"
                        )
                    raw = source_manifest(cache)
                    raw_bytes = sum(source["bytes"] for source in raw["files"])
                    known_canonical = max(
                        largest_known,
                        max(
                            (
                                sum(sample["canonical_sample_bytes"].values())
                                for sample in (
                                    r.get("measurement") for r in journal["sessions"].values()
                                )
                                if sample
                            ),
                            default=NEXT_PEAK_BYTES,
                        ),
                    )
                    _space(root, free, max(NEXT_PEAK_BYTES, raw_bytes * 2 + known_canonical))
                    record.update(state="capturing")
                    _save(journal_path, journal)
                    capture = capture_native_tables(session, directory)
                    permission_check()
                    raw = source_manifest(cache)
                    raw_bytes = sum(source["bytes"] for source in raw["files"])
                    measurement = {
                        "session": original,
                        "fastf1_version": fastf1.__version__,
                        "raw_bytes": raw_bytes,
                        **capture,
                        "session_statuses": statuses,
                        "completion_confirmed": True,
                        "elapsed_seconds": perf_counter() - start,
                        "purpose": "frozen representative native storage-study acquisition",
                        **finality_markers,
                    }
                    known_peak = max(
                        known_peak,
                        raw_bytes * 2 + sum(measurement["canonical_sample_bytes"].values()),
                    )
                    record.update(state="captured", measurement=measurement, raw_manifest=raw)
                    _save(journal_path, journal)
                # The published independent measurement must still bind current
                # replayed evidence and exact captured native record order.
                if (
                    entry.get("completion_basis") is not None
                    or entry.get("completion_temporal_sha256") is not None
                ):
                    _validate_measurement(directory, measurement, original, root=root)
                # A captured journal resumed before publication still provides
                # known native/raw sizes for the next selected session's reserve.
                largest_known = max(
                    largest_known, sum(table["bytes"] for table in measurement["tables"].values())
                )
                known_peak = max(
                    known_peak,
                    largest_known
                    + 2 * sum(source["bytes"] for source in record["raw_manifest"]["files"]),
                )
                _publish_once(_path(root, relative + "/measurement.json"), measurement)
                if _publish_once(
                    _path(root, f"preflight/{identity}/measurement.json"), measurement
                ):
                    summary["new_writes"] += 1
                record.update(state="complete")
                _save(journal_path, journal)
                report(f"Measured {identity}")
            except RateLimitExceededError as exc:
                record.update(state="rate-limited", error_type=type(exc).__name__)
                _save(journal_path, journal)
                raise
            except CapacityStop:
                # A disk safety stop is not an ordinary sample failure. Do not
                # attempt failure journals or another selected provider request.
                raise
            except PermissionError as exc:
                # Preserve the first denial; a best-effort journal cannot replace
                # its native WinError/filename, and no later provider work runs.
                record.update(state="permission-denied", error_type=type(exc).__name__)
                try:
                    _save(journal_path, journal)
                except Exception as logging_error:
                    exc.add_note(
                        f"permission-denied journal failed: path={journal_path}; "
                        f"secondary_error={logging_error!r}; "
                        f"winerror={getattr(logging_error, 'winerror', None)}; "
                        f"filename={getattr(logging_error, 'filename', None)}"
                    )
                raise
            except Exception as exc:
                try:
                    permission_check()
                except PermissionError as denied:
                    record.update(state="permission-denied", error_type=type(denied).__name__)
                    try:
                        _save(journal_path, journal)
                    except Exception as logging_error:
                        denied.add_note(
                            f"permission-denied journal failed: path={journal_path}; "
                            f"secondary_error={logging_error!r}; "
                            f"winerror={getattr(logging_error, 'winerror', None)}; "
                            f"filename={getattr(logging_error, 'filename', None)}"
                        )
                    raise
                record.update(state="failed", error_type=type(exc).__name__, error=str(exc))
                _save(journal_path, journal)
                summary["failed"] += 1
                summary["failures"].append(
                    {"session_id": identity, "error_type": type(exc).__name__}
                )
                report(f"Failed {identity}: {type(exc).__name__}")
    if summary["failed"]:
        summary["status"] = "failed"
    _save(_path(root, "study/acquisition-summary.json"), summary)
    return summary
