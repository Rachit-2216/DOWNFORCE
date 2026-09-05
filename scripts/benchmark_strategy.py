"""Reproducible strategy convergence and latency benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from downforce_core.storage import DownforceRepository
from downforce_core.strategy import ScenarioAssumptions, Strategy, StrategyEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--driver-id", required=True)
    parser.add_argument("--time-ms", type=int, required=True)
    parser.add_argument("--total-laps", type=int, required=True)
    parser.add_argument("--counts", type=int, nargs="+", default=[100, 500, 1_000])
    parser.add_argument("--seed", type=int, default=2216)
    args = parser.parse_args()
    root = args.root.resolve()
    engine = StrategyEngine(DownforceRepository(root), root)
    scenario = ScenarioAssumptions(args.total_laps)
    rows: list[dict[str, object]] = []
    for count in args.counts:
        started = perf_counter()
        result = engine.simulate(
            session_id=args.session_id,
            driver_id=args.driver_id,
            cursor_ms=args.time_ms,
            strategy=Strategy("stay-out", "Stay out"),
            scenario=scenario,
            simulations=count,
            seed=args.seed,
        )
        elapsed = (perf_counter() - started) * 1_000
        rows.append({"simulation_count": count, "elapsed_ms": round(elapsed, 3), "result": result})
    print(json.dumps({"seed": args.seed, "runs": rows}, sort_keys=True))


if __name__ == "__main__":
    main()
