"""Capture only the three reviewed official requests; never promote completion.

Run once in a fresh process. FastF1 3.8.3 Cache.requests_get automatically
initializes a SQLite cache, so this adapter uses its existing uncached native
session directly under fastf1_guard. Native limiters and durable request history
remain unchanged. No redirects, retries, alternate hosts, or arbitrary URLs.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import fastf1  # type: ignore[import-untyped]
from downforce_core.detailed.evidence import (
    MAX_BODY_BYTES,
    _unique_object,
    publish_proof,
    sha256,
    status_url,
)
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import _unlinked_path, fastf1_guard
from downforce_core.domain.identifiers import validate_safe_identifier
from fastf1.req import _SessionWithRateLimiting  # type: ignore[import-untyped]

# Paths are validated against these reviewed identities, then URLs are derived
# from the original completion bytes, never from a user-supplied URL.
TARGETS = {
    "2021-02-practice-1": {
        "season": 2021,
        "round_number": 2,
        "event_id": "2021-02",
        "event_name": "Emilia Romagna Grand Prix",
        "circuit_name": "Imola",
        "date": "2021-04-16",
        "timing_path": "2021/2021-04-18_Emilia_Romagna_Grand_Prix/2021-04-16_Practice_1/",
    },
    "2020-01-practice-1": {
        "season": 2020,
        "round_number": 1,
        "event_id": "2020-01",
        "event_name": "Austrian Grand Prix",
        "circuit_name": "Spielberg",
        "date": "2020-07-03",
        "timing_path": "2020/2020-07-05_Austrian_Grand_Prix/2020-07-03_Practice_1/",
    },
}
REQUESTS = (
    ("2021-02-practice-1", "ArchiveStatus.json"),
    ("2020-01-practice-1", "ArchiveStatus.json"),
    ("2020-01-practice-1", "SessionStatus.jsonStream"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_source(root: Path, relative: str) -> bytes:
    path = _unlinked_path(root / relative)
    if path.stat().st_size > MAX_BODY_BYTES:
        raise ValueError("source document exceeds capture bound")
    return path.read_bytes()


def _plan(root: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    sources = {"inventory.json": _read_source(root, "inventory.json")}
    inventory = json.loads(sources["inventory.json"], object_pairs_hook=_unique_object)
    entries = inventory["sessions"]
    plans = {}
    for identity, expected in TARGETS.items():
        matches = [entry for entry in entries if entry.get("session_id") == identity]
        if len(matches) != 1:
            raise ValueError("discovery session identity is missing or duplicate")
        entry = matches[0]
        if any(
            entry.get(key) != expected[key]
            for key in ("season", "round_number", "event_id", "event_name", "circuit_name")
        ) or any(
            entry.get(key) != value
            for key, value in {
                "session_type": "practice-1",
                "provider_session_name": "Practice 1",
                "fastf1_slot": 1,
                "provider": "fastf1",
            }.items()
        ):
            raise ValueError("discovery session identity differs from reviewed target")
        scheduled = datetime.fromisoformat(entry["scheduled_at"])
        if scheduled.tzinfo is None or scheduled.date().isoformat() != expected["date"]:
            raise ValueError("discovery scheduled identity differs from reviewed target")
        relative = f"completion/{identity}.json"
        sources[relative] = _read_source(root, relative)
        source = json.loads(sources[relative], object_pairs_hook=_unique_object)
        if source.get("session_id") != identity:
            raise ValueError("original completion identity differs")
        if source.get("timing_path") != expected["timing_path"]:
            raise ValueError("original completion timing path differs from reviewed target")
        plans[identity] = {
            "session_id": identity,
            "source_completion_path": relative,
            "source_completion_sha256": sha256(sources[relative]),
            "status_url": status_url(entry, source),
        }
    requests = []
    for identity, resource in REQUESTS:
        plan = plans[identity]
        requests.append(
            {
                **{key: value for key, value in plan.items() if key != "status_url"},
                "resource": resource,
                "request_url": plan["status_url"].removesuffix("SessionStatus.jsonStream")
                + resource,
            }
        )
    return requests, sources


def capture(root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    """One bounded operation; every captured response precedes the next request."""
    root = DetailedPaths(Path.cwd(), override=root).root
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz-") + uuid4().hex
    validate_safe_identifier(run_id)
    prefix = f"evidence-requests/{run_id}"
    directory = _unlinked_path(root / prefix)
    directory.mkdir(parents=True, exist_ok=False)  # Never reuse an existing run.
    started = _now()
    planned: list[dict[str, Any]] = []
    saved: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    sources_manifest: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None

    def publish(name: str, value: dict[str, Any]) -> None:
        publish_proof(root, f"{prefix}/{name}", value)

    def observe(response: Any) -> None:
        if active is None:
            raise ValueError("unplanned provider response")
        record = {
            "schema_version": "official-response-evidence-v1",
            **active,
            "actual_url": response.url,
            "prepared_request_url": response.request.url,
            "http_status": response.status_code,
            "headers": dict(response.headers),
            "observed_at": _now(),
        }
        body_failure: BaseException | None = None
        try:
            body = response.content
            if type(body) is not bytes:
                raise ValueError("provider response body must be exact bytes")
            record.update(
                body_available=True,
                body_hex=body.hex(),
                body_bytes=len(body),
                body_sha256=sha256(body),
            )
        except BaseException as exc:
            body_failure = exc
            record.update(
                body_available=False,
                body_error={"type": type(exc).__name__, "message": str(exc)},
            )
        name = f"response-{len(saved) + 1:02}.json"
        publish(name, record)
        saved.append(
            {
                **active,
                "path": f"{prefix}/{name}",
                "sha256": sha256((directory / name).read_bytes()),
                "http_status": response.status_code,
            }
        )
        if body_failure is not None:
            raise body_failure
        # Preserve the complete body even for an unexpected oversized response;
        # then fail closed, rather than publish a misleading truncated capture.
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("captured response exceeds expected evidence size")
        if response.url != active["request_url"] or response.request.url != active["request_url"]:
            raise ValueError("provider response URL differs from fixed request identity")

    metadata = {
        "schema_version": "official-evidence-request-run-v1",
        "run_id": run_id,
        "started_at": started,
        "fastf1_version": fastf1.__version__,
        "transport": "FastF1 existing uncached native session under durable fastf1_guard",
        "allow_redirects": False,
        "timeout_seconds": 45,
        "completion_promotion": False,
    }
    error: BaseException | None = None
    try:
        planned, sources = _plan(root)
        for number, (relative, body) in enumerate(sources.items(), 1):
            name = f"source-{number:02}.json"
            publish(
                name,
                {
                    "source_path": relative,
                    "body_hex": body.hex(),
                    "body_bytes": len(body),
                    "body_sha256": sha256(body),
                },
            )
            sources_manifest.append(
                {"source_path": relative, "path": f"{prefix}/{name}", "body_sha256": sha256(body)}
            )
        publish(
            "intent.json", {**metadata, "planned_requests": planned, "sources": sources_manifest}
        )
        cache = fastf1.Cache
        if (
            cache._CACHE_DIR is not None
            or cache._requests_session_cached is not None
            or type(cache._requests_session) is not _SessionWithRateLimiting
        ):
            raise ValueError("capture requires a fresh uncached FastF1 process")
        with fastf1_guard(root, response_observer=observe) as check_permission:
            for active in planned:
                check_permission()
                attempts.append({**active, "attempted_at": _now()})
                response = cache._requests_session.get(
                    active["request_url"], allow_redirects=False, timeout=45
                )
                try:
                    check_permission()
                    if response.status_code not in (200, 404):
                        raise ValueError(f"provider operation stopped: HTTP {response.status_code}")
                finally:
                    response.close()
    except BaseException as exc:
        error = exc
        raise
    finally:
        outcome = {
            **metadata,
            "finished_at": _now(),
            "run_path": str(directory),
            "outcome": "completed" if error is None else "failed",
            "attempts": attempts,
            "responses": saved,
            "error": None
            if error is None
            else {
                "type": type(error).__name__,
                "message": str(error),
                "cause_type": type(error.__cause__).__name__ if error.__cause__ else None,
                "cause_message": str(error.__cause__) if error.__cause__ else None,
            },
        }
        publish(
            "manifest.json",
            {
                **metadata,
                "planned_requests": planned,
                "requests": saved,
                "sources": sources_manifest,
            },
        )
        publish("outcome.json", outcome)
    return outcome


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = DetailedPaths(Path.cwd(), override=args.root).root
    print(json.dumps(capture(root), indent=2, allow_nan=False))
