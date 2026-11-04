"""Reproducible stratified sampling and storage estimates from completed evidence."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def stratum(season: int, category: str) -> str:
    era = "2018-2020" if season <= 2020 else "2021-2023" if season <= 2023 else "2024-current"
    if category in ("sprint-qualifying", "sprint-shootout"):
        category = "sprint-qualifying-shootout"
    return f"{era}/{category}"


def choose_sample(
    inventory: Iterable[Mapping[str, Any]],
    *,
    existing: set[str],
    seed: int = 2216,
) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for session in inventory:
        if session["state"] == "completed":
            groups[stratum(session["season"], session["session_type"])].append(
                session["session_id"]
            )
    completed = {identity for identifiers in groups.values() for identity in identifiers}
    if not existing.issubset(completed):
        raise ValueError("existing samples must belong to completed inventory")
    chosen = set()
    rng = random.Random(seed)
    for group, identifiers in sorted(groups.items()):
        identifiers = sorted(set(identifiers))
        pinned = sorted(existing.intersection(identifiers))
        target = 1 if group.split("/")[1].startswith("practice-") else 3
        chosen.update(pinned)
        pool = [s for s in identifiers if s not in pinned]
        chosen.update(rng.sample(pool, min(len(pool), max(0, target - len(pinned)))))
    return sorted(chosen)


def _percentile(values: Sequence[int | float], q: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = math.floor(index)
    high = math.ceil(index)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (index - low))


def stratified_projection(
    inventory: Iterable[Mapping[str, Any]],
    samples: Iterable[Mapping[str, Any]],
    *,
    free_bytes: int,
    peak_working_bytes: int,
    headroom_bytes: int = 50 * 1024**3,
    reserve_bytes: int = 20 * 1024**3,
    seed: int = 2216,
    inventory_complete: bool = True,
) -> dict[str, Any]:
    for value in (free_bytes, peak_working_bytes, headroom_bytes, reserve_bytes):
        if type(value) is not int or value < 0:
            raise ValueError("storage limits must be nonnegative integer bytes")
    if headroom_bytes < reserve_bytes:
        raise ValueError("preferred headroom cannot be below the hard reserve")
    counts: Counter[str] = Counter()
    states: Counter[str] = Counter()
    for session in inventory:
        states[session["state"]] += 1
        if session["state"] == "completed":
            counts[stratum(session["season"], session["session_type"])] += 1
    groups: dict[str, list[int]] = defaultdict(list)
    for sample in samples:
        size = sample["bytes"]
        if type(size) is not int or size < 0:
            raise ValueError("sample bytes must preserve exact nonnegative integer measurements")
        groups[stratum(sample["season"], sample["session_type"])].append(size)
    missing = sorted(key for key in counts if not groups[key])
    distributions = {}
    expected = conservative = stress = 0.0
    for key, count in sorted(counts.items()):
        values = groups[key]
        if not values:
            continue
        mean = statistics.mean(values)
        upper = max(values) if len(values) < 10 else _percentile(values, 0.9)
        expected += count * mean
        conservative += count * upper
        stress += count * max(values)
        distributions[key] = {
            "completed_count": count,
            "sample_count": len(values),
            "min": min(values),
            "median": statistics.median(values),
            "mean": mean,
            "p75": _percentile(values, 0.75),
            "p90": _percentile(values, 0.9),
            "p95": _percentile(values, 0.95) if len(values) >= 20 else None,
            "max": max(values),
            "estimate_strength": "weak" if len(values) < 10 else "empirical",
            "conservative_method": "sample maximum" if len(values) < 10 else "empirical p90",
        }
    rng = random.Random(seed)
    bootstrap = [
        sum(
            counts[key] * statistics.mean(rng.choices(groups[key], k=len(groups[key])))
            for key in counts
            if groups[key]
        )
        for _ in range(1000)
    ]
    stress_margin = math.ceil(stress * 1.25)
    remaining = free_bytes - stress_margin - peak_working_bytes
    unverified = states["unverified"] + states["past-unverified"] + states["unknown"]
    complete = inventory_complete and not missing and not unverified and bool(counts)
    return {
        "completed_sessions": states["completed"],
        "unverified_sessions": unverified,
        "inventory_complete": inventory_complete,
        "expected_bytes": math.ceil(expected),
        "conservative_bytes": math.ceil(conservative),
        "stress_bytes": math.ceil(stress),
        "stress_with_margin_bytes": stress_margin,
        "stress_margin": 0.25,
        "bootstrap_seed": seed,
        "bootstrap_repetitions": 1000,
        "bootstrap_95_interval_bytes": [
            _percentile(bootstrap, 0.025),
            _percentile(bootstrap, 0.975),
        ],
        "bootstrap_limit": (
            "conditional on sampled sessions; rare/unobserved extremes are not bounded"
        ),
        "distributions": distributions,
        "missing_strata": missing,
        "weak_strata": sorted(key for key in counts if len(groups[key]) < 10),
        "free_bytes": free_bytes,
        "peak_working_bytes": peak_working_bytes,
        "headroom_bytes": headroom_bytes,
        "reserve_bytes": reserve_bytes,
        "remaining_after_stress_and_peak_bytes": remaining,
        "hard_reserve_safe": complete and remaining >= reserve_bytes,
        "safe_to_sync": complete and remaining >= headroom_bytes,
        "additional_for_preferred_bytes": max(0, headroom_bytes - remaining),
    }
