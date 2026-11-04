"""Inventory modern provider sessions and measure bounded preflight samples.

This command never performs bulk synchronization or modifies existing corpora.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib.request import urlopen

import fastf1  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
from downforce_core.detailed.audit import inventory, write_sample_table
from downforce_core.detailed.cli import project_study
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.storage.layout import write_text_atomic


def _json(path: Path, value: object) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def discover(root: Path) -> None:
    cache = root / "discovery-cache"
    cache.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache))

    def fetch(year: int) -> list[dict[str, Any]]:
        print(f"Discovering FastF1 {year}", flush=True)
        schedule = fastf1.get_event_schedule(year, include_testing=False, backend="fastf1")
        return list(schedule.to_dict(orient="records"))

    report = inventory(fetch, now=datetime.now(UTC))
    report["fastf1_version"] = fastf1.__version__
    _json(root / "inventory.json", report)
    print(json.dumps(report["summary"]), flush=True)
    openf1: dict[str, Any] = {"sessions": [], "failures": []}
    for year in range(2023, datetime.now(UTC).year + 1):
        try:
            with urlopen(f"https://api.openf1.org/v1/sessions?year={year}", timeout=45) as response:
                rows = json.load(response)
            if not isinstance(rows, list):
                raise ValueError("unexpected provider response")
            openf1["sessions"].extend(rows)
            print(f"OpenF1 {year}: {len(rows)} sessions", flush=True)
        except Exception as exc:
            openf1["failures"].append({"year": year, "error_type": type(exc).__name__})
            print(f"OpenF1 {year}: {type(exc).__name__}", flush=True)
        sleep(2.1)  # below the documented 30 requests/minute historical free limit
    _json(root / "openf1-inventory.json", openf1)


def sample(root: Path, year: int, round_number: int, slot: int) -> None:
    catalog = json.loads((root / "inventory.json").read_text())
    entries = [
        s
        for s in catalog["sessions"]
        if s["season"] == year and s["round_number"] == round_number and s["fastf1_slot"] == slot
    ]
    if len(entries) != 1 or entries[0]["completion"] != "past-unverified":
        raise ValueError("sample must identify one discovered past session")
    entry = entries[0]
    directory = root / "preflight" / entry["session_id"]
    if (directory / "measurement.json").exists():
        print(f"Measured sample already exists: {entry['session_id']}")
        return
    # Incomplete attempts remain evidence; a new attempt uses a distinct directory.
    directory = directory / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    cache = directory / "raw-cache"
    cache.mkdir(parents=True)
    fastf1.Cache.enable_cache(str(cache))
    start = perf_counter()
    session = fastf1.get_session(year, round_number, slot)
    session.load(laps=True, telemetry=True, weather=True, messages=True)
    tables: dict[str, Any] = {}
    failures: list[dict[str, str]] = []

    def write(name: str, frame: Any) -> None:
        try:
            tables[name] = write_sample_table(pd.DataFrame(frame), directory / f"{name}.parquet")
        except Exception as exc:
            failures.append({"channel": name, "error_type": type(exc).__name__})

    for name in (
        "results",
        "laps",
        "weather_data",
        "race_control_messages",
        "session_status",
        "track_status",
    ):
        try:
            write(name, getattr(session, name))
        except Exception as exc:
            failures.append({"channel": name, "error_type": type(exc).__name__})
    for group in ("car_data", "pos_data"):
        try:
            for driver, frame in getattr(session, group).items():
                if not str(driver).isdigit():
                    raise ValueError("invalid provider driver number")
                write(f"{group}/{driver}", frame)
        except Exception as exc:
            failures.append({"channel": group, "error_type": type(exc).__name__})
    try:
        circuit = session.get_circuit_info()
        write("corners", circuit.corners)
    except Exception as exc:
        failures.append({"channel": "corners", "error_type": type(exc).__name__})
    totals: Counter[str] = Counter()
    for name, stats in tables.items():
        totals[name.split("/")[0]] += stats["bytes"]
    raw_bytes = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())
    statuses = [str(v) for v in session.session_status["Status"]]
    measurement = {
        "session": entry,
        "fastf1_version": fastf1.__version__,
        "raw_bytes": raw_bytes,
        "canonical_sample_bytes": dict(totals),
        "tables": tables,
        "failures": failures,
        "session_statuses": statuses,
        "completion_confirmed": "Finalised" in statuses or "Ends" in statuses,
        "elapsed_seconds": perf_counter() - start,
        "purpose": "storage preflight; provider-native columns, not a published canonical revision",
    }
    _json(directory / "measurement.json", measurement)
    _json(directory.parent / "measurement.json", measurement)
    print(json.dumps({k: v for k, v in measurement.items() if k != "tables"}), flush=True)


def project(root: Path) -> None:
    report = project_study(DetailedPaths(Path.cwd(), override=root))
    print(json.dumps(report, indent=2), flush=True)
    for name, option in report["options"].items():
        remaining = option["remaining_after_stress_and_peak_bytes"] / 1024**3
        print(
            f"Option {name}: expected {option['expected_bytes'] / 1024**3:.2f} GiB; "
            f"stress+margin {option['stress_with_margin_bytes'] / 1024**3:.2f} GiB; "
            f"remaining after peak {remaining:.2f} GiB; "
            f"safe={option['safe_to_sync']}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("discover", "sample", "project"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--year", type=int)
    parser.add_argument("--round", dest="round_number", type=int)
    parser.add_argument("--slot", type=int)
    args = parser.parse_args()
    args.root = DetailedPaths(Path.cwd(), override=args.root).root
    if args.action == "discover":
        discover(args.root)
    elif args.action == "project":
        project(args.root)
    elif None in (args.year, args.round_number, args.slot):
        parser.error("sample requires --year, --round and --slot")
    else:
        sample(args.root, args.year, args.round_number, args.slot)


if __name__ == "__main__":
    main()
