"""Strict completion evidence and one-to-one provider session identity matching."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from downforce_core.detailed.catalog import canonical_session_type


def classify_completion(
    scheduled_state: str,
    statuses: Iterable[str],
    *,
    supported: bool | None,
    cancelled: bool = False,
    archive_complete: bool = False,
) -> str:
    if scheduled_state == "future":
        return "future"
    if cancelled or scheduled_state == "cancelled":
        return "cancelled"
    values = list(statuses)
    if values and values[-1] in ("Finalised", "Ends"):
        return "completed" if "Started" in values else "abandoned-no-data"
    if values and values[-1] == "Aborted":
        return "abandoned-no-data"
    if archive_complete and "Started" in values:
        return "completed"
    if supported is False:
        return "unsupported"
    return "unverified"


def match_openf1(
    rows: Iterable[Mapping[str, Any]],
    *,
    season: int,
    category: str,
    provider_key: int | None,
    scheduled: datetime | None,
) -> dict[str, Any] | None:
    eligible = [
        r
        for r in rows
        if r.get("year") == season
        and canonical_session_type(season, str(r.get("session_name", ""))) == category
    ]
    if provider_key is not None:
        exact = [r for r in eligible if r.get("session_key") == provider_key]
        if len(exact) == 1:
            return dict(exact[0])
        if len(exact) > 1:
            return None
    # Only a unique same-type session within 15 minutes can be automatically matched.
    nearby = []
    for row in eligible:
        try:
            start = datetime.fromisoformat(str(row["date_start"]).replace("Z", "+00:00"))
            if scheduled is not None and abs((start - scheduled).total_seconds()) <= 900:
                nearby.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return dict(nearby[0]) if len(nearby) == 1 else None


def reclassify_evidence(
    entry: Mapping[str, Any],
    evidence: Mapping[str, Any],
    archive: Mapping[str, Any] | None = None,
    *,
    independent_finalization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revise derived classification without modifying captured provider evidence."""
    state = classify_completion(
        entry["completion"],
        evidence.get("statuses", []),
        supported=entry.get("fastf1_supported"),
        cancelled=bool(evidence.get("cancelled")),
        archive_complete=bool(archive and archive.get("Status") == "Complete"),
    )
    if evidence.get("error") == "SessionNotAvailableError" and state != "future":
        state = "abandoned-no-data"
    if evidence.get("completion_integrity") in {"invalid", "ambiguous"} and state not in {
        "future",
        "cancelled",
    }:
        state = "unverified"
    markers: dict[str, Any] = {}
    if independent_finalization is not None:
        from downforce_core.detailed.independent_finalization import BASIS

        _validate_independent_finalization(entry, evidence, independent_finalization)
        state = "completed"
        markers = {"completion_basis": BASIS, "chronology_valid": False}
    return {
        **evidence,
        "source_classification": evidence.get("source_classification", evidence["state"]),
        "state": state,
        "classification_version": "formal-completion-v2",
        "archive_status": dict(archive) if archive is not None else None,
        **markers,
    }


