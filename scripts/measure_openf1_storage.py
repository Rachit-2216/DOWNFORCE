"""Measure non-duplicated OpenF1 enrichment of frozen preflight or planned samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from downforce_core.detailed.openf1 import (
    OpenF1Client,
    measure_enrichment,
    validated_historical_session,
)
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.detailed.status import guard_operation
from downforce_core.detailed.study_runner import ready_inventory
from downforce_core.storage.layout import write_text_atomic


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = DetailedPaths(Path.cwd(), override=args.root).root
    catalog, _ = ready_inventory(root)
    canonical = {row["session_id"]: row for row in catalog["sessions"]}
    samples = []
    for path in sorted((root / "study" / "session-measurements").glob("*.json")):
        _unlinked_path(path)
        candidate = json.loads(path.read_text())
        identity = candidate["session_id"]
        if path.name != f"{identity}.json" or identity not in canonical:
            raise ValueError("sample measurement has conflicting canonical identity")
        entry = canonical[identity]
        if entry["season"] < 2023:
            continue
        validated_historical_session(root, entry)
        samples.append((path, path.read_bytes(), candidate, entry))
    if not samples:
        return
    # All relevant samples pass replay before the one operation-owned worker is created.
    with OpenF1Client(root) as client:
        for path, original, candidate, entry in samples:
            validated_historical_session(root, entry)
            if path.read_bytes() != original:
                raise ValueError("candidate measurement changed during enrichment")
            report = measure_enrichment(root, entry, client)
            validated_historical_session(root, entry)
            if path.read_bytes() != original:
                raise ValueError("candidate measurement changed during enrichment")
            for key in ("openf1_measured", "openf1_raw_bytes", "openf1_canonical_bytes"):
                candidate[key] = report[key]
            if "openf1_evidence_path" in report:
                candidate["openf1_evidence_path"] = report["openf1_evidence_path"]
            encoded = json.dumps(candidate, indent=2, allow_nan=False)
            _unlinked_path(path)
            guard_operation(
                path,
                next_peak_bytes=2 * len(encoded.encode()) + 4096,
                operation="OpenF1 candidate measurement update",
            )
            write_text_atomic(path, encoded)
            print(
                f"OpenF1 {entry['session_id']}: raw {report['openf1_raw_bytes']}, "
                f"canonical {report['openf1_canonical_bytes']}",
                flush=True,
            )


if __name__ == "__main__":
    main()
