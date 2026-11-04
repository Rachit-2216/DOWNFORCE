"""Reproduce three saved evidence defects offline; explicitly publish derived proofs.

Default is a read-only inspection. --publish writes only completion-derived and
evidence-repair proofs. Run finalize_detailed_inventory.py --offline separately
after reviewing these outputs. No HTTP client or provider session is constructed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from downforce_core.detailed.evidence import (
    build_index_fallback,
    completion_wire,
    derive_completion,
    publish_derived_completion,
    publish_proof,
    sha256,
)
from downforce_core.detailed.paths import DetailedPaths

TARGETS = {
    "2021-02-practice-1": (208, "8d9ec78062db612016bda5a9014a24323fcebab9b1efbf0ff268b037c3cd8d36"),
    "2020-01-practice-1": (488, "d57d7f11dfc22c1e8fa38610878e08b23b0a5bd919433f8835f8c25418bcbf38"),
}


def repair(root: Path, *, publish: bool = False) -> dict[str, Any]:
    root = DetailedPaths(Path.cwd(), override=root).root
    discovery_path = root / "inventory.json"
    discovery = json.loads(discovery_path.read_bytes())
    entries = {entry["session_id"]: entry for entry in discovery["sessions"]}
    corrections = {}
    findings = []
    for identity, (expected_bytes, expected_sha) in TARGETS.items():
        entry = entries[identity]
        source_path = root / "completion" / (identity + ".json")
        source_bytes = source_path.read_bytes()
        source = json.loads(source_bytes)
        wire = completion_wire(root, entry, source)
        body = bytes.fromhex(wire["body_hex"])
        if len(body) != expected_bytes or sha256(body) != expected_sha:
            raise ValueError("targeted saved wire differs from reviewed evidence")
        proof = derive_completion(entry, source_bytes, wire)
        records = proof["parser"]["records"]
        if identity == "2021-02-practice-1":
            if (
                len(source["statuses"]) != 5
                or len(records) != 6
                or records[-1]["status"] != "Finished"
                or proof["legacy_comparison"] != "missing-nonnewline-final-record"
            ):
                raise ValueError("Imola final-record finding was not reproduced")
            kind = "missing-nonnewline-final-record"
        else:
            if (
                len(source["statuses"]) != 14
                or len(records) != 14
                or records[-1]["status"] != "Started"
                or proof["completion_integrity"] != "ambiguous"
            ):
                raise ValueError("Austria identical-time ambiguity was not reproduced")
            kind = "ambiguous-identical-timestamp-restarts"
        if proof["state"] != "unverified":
            raise ValueError("offline correction must not upgrade unresolved completion")
        corrections[identity] = proof
        findings.append(
            {
                "session_id": identity,
                "kind": kind,
                "source_completion_sha256": sha256(source_bytes),
                "body_sha256": wire["body_sha256"],
                "source_records": len(source["statuses"]),
                "wire_records": len(records),
                "last_status": records[-1]["status"],
                "state": proof["state"],
                "issues": proof["parser"]["issues"],
            }
        )
    fallback = build_index_fallback(root)
    findings.append(
        {
            "year": 2022,
            "kind": "historical-index-http-403",
            "resolution": fallback["resolution"],
            "historical_403_records": fallback["historical_403_records"],
            "verified_sessions": len(fallback["matched_session_ids"]),
            "capabilities": fallback["capabilities"],
        }
    )
    report = {
        "schema_version": "offline-evidence-repair-v1",
        "discovery_sha256": sha256(discovery_path.read_bytes()),
        "findings": findings,
        "corrected_states": {identity: proof["state"] for identity, proof in corrections.items()},
        "acquisition": "unstarted",
        "study_gate": "blocked-by-unverified-sessions",
    }
    if publish:
        # All three findings/proofs have been checked before the first publication.
        for proof in corrections.values():
            publish_derived_completion(root, proof)
        publish_proof(root, "evidence-repair/index-fallback-2022.json", fallback)
        publish_proof(root, "evidence-repair/repair-report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    root = DetailedPaths(Path.cwd(), override=args.root).root
    print(json.dumps(repair(root, publish=args.publish), indent=2, allow_nan=False))
