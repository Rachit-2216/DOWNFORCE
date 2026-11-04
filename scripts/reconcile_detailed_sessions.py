"""Resume formal completion reconciliation without downloading telemetry."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

import fastf1  # type: ignore[import-untyped]
from downforce_core.detailed.catalog import canonical_session_type
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.reconciliation import classify_completion, match_openf1
from downforce_core.storage.layout import write_text_atomic
from fastf1 import _api


def save(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def reconcile(root: Path) -> None:
    catalog = json.loads((root / "inventory.json").read_text())
    openf1 = json.loads((root / "openf1-inventory.json").read_text())["sessions"]
    cache = root / "reconciliation-cache"
    cache.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache))
    evidence_root = root / "completion"
    evidence_root.mkdir(exist_ok=True)
    timing: dict[tuple[int, str, str], dict[str, Any]] = {}
    index_failures = []
    for year in range(catalog["first_year"], catalog["last_year"] + 1):
        path = root / "timing-index" / f"{year}.json"
        if path.exists():
            index = json.loads(path.read_text())
        else:
            try:
                response = fastf1.Cache.requests_get(
                    f"https://livetiming.formula1.com/static/{year}/Index.json", timeout=45
                )
                response.raise_for_status()
                index = json.loads(response.content.decode("utf-8-sig"))
                save(path, index)
            except Exception as exc:
                index_failures.append({"year": year, "error": type(exc).__name__})
                print(
                    f"Timing index {year} unavailable; using configured FastF1 schedule", flush=True
                )
                index = {}
        for meeting in index.get("Meetings", []):
            if not meeting.get("Number") or "test" in meeting.get("Name", "").lower():
                continue
            for session in meeting.get("Sessions", []):
                category = canonical_session_type(year, session.get("Name", ""))
                if category == "unknown":
                    continue
                key = (year, meeting.get("Name", "").strip().casefold(), category)
                if key in timing:
                    if timing[key].get("Path") == session.get("Path"):
                        continue
                    raise ValueError(f"ambiguous official timing identity: {key}")
                timing[key] = session
    records = []
    for number, entry in enumerate(catalog["sessions"], start=1):
        identity = entry["session_id"]
        official = timing.get(
            (entry["season"], entry["event_name"].strip().casefold(), entry["session_type"]), {}
        )
        if not official.get("Path") and entry["completion"] != "future":
            try:
                session = fastf1.get_session(
                    entry["season"], entry["round_number"], entry["fastf1_slot"]
                )
                official = {"Path": session.api_path.removeprefix("/static/")}
            except Exception:
                official = {}
        scheduled = datetime.fromisoformat(entry["scheduled_at"]) if entry["scheduled_at"] else None
        matched = match_openf1(
            openf1,
            season=entry["season"],
            category=entry["session_type"],
            provider_key=official.get("Key"),
            scheduled=scheduled,
        )
        evidence_path = evidence_root / f"{identity}.json"
        if evidence_path.exists():
            evidence = json.loads(evidence_path.read_text())
        else:
            statuses: list[str] = []
            times: list[str] = []
            error: str | None = None
            sample_path = root / "preflight" / identity / "measurement.json"
            if entry["completion"] != "future":
                if sample_path.exists():
                    measured = json.loads(sample_path.read_text())
                    statuses = measured["session_statuses"]
                elif official.get("Path"):
                    relative = official["Path"]
                    if not relative.startswith(f"{entry['season']}/") or ".." in relative:
                        raise ValueError("unsafe provider archive path")
                    while True:
                        try:
                            values = _api.session_status_data("/static/" + relative)
                            statuses = list(values["Status"])
                            times = [str(v) for v in values["Time"]]
                            break
                        except fastf1.req.RateLimitExceededError:
                            # Let the installed provider limiter expire; never reset/bypass it.
                            print(
                                "Provider hourly limit: waiting 60s; progress is persisted",
                                flush=True,
                            )
                            sleep(60)
                        except Exception as exc:
                            error = type(exc).__name__
                            break
                else:
                    error = "MissingOfficialArchivePath"
            state = classify_completion(
                entry["completion"],
                statuses,
                supported=entry["fastf1_supported"],
                cancelled=bool(matched and matched.get("is_cancelled")),
            )
            if error == "SessionNotAvailableError":
                state = "abandoned-no-data"
            evidence = {
                "session_id": identity,
                "state": state,
                "statuses": statuses,
                "times": times,
                "error": error,
                "timing_key": official.get("Key"),
                "timing_path": official.get("Path"),
                "observed_at": datetime.now(UTC).isoformat(),
                "fastf1_version": fastf1.__version__,
            }
            save(evidence_path, evidence)
        records.append(
            {
                **entry,
                **evidence,
                "openf1_session_key": matched.get("session_key") if matched else None,
            }
        )
        if number % 25 == 0 or number == len(catalog["sessions"]):
            summary = dict(Counter(r["state"] for r in records))
            save(
                root / "reconciled-inventory.json",
                {
                    "complete": number == len(catalog["sessions"]),
                    "sessions": records,
                    "summary": summary,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "index_failures": index_failures,
                },
            )
            print(f"Reconciled {number}/{len(catalog['sessions'])}: {summary}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    reconcile(DetailedPaths(Path.cwd(), override=parser.parse_args().root).root)
