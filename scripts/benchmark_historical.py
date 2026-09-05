"""Measure persisted historical replay without provider or network work."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

from downforce_core.replay import ReplayEngine, build_timeline
from downforce_core.storage import CanonicalTableName, DownforceRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--lap", type=int, default=20)
    parser.add_argument("--iterations", type=int, default=25)
    return parser


def _milliseconds(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 3)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.iterations < 1:
        raise ValueError("iterations must be positive")

    repository = DownforceRepository(args.root.resolve())

    load_started = perf_counter()
    session = repository.load_session(args.session_id)
    events = repository.load_events(args.session_id)
    normalized_session_load_ms = _milliseconds(load_started)

    timeline_started = perf_counter()
    build_timeline(session)
    rebuilt_timeline_ms = _milliseconds(timeline_started)

    persisted_started = perf_counter()
    persisted_engine = ReplayEngine.from_repository(repository, args.session_id)
    persisted_engine_load_ms = _milliseconds(persisted_started)

    cold_started = perf_counter()
    cold_state = persisted_engine.state_at_lap(args.lap, phase="end")
    cold_state_at_lap_ms = _milliseconds(cold_started)

    warm_measurements: list[float] = []
    for _ in range(args.iterations):
        warm_started = perf_counter()
        persisted_engine.state_at(cold_state.session_time_ms)
        warm_measurements.append((perf_counter() - warm_started) * 1_000)

    manifest = repository.load_manifest(args.session_id)
    event_count = manifest.tables[CanonicalTableName.EVENTS.value].row_count
    output = {
        "session_id": manifest.session_id,
        "event_count": event_count,
        "normalized_session_load_ms": normalized_session_load_ms,
        "rebuilt_timeline_ms": rebuilt_timeline_ms,
        "persisted_engine_load_ms": persisted_engine_load_ms,
        "cold_state_at_lap_ms": cold_state_at_lap_ms,
        "warm_state_query_ms": {
            "iterations": args.iterations,
            "minimum": round(min(warm_measurements), 3),
            "mean": round(sum(warm_measurements) / len(warm_measurements), 3),
            "maximum": round(max(warm_measurements), 3),
        },
        "persisted_events_verified": len(events),
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
