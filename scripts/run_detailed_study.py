"""Prepare or resume only frozen representative samples; never bulk-sync data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import sleep

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.study_runner import ready_inventory, run_study


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--wait",
        action="store_true",
        help="wait in at most 60-second chunks for formal complete reconciliation",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="validate formal reconciliation and publish/report plan without provider requests",
    )
    args = parser.parse_args(argv)
    try:
        root = DetailedPaths(Path.cwd(), override=args.root).root
        if args.wait:
            while True:
                try:
                    ready_inventory(root)
                    break
                except (FileNotFoundError, ValueError) as exc:
                    print(f"Waiting without provider requests: {exc}", flush=True)
                    sleep(60)
        result = run_study(Path.cwd(), detailed_root=args.root, prepare_only=args.prepare_only)
    except PermissionError as exc:
        print(f"Stopped: provider or filesystem permission denied: {exc}", file=sys.stderr)
        return 3
    except (ValueError, OSError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False), flush=True)
    return 0 if result["status"] in {"complete", "prepared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
