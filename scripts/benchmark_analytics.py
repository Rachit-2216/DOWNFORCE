"""Measure cold derived-cache load and warm analytics response latency."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from statistics import median
from time import perf_counter

from downforce_core.analytics import (
    AnalyticsEngine,
    AnalyticsEntity,
    AnalyticsQuery,
    ComparisonMode,
    RankingMetric,
)
from downforce_core.archive import HistoricalArchiveStore


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")

    engine = AnalyticsEngine(HistoricalArchiveStore(args.root.resolve()))
    started = perf_counter()
    engine.status()
    cold_ms = (perf_counter() - started) * 1_000
    queries: dict[str, Callable[[], dict[str, object]]] = {
        "season": lambda: engine.season(2024),
        "driver": lambda: engine.driver("hamilton", AnalyticsQuery(2000, 2026)),
        "constructor": lambda: engine.constructor("ferrari", AnalyticsQuery(2000, 2026)),
        "circuit": lambda: engine.circuit("silverstone", AnalyticsQuery(2000, 2026)),
        "race": lambda: engine.race("archive-2024-round-12-race"),
        "comparison": lambda: engine.compare(
            AnalyticsEntity.DRIVER,
            "hamilton",
            "russell",
            AnalyticsQuery(2022, 2024),
            mode=ComparisonMode.COMMON_RACES,
        ),
        "rankings": lambda: engine.rankings(
            AnalyticsEntity.DRIVER,
            RankingMetric.AVERAGE_FINISH,
            AnalyticsQuery(2000, 2026),
            minimum_starts=20,
        ),
    }
    measurements: dict[str, dict[str, float]] = {}
    for name, query in queries.items():
        query()
        samples = []
        for _ in range(args.iterations):
            started = perf_counter()
            query()
            samples.append((perf_counter() - started) * 1_000)
        measurements[name] = {
            "p50_ms": round(median(samples), 3),
            "p95_ms": round(_percentile(samples, 0.95), 3),
        }
    print(
        json.dumps(
            {
                "cold_derived_load_ms": round(cold_ms, 3),
                "iterations": args.iterations,
                "warm": measurements,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
