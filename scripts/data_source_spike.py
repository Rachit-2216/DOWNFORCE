"""Inspect one historical OpenF1 session without persisting provider data."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OPENF1_BASE_URL = "https://api.openf1.org/v1"
DISPLAY_SAMPLE_LIMIT = 2


class SpikeError(RuntimeError):
    """Expected provider or selection failure with an actionable message."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    records: tuple[Mapping[str, object], ...]
    cache_headers: Mapping[str, str]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect the schema of one historical OpenF1 race session."
    )
    parser.add_argument("--provider", choices=("openf1",), default="openf1")
    parser.add_argument("--season", type=int, default=2024)
    parser.add_argument("--event", default="British Grand Prix")
    parser.add_argument("--session", default="Race")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def fetch_records(endpoint: str, params: Mapping[str, object], timeout: float) -> HttpResult:
    url = f"{OPENF1_BASE_URL}/{endpoint}?{urlencode(params)}"
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "DOWNFORCE-Step1-Spike/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS origin
            decoded: Any = json.loads(response.read().decode("utf-8"))
            headers = {
                name: value
                for name in ("cache-control", "age", "etag", "last-modified")
                if (value := response.headers.get(name)) is not None
            }
    except HTTPError as error:
        raise SpikeError(f"OpenF1 returned HTTP {error.code} for {endpoint}.") from error
    except URLError as error:
        raise SpikeError(
            f"Could not reach OpenF1 for {endpoint}: {error.reason}. Network access is required."
        ) from error
    except TimeoutError as error:
        raise SpikeError(f"OpenF1 request timed out for {endpoint}.") from error

    if not isinstance(decoded, list):
        raise SpikeError(f"OpenF1 returned a non-list payload for {endpoint}.")

    records: list[Mapping[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise SpikeError(f"OpenF1 returned an unexpected record for {endpoint}.")
        records.append(item)
    return HttpResult(records=tuple(records), cache_headers=headers)


def normalized(value: object) -> str:
    return " ".join(str(value).casefold().replace("-", " ").split())


def select_record(
    records: Sequence[Mapping[str, object]],
    requested: str,
    fields: Sequence[str],
    resource_name: str,
) -> Mapping[str, object]:
    target = normalized(requested)
    exact_matches = [
        record
        for record in records
        if any(normalized(record.get(field)) == target for field in fields)
    ]
    if exact_matches:
        return exact_matches[0]

    partial_matches = [
        record
        for record in records
        if any(
            target in normalized(record.get(field)) or normalized(record.get(field)) in target
            for field in fields
            if record.get(field)
        )
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]

    available = sorted(
        {
            str(record[field])
            for record in records
            for field in fields
            if record.get(field) is not None
        }
    )
    raise SpikeError(
        f"Could not uniquely select {resource_name} {requested!r}. "
        f"Available values: {', '.join(available)}"
    )


def require_int(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int):
        raise SpikeError(f"Expected integer field {key!r}; received {value!r}.")
    return value


def print_dataset(name: str, result: HttpResult) -> None:
    columns = sorted({key for record in result.records for key in record})
    samples = result.records[:DISPLAY_SAMPLE_LIMIT]
    print(f"\n{name}")
    print(f"  records: {len(result.records)}")
    print(f"  columns: {', '.join(columns) if columns else '(none)'}")
    print(f"  cache headers: {dict(result.cache_headers) or '(none returned)'}")
    print(f"  representative values: {json.dumps(samples, indent=2, default=str)}")


def run_openf1_spike(args: argparse.Namespace) -> None:
    meetings = fetch_records("meetings", {"year": args.season}, args.timeout)
    meeting = select_record(
        meetings.records,
        args.event,
        ("meeting_name", "meeting_official_name", "location", "country_name"),
        "event",
    )
    meeting_key = require_int(meeting, "meeting_key")

    sessions = fetch_records("sessions", {"meeting_key": meeting_key}, args.timeout)
    session = select_record(
        sessions.records,
        args.session,
        ("session_name", "session_type"),
        "session",
    )
    session_key = require_int(session, "session_key")

    print("DOWNFORCE data-source spike")
    print("  provider: OpenF1")
    print(f"  event: {meeting.get('meeting_name')} ({meeting_key})")
    print(f"  session: {session.get('session_name')} ({session_key})")
    print("  persistence: disabled; responses are inspected in memory only")

    print_dataset("meetings", meetings)
    print_dataset("sessions", sessions)
    for endpoint in ("laps", "stints", "weather"):
        result = fetch_records(endpoint, {"session_key": session_key}, args.timeout)
        print_dataset(endpoint, result)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_openf1_spike(args)
    except SpikeError as error:
        print(f"SPIKE FAILED — {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