def _validate_independent_finalization(
    entry: Mapping[str, Any], evidence: Mapping[str, Any], proof: Mapping[str, Any]
) -> None:
    """Check an already replayed proof, never grant a boolean ambiguity exemption."""
    import pandas as pd  # type: ignore[import-untyped]

    from downforce_core.detailed.independent_finalization import (
        BASIS,
        SCHEMA,
        SESSION_ID,
        independent_native_observations,
    )

    expected = {
        "session_id": SESSION_ID,
        "event_id": "2020-01",
        "season": 2020,
        "round_number": 1,
        "event_name": "Austrian Grand Prix",
        "session_type": "practice-1",
        "provider_session_name": "Practice 1",
        "fastf1_slot": 1,
        "fastf1_supported": True,
        "completion": "past-unverified",
    }
    if any(
        entry.get(key) != value or type(entry.get(key)) is not type(value)
        for key, value in expected.items()
    ):
        raise ValueError("independent finality session scope differs")
    try:
        required = {
            "schema_version",
            "session_id",
            "state",
            "basis",
            "completion_integrity",
            "chronology_valid",
            "source_classification",
            "capture_path",
            "discovery_sha256",
            "source_completion_sha256",
            "dependencies",
            "wire",
            "parser",
            "statuses",
            "times",
            "time_nanoseconds",
            "elapsed_before_boundary_seconds",
            "classification",
            "archive",
        }
        if set(proof) != required:
            raise ValueError("independent finality proof schema fields differ")
        dependencies = proof["dependencies"]
        classification = proof["classification"]
        archive = proof["archive"]
        if (
            type(dependencies) is not dict
            or type(classification) is not dict
            or type(archive) is not dict
            or type(proof["capture_path"]) is not str
            or not proof["capture_path"]
            or any(
                type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in (proof["discovery_sha256"], proof["source_completion_sha256"])
            )
            or any(
                type(path) is not str
                or type(digest) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for path, digest in dependencies.items()
            )
            or dependencies.get("inventory.json") != proof["discovery_sha256"]
            or dependencies.get(f"completion/{SESSION_ID}.json")
            != proof["source_completion_sha256"]
            or proof["capture_path"] not in dependencies
            or f"completion-derived/{SESSION_ID}.json" not in dependencies
            or set(classification)
            != {"title", "event_name", "season", "classified_drivers", "rows"}
            or classification.get("title") != "PRACTICE 1 CLASSIFICATION 2020 - OFFICIAL"
            or classification.get("event_name") != "Austrian Grand Prix"
            or classification.get("season") != 2020
            or type(classification.get("season")) is not int
            or classification.get("classified_drivers") != 20
            or type(classification.get("classified_drivers")) is not int
            or type(classification.get("rows")) is not list
            or len(classification["rows"]) != 20
            or archive.get("schema_version") != "captured-archive-finalization-v1"
            or archive.get("session_id") != SESSION_ID
            or archive.get("source_completion_sha256") != proof["source_completion_sha256"]
            or archive.get("http_status") != 200
            or type(archive.get("http_status")) is not int
            or f"archive-finalization/{SESSION_ID}.json" not in dependencies
        ):
            raise ValueError("malformed independent finality proof structure")
        # Builder-native row schema only: HTML authority and dependency replay
        # remain the responsibility of the independent-finalization loader.
        drivers: set[str] = set()
        for rank, row in enumerate(classification["rows"], 1):
            if (
                type(row) is not dict
                or set(row) != {"position", "driver", "team", "laps", "km_h", "time"}
                or any(type(value) is not str or not value.strip() for value in row.values())
            ):
                raise ValueError("independent classification row schema differs")
            lap_time = re.fullmatch(r"(\d+):(\d{2})\.(\d{3})", row["time"])
            if (
                row["position"] != str(rank)
                or row["driver"].casefold() in drivers
                or not re.fullmatch(r"[1-9]\d*", row["laps"])
                or not re.fullmatch(r"[1-9]\d*\.\d+", row["km_h"])
                or lap_time is None
                or int(lap_time[2]) >= 60
                or int(lap_time[1]) * 60_000 + int(lap_time[2]) * 1000 + int(lap_time[3]) <= 0
            ):
                raise ValueError("independent classification row activity differs")
            drivers.add(row["driver"].casefold())
        if (
            any(
                type(archive.get(key)) is not str or not archive[key].strip()
                for key in (
                    "schema_version",
                    "session_id",
                    "url",
                    "body_hex",
                    "body_sha256",
                    "observed_at",
                    "source_completion_sha256",
                    "capture_path",
                    "capture_sha256",
                )
            )
            or not re.fullmatch(r"(?:[0-9a-f]{2})+", archive["body_hex"])
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", archive[key])
                for key in ("body_sha256", "source_completion_sha256", "capture_sha256")
            )
        ):
            raise ValueError("independent archive audited field schema differs")
        scheduled = datetime.fromisoformat(entry["scheduled_at"].replace("Z", "+00:00"))
        if scheduled.tzinfo is None or scheduled >= datetime.now(UTC):
            raise ValueError("independent finality requires a past aware schedule")
        if (
            proof.get("schema_version") != SCHEMA
            or proof.get("session_id") != SESSION_ID
            or proof.get("state") != "completed"
            or proof.get("basis") != BASIS
            or proof.get("chronology_valid") is not False
            or proof.get("source_classification") != "unverified"
            or evidence.get("session_id") != SESSION_ID
            or evidence.get("schema_version") != "derived-completion-v1"
            or evidence.get("state") != "unverified"
            or evidence.get("source_classification") != "unverified"
            or evidence.get("completion_integrity") != "ambiguous"
            or evidence.get("legacy_comparison") != "exact"
            or evidence.get("cancelled")
            or evidence.get("error")
            or any(
                key not in evidence or proof.get(key) != evidence[key]
                for key in (
                    "session_id",
                    "source_completion_sha256",
                    "wire",
                    "parser",
                    "statuses",
                    "times",
                    "completion_integrity",
                )
            )
        ):
            raise ValueError("independent finality differs from strict derived completion")
        frame = pd.DataFrame({"Status": proof["statuses"], "Time": pd.to_timedelta(proof["times"])})
        observations = independent_native_observations(frame, dict(proof))
        nanos = observations["time_nanoseconds"]
        elapsed = proof["elapsed_before_boundary_seconds"]
        if (
            type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or elapsed <= 0
            or elapsed != (nanos[2] - nanos[1]) / 1_000_000_000
        ):
            raise ValueError("independent elapsed interval differs from bound native timestamps")
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("malformed independent finality proof") from exc


