"""Frozen sample plans and measured storage projection inputs."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from downforce_core.detailed.study import choose_sample, stratified_projection, stratum
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256


def load_reconciled_inventory(root: Path) -> dict[str, Any]:
    catalog: dict[str, Any] = json.loads((root / "reconciled-inventory.json").read_text())
    if not catalog.get("complete"):
        raise ValueError("completion reconciliation must be complete before this operation")
    ids = [entry["session_id"] for entry in catalog["sessions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate canonical session identities")
    return catalog


def publish_sample_plan(root: Path, *, seed: int = 2216) -> dict[str, Any]:
    from downforce_core.detailed.status import guard_operation
    from downforce_core.detailed.study_runner import _path

    root = root.absolute()
    destination = _path(root, "study/sample-plan.json")
    if destination.exists():
        original: dict[str, Any] = json.loads(destination.read_text())
        if original["seed"] != seed:
            raise ValueError("sampling plan is frozen; results cannot trigger reselection")
        return original
    inventory_path = _path(root, "reconciled-inventory.json")
    initial_bytes = inventory_path.read_bytes()
    initial_digest = hashlib.sha256(initial_bytes).hexdigest()
    catalog = load_reconciled_inventory(root)
    existing = {path.stem for path in (root / "study" / "frozen").glob("*.json")}
    required = set(existing)
    # Pin well-known wet/interrupted races before any new storage results are seen.
    coverage = {
        (2019, "German Grand Prix"),
        (2020, "Turkish Grand Prix"),
        (2021, "Hungarian Grand Prix"),
        (2023, "Dutch Grand Prix"),
        (2024, "Canadian Grand Prix"),
    }
    for entry in catalog["sessions"]:
        if (
            (entry["season"], entry["event_name"]) in coverage
            and entry["session_type"] == "race"
            and entry["state"] == "completed"
        ):
            required.add(entry["session_id"])
    # The 2021-23 era overlaps OpenF1 only in 2023. Pin one mapped 2023 sample
    # per eligible category before observing sizes, so enrichment projection
    # does not accidentally extrapolate an entirely unmeasured provider era.
    enrichment_groups: dict[str, list[str]] = {}
    for entry in catalog["sessions"]:
        if (
            entry["season"] == 2023
            and entry["state"] == "completed"
            and entry.get("openf1_session_key") is not None
        ):
            enrichment_groups.setdefault(
                stratum(entry["season"], entry["session_type"]), []
            ).append(entry["session_id"])
    rng = random.Random(seed)
    enrichment_required = set()
    for identifiers in (enrichment_groups[key] for key in sorted(enrichment_groups)):
        eligible = sorted(identifiers)
        pinned = sorted(required.intersection(eligible))
        selected_enrichment = pinned[0] if pinned else rng.choice(eligible)
        required.add(selected_enrichment)
        enrichment_required.add(selected_enrichment)
    selected = choose_sample(catalog["sessions"], existing=required, seed=seed)
    by_id = {entry["session_id"]: entry for entry in catalog["sessions"]}
    plan = {
        "schema_version": "detailed-sample-plan-v1",
        "seed": seed,
        "inventory_sha256": initial_digest,
        "inventory_states": dict(Counter(entry["state"] for entry in catalog["sessions"])),
        "selected_before_download": True,
        "selected_at": datetime.now(UTC).isoformat(),
        "reuse_existing": sorted(existing),
        "required_coverage": sorted(required - existing),
        "openf1_coverage_required": sorted(enrichment_required),
        "selection_rule": "one each FP1/FP2/FP3 and three Q/R/S/SQ per era when available; "
        "seeded selection after frozen samples, wet/interrupted-race pins, "
        "and mapped 2023 boundary coverage for OpenF1 enrichment",
        "selected_sessions": [by_id[identity] for identity in selected],
    }
    snapshot = _path(root, f"study/inventory-snapshots/{initial_digest}.json")
    plan_json = json.dumps(plan, indent=2, allow_nan=False)
    next_peak = 2 * len(initial_bytes) + 2 * len(plan_json.encode("utf-8")) + 4096
    if file_sha256(inventory_path) != initial_digest:
        raise ValueError("catalog changed during frozen plan selection")
    if snapshot.exists():
        if snapshot.read_bytes() != initial_bytes:
            raise ValueError("existing initial inventory snapshot differs from exact source bytes")
    else:
        guard_operation(snapshot, next_peak_bytes=next_peak, operation="frozen inventory snapshot")
        write_text_atomic(snapshot, initial_bytes.decode("utf-8"))
    if file_sha256(snapshot) != initial_digest or file_sha256(inventory_path) != initial_digest:
        raise ValueError("catalog or initial inventory snapshot changed before plan publication")
    guard_operation(destination, next_peak_bytes=next_peak, operation="frozen sample plan")
    write_text_atomic(destination, plan_json)
    return plan


def validated_frozen_plan(
    root: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    """Replay current authority, allowing only proven growth of a frozen catalog.

    The initial byte-bound snapshot is provenance, never a readiness substitute.
    No caller-supplied inventory or snapshot can opt out of current authority.
    """
    from downforce_core.detailed.evidence import _typed_finite_json_equal as equal
    from downforce_core.detailed.study_runner import _path, _read, ready_inventory

    inventory_path = _path(root, "reconciled-inventory.json")
    plan_path = _path(root, "study/sample-plan.json")
    inventory_digest = file_sha256(inventory_path)
    plan_digest = file_sha256(plan_path)
    catalog, originals = ready_inventory(root)
    plan = _read(plan_path)
    journal_path = _path(root, "study/acquisition-journal.json")
    if journal_path.exists() and _read(journal_path).get("plan_sha256") != plan_digest:
        raise ValueError("acquisition journal does not belong to the unchanged frozen plan")
    initial_digest = plan.get("inventory_sha256")
    if (
        plan.get("schema_version") != "detailed-sample-plan-v1"
        or plan.get("selected_before_download") is not True
        or not isinstance(initial_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", initial_digest) is None
        or "seed" in plan
        and (type(plan["seed"]) is not int or plan["seed"] != 2216)
    ):
        raise ValueError("invalid frozen sample plan")
    selected = plan.get("selected_sessions")
    if not isinstance(selected, list) or not selected:
        raise ValueError("empty frozen sample selection")
    rows = {row["session_id"]: row for row in catalog["sessions"]}
    selected_ids = [row["session_id"] for row in selected]
    if len(selected_ids) != len(set(selected_ids)) or any(
        entry.get("state") != "completed" or not equal(entry, rows.get(entry["session_id"]))
        for entry in selected
    ):
        raise ValueError("frozen selection differs from current completed inventory")
    if initial_digest != inventory_digest:
        snapshot_path = _path(root, f"study/inventory-snapshots/{initial_digest}.json")
        if not snapshot_path.is_file() or file_sha256(snapshot_path) != initial_digest:
            raise ValueError("initial frozen inventory snapshot is missing or corrupt")
        initial = _read(snapshot_path)
        initial_rows = {row["session_id"]: row for row in initial["sessions"]}
        if (
            type(plan.get("seed")) is not int
            or plan["seed"] != 2216
            or initial.get("complete") is not True
            or len(initial_rows) != len(initial["sessions"])
            or list(initial_rows) != list(rows)
            or not equal(
                plan.get("inventory_states"),
                dict(Counter(row["state"] for row in initial_rows.values())),
            )
            or any(not equal(entry, initial_rows.get(entry["session_id"])) for entry in selected)
        ):
            raise ValueError("frozen plan does not match its initial inventory snapshot")
        selected_at = datetime.fromisoformat(plan["selected_at"])
        if selected_at.tzinfo is None or selected_at > datetime.now(UTC):
            raise ValueError("frozen selection timestamp is invalid")
        dynamic = {
            "sessions",
            "provider_entries",
            "summary",
            "provider_states",
            "by_season_type_state",
            "completion_temporal_sha256",
        }
        if not equal(
            {key: value for key, value in initial.items() if key not in dynamic},
            {key: value for key, value in catalog.items() if key not in dynamic},
        ):
            raise ValueError("frozen source or historical proof bindings changed")
        old_temporal = initial.get("completion_temporal_sha256", {})
        current_temporal = catalog.get("completion_temporal_sha256", {})
        if (
            not isinstance(old_temporal, dict)
            or not isinstance(current_temporal, dict)
            or any(
                not equal(value, current_temporal.get(identity))
                for identity, value in old_temporal.items()
            )
        ):
            raise ValueError("existing temporal proof bindings changed or disappeared")
        promoted = set()
        discovery = _read(_path(root, "inventory.json"))
        discovered = {entry["session_id"]: entry for entry in discovery["sessions"]}
        for identity, original in initial_rows.items():
            current = rows[identity]
            if equal(original, current):
                continue
            source = discovered[identity]
            if (
                original.get("state") != "future"
                or source.get("completion") != "future"
                or current.get("state") != "completed"
                or identity in old_temporal
                or identity not in current_temporal
                or not equal(
                    {key: original.get(key) for key in source if key != "completion"},
                    {key: current.get(key) for key in source if key != "completion"},
                )
            ):
                raise ValueError("catalog is not strict temporal growth of the frozen inventory")
            promoted.add(identity)
        if set(current_temporal) - set(old_temporal) != promoted or not promoted:
            raise ValueError("catalog changes lack newly replayed temporal completion")
        expected_provider = [
            {
                **entry,
                **(
                    {"reconciliation_state": "completed"}
                    if entry.get("canonical_session_id") in promoted
                    else {}
                ),
            }
            for entry in initial["provider_entries"]
        ]
        if not equal(expected_provider, catalog["provider_entries"]):
            raise ValueError("frozen provider mappings changed")
        groups = Counter(
            (row["season"], row["session_type"], row["state"]) for row in rows.values()
        )
        if (
            not equal(catalog.get("summary"), dict(Counter(row["state"] for row in rows.values())))
            or not equal(
                catalog.get("provider_states"),
                dict(Counter(row["reconciliation_state"] for row in expected_provider)),
            )
            or not equal(
                catalog.get("by_season_type_state"),
                [
                    {"season": season, "session_type": category, "state": state, "count": count}
                    for (season, category, state), count in sorted(groups.items())
                ],
            )
        ):
            raise ValueError("current population accounting differs from actual catalog rows")
        coverage = {stratum(row["season"], row["session_type"]) for row in selected}
        modern_coverage = {
            stratum(row["season"], row["session_type"])
            for row in selected
            if row["season"] >= 2023 and row.get("openf1_session_key") is not None
        }
        for row in rows.values():
            if row["state"] != "completed":
                continue
            group = stratum(row["season"], row["session_type"])
            if group not in coverage or (
                row["season"] >= 2023
                and row.get("openf1_session_key") is not None
                and group not in modern_coverage
            ):
                raise ValueError("current eligible stratum lacks frozen sample coverage")
        if file_sha256(snapshot_path) != initial_digest:
            raise ValueError("initial frozen inventory snapshot changed during validation")
    if file_sha256(inventory_path) != inventory_digest or file_sha256(plan_path) != plan_digest:
        raise ValueError("catalog or frozen plan changed during validation")
    return catalog, originals, plan


def project_measured_study(
    root: Path,
    samples: list[dict[str, Any]],
    *,
    free_bytes: int,
) -> dict[str, Any]:
    """Replay authority and require finite sorted JSON-content measurement parity."""
    # These imports stay local because the runner uses plan I/O from this module.
    from downforce_core.detailed.paths import DetailedPaths
    from downforce_core.detailed.proofs import validated_study_measurements
    from downforce_core.detailed.study_runner import _path

    # The API receives a configured data root, not its actual project root. The
    # parent is only a read-only constructor representation; no acquisition or
    # configuration authority is granted here, and all dedicated-root checks stay.
    inventory_path = _path(root, "reconciled-inventory.json")
    plan_path = _path(root, "study/sample-plan.json")
    inventory_digest = file_sha256(inventory_path)
    plan_digest = file_sha256(plan_path)
    actual = validated_study_measurements(DetailedPaths(root.parent, override=root))
    supplied = {sample["session_id"]: sample for sample in samples}
    verified = {sample["session_id"]: sample for sample in actual}
    if len(supplied) != len(samples) or json.dumps(
        supplied, sort_keys=True, allow_nan=False
    ) != json.dumps(verified, sort_keys=True, allow_nan=False):
        raise ValueError("projection requires exact current validated measurements")
    catalog, _, plan = validated_frozen_plan(root)
    selected = plan["selected_sessions"]
    selected_by_id = {entry["session_id"]: entry for entry in selected}
    rows = {entry["session_id"]: entry for entry in catalog["sessions"]}
    if (
        len(selected_by_id) != len(selected)
        or set(selected_by_id) != set(verified)
        or json.dumps(selected_by_id, sort_keys=True, allow_nan=False)
        != json.dumps(
            {identity: rows.get(identity) for identity in selected_by_id},
            sort_keys=True,
            allow_nan=False,
        )
    ):
        raise ValueError("final frozen selection differs from current inventory or measurements")
    projection = _calculate_measured_study(
        catalog["sessions"],
        actual,
        free_bytes=free_bytes,
        selected_ids={entry["session_id"] for entry in selected},
    )
    if file_sha256(inventory_path) != inventory_digest or file_sha256(plan_path) != plan_digest:
        raise ValueError("catalog or frozen plan changed during final projection")
    return projection


def _calculate_measured_study(
    rows: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    *,
    free_bytes: int,
    selected_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Pure storage arithmetic, without authority to acquire or publish anything."""
    by_id = {row["session_id"]: row for row in rows}
    for sample in samples:
        if not sample.get("round_trip_exact") or not sample.get("semantic_identity"):
            raise ValueError("projection requires proven lossless measurements")
        entry = by_id.get(sample["session_id"])
        if (
            entry is None
            or entry["state"] != "completed"
            or any(sample[key] != entry[key] for key in ("season", "session_type"))
        ):
            raise ValueError("sample identity must match completed inventory")
    identities = [sample["session_id"] for sample in samples]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate storage measurements")
    peak = max((sample["temporary_peak_bytes"] for sample in samples), default=0)
    options = {}
    for option_name, fields in {
        "A": ("raw_expanded_bytes", "canonical_original_bytes", "metadata_bytes"),
        "B": ("raw_evidence_bytes", "canonical_bytes", "metadata_bytes"),
    }.items():
        values = [{**sample, "bytes": sum(sample[field] for field in fields)} for sample in samples]
        options[option_name] = stratified_projection(
            rows, values, free_bytes=free_bytes, peak_working_bytes=peak
        )
    component_distributions = {}
    for field in (
        "raw_expanded_bytes",
        "raw_evidence_bytes",
        "canonical_original_bytes",
        "canonical_bytes",
        "metadata_bytes",
    ):
        component_distributions[field] = stratified_projection(
            rows,
            [{**sample, "bytes": sample[field]} for sample in samples],
            free_bytes=free_bytes,
            peak_working_bytes=peak,
        )["distributions"]
    modern = [
        row
        for row in rows
        if row["season"] >= 2023
        and row["state"] == "completed"
        and row.get("openf1_session_key") is not None
    ]
    enrichments = [
        {**sample, "bytes": sample["openf1_raw_bytes"] + sample["openf1_canonical_bytes"]}
        for sample in samples
        if sample.get("openf1_measured")
    ]
    enrichment = stratified_projection(
        modern, enrichments, free_bytes=free_bytes, peak_working_bytes=peak
    )
    enrichment_measured = not modern or not enrichment["missing_strata"]
    unmapped = [
        row["session_id"]
        for row in rows
        if row["season"] >= 2023
        and row["state"] == "completed"
        and row.get("openf1_session_key") is None
        and not row.get("openf1_verified_unavailable")
    ]
    enrichment_measured = enrichment_measured and not unmapped
    planned_complete = selected_ids is None or selected_ids.issubset(identities)
    for option in options.values():
        for scenario in ("expected_bytes", "conservative_bytes", "stress_bytes"):
            option[scenario] += enrichment[scenario]
        option["stress_with_margin_bytes"] = math.ceil(option["stress_bytes"] * 1.25)
        remaining = free_bytes - option["stress_with_margin_bytes"] - peak
        complete = (
            not option["missing_strata"]
            and not option["unverified_sessions"]
            and option["completed_sessions"] > 0
            and enrichment_measured
            and planned_complete
        )
        option["remaining_after_stress_and_peak_bytes"] = remaining
        option["hard_reserve_safe"] = complete and remaining >= option["reserve_bytes"]
        option["safe_to_sync"] = complete and remaining >= option["headroom_bytes"]
        option["additional_for_preferred_bytes"] = max(0, option["headroom_bytes"] - remaining)
        for scenario in ("expected", "conservative", "stress"):
            option[f"remaining_after_{scenario}_bytes"] = free_bytes - option[f"{scenario}_bytes"]
        option["bootstrap_scope"] = (
            "FastF1 storage component only; see separate enrichment interval"
        )
    return {
        **options["B"],
        "options": options,
        "component_distributions": component_distributions,
        "openf1_projection": enrichment,
        "openf1_measured": enrichment_measured,
        "planned_samples_complete": planned_complete,
        "openf1_unmapped_completed": unmapped,
        "measurement_count": len(samples),
        "measurements": samples,
        "chosen_design": "B" if options["B"]["safe_to_sync"] else None,
        "option_C": "No CAS adopted: measured exact file-level savings are negligible",
        "option_D": "Same lossless corpus under an explicitly configured larger data root",
    }
