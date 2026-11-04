"""Finalize saved completion evidence after the discovery worker finishes.

The legacy worker is never restarted to reset its limiter. New network work
inherits a conservative full cooldown from its last persisted observation.
Only the two unresolved formal archive-finalisation documents are requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

import fastf1  # type: ignore[import-untyped]
from downforce_core.detailed.evidence import (
    index_failure_resolutions,
    load_archive_status,
    load_derived_corrections,
)
from downforce_core.detailed.independent_finalization import load_independent_finalizations
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import fastf1_guard
from downforce_core.detailed.rate_budget import RequestBudget
from downforce_core.detailed.reconciliation import finalize_inventory, reclassify_evidence
from downforce_core.detailed.temporal_completion import (
    effective_source_view,
    load_temporal_completions,
)
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256


def save(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def finalize(root: Path, *, offline: bool = False) -> dict[str, Any]:
    discovery_path = root / "inventory.json"
    discovery = json.loads(discovery_path.read_text())
    checkpoint = root / "reconciled-inventory.json"
    while not checkpoint.exists() or not json.loads(checkpoint.read_text()).get("complete"):
        if offline:
            raise ValueError("the source completion worker has not finished")
        print(
            "Waiting for persisted completion reconciliation; no new provider requests", flush=True
        )
        sleep(60)
    prior = json.loads(checkpoint.read_text())
    evidence = {
        entry["session_id"]: json.loads(
            (root / "completion" / f"{entry['session_id']}.json").read_text()
        )
        for entry in discovery["sessions"]
    }
    corrections, correction_hashes = load_derived_corrections(root, discovery["sessions"])
    evidence.update(corrections)
    proofs, proof_hashes = load_independent_finalizations(root, discovery["sessions"])
    temporal, temporal_hashes = load_temporal_completions(root, discovery["sessions"])
    if set(temporal) & (set(proofs) | set(corrections)):
        raise ValueError("temporal and historical completion authorities overlap")
    effective = effective_source_view(discovery["sessions"], temporal, temporal_hashes)
    effective_discovery = {**discovery, "sessions": list(effective.values())}
    evidence.update({identity: proof["evidence"] for identity, proof in temporal.items()})
    archives: dict[str, dict[str, Any]] = {}
    unresolved = []
    for entry in effective_discovery["sessions"]:
        identity = entry["session_id"]
        source_path = root / "archive-finalization" / f"{identity}.json"
        if source_path.exists():
            archive = load_archive_status(root, entry)
            if archive is not None:
                archives[identity] = archive
        if identity in temporal:
            archives[identity] = temporal[identity]["archive"]
        state = reclassify_evidence(
            entry,
            evidence[identity],
            archives.get(identity),
            independent_finalization=proofs.get(identity),
        )["state"]
        if state == "unverified" and not source_path.exists():
            unresolved.append(entry)
    if unresolved and not offline:
        budget_path = root / "runtime" / "fastf1-budget.json"
        cache = root / "reconciliation-cache"
        with fastf1_guard(root):
            if not budget_path.exists():
                observations = [
                    datetime.fromisoformat(source["observed_at"]) for source in evidence.values()
                ]
                not_before = max(observations).timestamp() + 3600
                RequestBudget(budget_path).defer_until(not_before)
                print(
                    "New worker inherits a full conservative provider cooldown until "
                    + datetime.fromtimestamp(not_before, UTC).isoformat(),
                    flush=True,
                )
            fastf1.Cache.enable_cache(str(cache))
            for entry in unresolved:
                identity = entry["session_id"]
                relative = evidence[identity].get("timing_path")
                if not isinstance(relative, str) or not relative.startswith(f"{entry['season']}/"):
                    continue  # unknown support stays unverified; do not invent an archive path
                if ".." in relative or ":" in relative or "\\" in relative:
                    raise ValueError("unsafe provider archive path")
                url = "https://livetiming.formula1.com/static/" + relative + "ArchiveStatus.json"
                response = fastf1.Cache.requests_get(url, timeout=45)
                body = response.content
                if response.status_code not in (200, 404):
                    response.raise_for_status()
                record = {
                    "session_id": identity,
                    "url": url,
                    "http_status": response.status_code,
                    "body_hex": body.hex(),
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "source_completion_sha256": file_sha256(
                        root / "completion" / f"{identity}.json"
                    ),
                }
                save(root / "archive-finalization" / f"{identity}.json", record)
                if response.status_code == 200:
                    archives[identity] = json.loads(body.decode("utf-8-sig"))
                print(f"Formal archive {identity}: HTTP {response.status_code}", flush=True)
    provider_path = root / "openf1-inventory.json"
    provider_inventory = json.loads(provider_path.read_text())
    if provider_inventory.get("failures"):
        raise ValueError("OpenF1 inventory has unresolved provider fetch failures")
    result = finalize_inventory(
        effective_discovery,
        evidence,
        provider_inventory["sessions"],
        now=datetime.now(UTC),
        archives=archives,
        independent_finalizations=proofs,
    )
    result.update(
        discovery_sha256=file_sha256(discovery_path),
        openf1_inventory_sha256=file_sha256(provider_path),
        completion_evidence_sha256={
            identity: file_sha256(root / "completion" / f"{identity}.json") for identity in evidence
        },
        archive_evidence_sha256={
            identity: file_sha256(root / "archive-finalization" / f"{identity}.json")
            for identity in archives
            if identity not in temporal
        },
        index_failures=prior.get("index_failures", []),
        completion_derived_sha256=correction_hashes,
        completion_finalization_sha256=proof_hashes,
        completion_temporal_sha256=temporal_hashes,
    )
    reports = {}
    fallback = root / "evidence-repair" / "index-fallback-2022.json"
    if fallback.exists():
        reports["2022"] = {
            "path": "evidence-repair/index-fallback-2022.json",
            "sha256": file_sha256(fallback),
        }
    result["index_fallback_reports"] = reports
    blocking, resolutions = index_failure_resolutions(root, result)
    result.update(blocking_index_failures=blocking, index_failure_resolutions=resolutions)
    # Source observation timestamps remain in source documents; do not churn the
    # inventory digest simply because an identical offline finalization is rerun.
    save(checkpoint, result)
    save(
        root / "provider-reconciliation.json",
        {
            "states": result["provider_states"],
            "provider_entries": result["provider_entries"],
            "duplicate_provider_entries": result["duplicate_provider_entries"],
            "unmatched_unverified": result["unmatched_unverified"],
        },
    )
    print(
        f"Finalized {len(result['sessions'])} canonical sessions: {result['summary']}", flush=True
    )
    print(f"OpenF1 reconciliation: {dict(Counter(result['provider_states']))}", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    finalize(DetailedPaths(Path.cwd(), override=args.root).root, offline=args.offline)