def reconcile_provider_inventory(
    canonical: Iterable[Mapping[str, Any]],
    provider_rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Map once; testing, cancelled and future provider-only rows add no corpus cost.

    Unmatched past championship rows are explicitly unresolved, never treated as
    absent coverage. Exact duplicate JSON entries are counted, conflicting keys fail.
    """
    if now.tzinfo is None:
        raise ValueError("reconciliation clock must be timezone aware")
    unique: dict[int, dict[str, Any]] = {}
    duplicates = 0
    for row in provider_rows:
        key = row.get("session_key")
        if type(key) is not int or key < 1:
            raise ValueError("invalid provider session key")
        if key in unique:
            if unique[key] != dict(row):
                raise ValueError("conflicting provider session key")
            duplicates += 1
        else:
            unique[key] = dict(row)
    records = []
    mapped: dict[int, str] = {}
    canonical_ids: set[str] = set()
    for entry in canonical:
        identity = entry["session_id"]
        if identity in canonical_ids:
            raise ValueError("duplicate canonical session identity")
        canonical_ids.add(identity)
        scheduled = (
            datetime.fromisoformat(entry["scheduled_at"]) if entry.get("scheduled_at") else None
        )
        match = match_openf1(
            unique.values(),
            season=entry["season"],
            category=entry["session_type"],
            provider_key=entry.get("timing_key"),
            scheduled=scheduled,
        )
        key = match["session_key"] if match else None
        if key is not None:
            if key in mapped:
                raise ValueError("provider identity maps to multiple canonical sessions")
            mapped[key] = identity
        records.append({**entry, "openf1_session_key": key})
    canonical_states = {row["session_id"]: row["state"] for row in records}
    provider_records = []
    for key, row in unique.items():
        category = canonical_session_type(row["year"], str(row.get("session_name", "")))
        label = str(row.get("session_name", "")).strip().lower()
        if key in mapped:
            state = canonical_states[mapped[key]]
        elif label.startswith("day "):
            state = "testing"
        elif row.get("is_cancelled"):
            state = "cancelled"
        else:
            try:
                start = datetime.fromisoformat(str(row["date_start"]).replace("Z", "+00:00"))
                state = "future" if start >= now else "unmatched-unverified"
            except (KeyError, TypeError, ValueError):
                state = "unmatched-unverified"
        provider_records.append(
            {
                **row,
                "canonical_session_id": mapped.get(key),
                "canonical_session_type": category,
                "reconciliation_state": state,
            }
        )
    return {
        "sessions": records,
        "provider_entries": provider_records,
        "provider_states": dict(Counter(row["reconciliation_state"] for row in provider_records)),
        "duplicate_provider_entries": duplicates,
        "unmatched_unverified": [
            row["session_key"]
            for row in provider_records
            if row["reconciliation_state"] == "unmatched-unverified"
        ],
    }


def finalize_inventory(
    catalog: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    provider_rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    archives: Mapping[str, Mapping[str, Any]] | None = None,
    independent_finalizations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure reconciliation of a complete discovery and captured source evidence."""
    if not catalog.get("complete_inventory"):
        raise ValueError("provider discovery is incomplete")
    identities = [entry["session_id"] for entry in catalog["sessions"]]
    if len(identities) != len(set(identities)) or set(identities) != set(evidence):
        raise ValueError("all and only discovered session evidence is required")
    proofs = independent_finalizations or {}
    if not set(proofs).issubset(identities):
        raise ValueError("independent finality contains undiscovered identities")
    records = []
    for entry in catalog["sessions"]:
        identity = entry["session_id"]
        source = evidence[identity]
        if source.get("session_id") != identity:
            raise ValueError("completion evidence session identity differs")
        revised = reclassify_evidence(
            entry,
            source,
            (archives or {}).get(identity),
            independent_finalization=proofs.get(identity),
        )
        records.append({**entry, **revised})
    mapped = reconcile_provider_inventory(records, provider_rows, now=now)
    # OpenF1's explicit cancellation is additional formal source evidence.
    cancelled = {
        row["canonical_session_id"]
        for row in mapped["provider_entries"]
        if row.get("is_cancelled") and row["canonical_session_id"] is not None
    }
    for row in mapped["sessions"]:
        if row["session_id"] in cancelled and row["state"] != "future":
            if row["session_id"] in proofs:
                raise ValueError("independent finality contradicts provider cancellation")
            row["state"] = "cancelled"
            row["openf1_cancellation_evidence"] = True
    provider_states = {row["session_id"]: row["state"] for row in mapped["sessions"]}
    for row in mapped["provider_entries"]:
        if row["canonical_session_id"] is not None:
            row["reconciliation_state"] = provider_states[row["canonical_session_id"]]
    mapped["provider_states"] = dict(
        Counter(row["reconciliation_state"] for row in mapped["provider_entries"])
    )
    groups = Counter(
        (row["season"], row["session_type"], row["state"]) for row in mapped["sessions"]
    )
    return {
        **mapped,
        "complete": True,
        "classification_version": "formal-completion-v2",
        "summary": dict(Counter(row["state"] for row in mapped["sessions"])),
        "by_season_type_state": [
            {"season": season, "session_type": category, "state": state, "count": count}
            for (season, category, state), count in sorted(groups.items())
        ],
    }
