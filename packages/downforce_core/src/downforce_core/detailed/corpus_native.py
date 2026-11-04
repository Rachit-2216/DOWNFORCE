"""Captured-identity FastF1 loading and exact native Arrow capture, no resampling."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import fastf1  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import requests
from fastf1.events import Event  # type: ignore[import-untyped]
from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]
from fastf1.req import _SessionWithRateLimiting  # type: ignore[import-untyped]

from downforce_core.detailed.evidence import (
    _schedule_rows,
    read_cached_response,
    schedule_sessions,
    schedule_url,
)
from downforce_core.detailed.status import CapacityStop, guard_operation, parquet_peak_bytes
from downforce_core.detailed.study_runner import _path, _read
from downforce_core.detailed.temporal_completion import load_temporal_completions
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.parquet import file_sha256

FATAL = (PermissionError, CapacityStop, RateLimitExceededError)


@contextmanager
def stop_latch(*, offline: bool = False) -> Iterator[Callable[[], None]]:
    """Latch swallowed native quota/capacity/cancellation errors; forbid offline HTTP."""
    original = _SessionWithRateLimiting.send
    http_class = cast(Any, requests.sessions.Session)
    http_send = http_class.send
    failure: BaseException | None = None

    def check() -> None:
        if failure is not None:
            raise failure

    def send(session: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failure
        check()
        try:
            return original(session, *args, **kwargs)
        except BaseException as exc:
            if isinstance(exc, FATAL) or not isinstance(exc, Exception):
                failure = exc
            raise

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal failure
        failure = PermissionError("offline provider restoration attempted network access")
        raise failure

    _SessionWithRateLimiting.send = send
    if offline:
        http_class.send = deny
    try:
        yield check
        check()
    finally:
        _SessionWithRateLimiting.send = original
        http_class.send = http_send
        check()


def construct_session(root: Path, entry: dict[str, Any]) -> Any:
    """Only actual captured schedule rows; never call get_session or official Index."""
    cache = _path(root, "discovery-cache/fastf1_http_cache.sqlite")
    before = file_sha256(cache)
    wire = read_cached_response(cache, schedule_url(entry["season"]))
    body = bytes.fromhex(wire["body_hex"])
    discovered = schedule_sessions(
        body, entry["season"], now=datetime(entry["season"] + 1, 1, 1, tzinfo=UTC)
    )
    identities = [item for item in discovered if item["session_id"] == entry["session_id"]]
    if len(identities) != 1 or any(
        entry.get(key) != identities[0][key] for key in identities[0] if key != "completion"
    ):
        raise ValueError("captured schedule identity differs from canonical entry")
    rows = _schedule_rows(body)
    matching = [row for row in rows if row["RoundNumber"] == entry["round_number"]]
    if len(matching) != 1:
        raise ValueError("captured schedule round is ambiguous or unavailable")
    row = matching[0]
    slot = entry["fastf1_slot"]
    if (
        row["EventName"] != entry["event_name"]
        or row[f"Session{slot}"] != entry["provider_session_name"]
    ):
        raise ValueError("captured schedule identity differs from canonical entry")
    # Rescheduled official times may differ from the frozen schedule, but only
    # a fully replayed temporal source can justify this native Event override.
    if entry.get("completion_temporal_sha256") is not None:
        originals = _read(_path(root, "inventory.json"))["sessions"]
        proofs, hashes = load_temporal_completions(root, originals)
        identity = entry["session_id"]
        if hashes.get(identity) != entry["completion_temporal_sha256"]:
            raise ValueError("native Event lacks exact temporal timing authority")
        official = proofs[identity]["official_session"]
        start = datetime.fromisoformat(official["StartDate"].replace("Z", "+00:00"))
        row = {
            **row,
            f"Session{slot}Date": start,
            f"Session{slot}DateUtc": start.astimezone(UTC).replace(tzinfo=None),
        }
    for key, value in list(row.items()):
        if key == "EventDate" or (key.startswith("Session") and "Date" in key):
            row[key] = pd.Timestamp(value) if value is not None else pd.NaT
    session = Event(row, year=entry["season"]).get_session(slot)
    if session.api_path != "/static/" + entry["timing_path"]:
        raise ValueError("generated native timing path differs from canonical captured identity")
    if file_sha256(cache) != before:
        raise ValueError("captured schedule source changed during native reconstruction")
    return session


def provider_loader(root: Path) -> Callable[[dict[str, Any], Path, bool], Any]:
    def load(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        fastf1.Cache.enable_cache(str(cache))
        fastf1.Cache.offline_mode(offline)
        try:
            session = construct_session(root, entry)
            session.load(laps=True, telemetry=True, weather=True, messages=True)
            return session
        finally:
            cached = fastf1.Cache._requests_session_cached
            if cached is not None:
                cached.close()

    return load


def capture(session: Any, check: Callable[[], None]) -> tuple[dict[str, pa.Table], dict[str, Any]]:
    tables: dict[str, pa.Table] = {}
    availability: dict[str, Any] = {}

    def table(name: str, native: Any) -> None:
        try:
            frame = pd.DataFrame(native).copy()
            if "source_index" in frame:
                raise ValueError("native source_index collision")
            frame.insert(0, "source_index", frame.index)
            tables[name] = pa.Table.from_pandas(frame, preserve_index=False)
            availability[name] = {
                "state": "available" if len(frame) else "unavailable",
                "reason": None if len(frame) else "empty native provider table",
            }
        except FATAL:
            raise
        except Exception as exc:
            availability[name] = {"state": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
        check()

    for name in (
        "results",
        "laps",
        "weather_data",
        "race_control_messages",
        "session_status",
        "track_status",
    ):
        try:
            native = getattr(session, name)
        except FATAL:
            raise
        except Exception as exc:
            availability[name] = {"state": "unavailable", "reason": type(exc).__name__}
        else:
            table(name, native)
        check()
    for name in ("car_data", "pos_data"):
        try:
            partitions = getattr(session, name)
            if not isinstance(partitions, dict):
                raise ValueError("invalid native driver mapping")
            availability[name] = {
                "state": "available" if partitions else "unavailable",
                "reason": None if partitions else "no native driver partitions",
            }
            for driver, native in sorted(partitions.items(), key=lambda item: str(item[0])):
                driver = validate_safe_identifier(str(driver))
                if not driver.isdigit():
                    raise ValueError("invalid native driver number")
                table(f"{name}/{driver}", native)
        except FATAL:
            raise
        except Exception as exc:
            availability[name] = {"state": "unavailable", "reason": type(exc).__name__}
        check()
    try:
        circuit = session.get_circuit_info()
        if circuit is None:
            raise ValueError("no native circuit metadata")
        table("corners", circuit.corners)
    except FATAL:
        raise
    except Exception as exc:
        availability["corners"] = {"state": "unavailable", "reason": type(exc).__name__}
    check()
    for name in ("results", "laps"):
        if name not in tables or tables[name].num_rows == 0:
            raise ValueError(f"required native table unavailable: {name}")
    return tables, availability


def persist_native(directory: Path, tables: dict[str, pa.Table]) -> dict[str, Any]:
    descriptors = {}
    for name, table in tables.items():
        path = directory / f"{name}.parquet"
        guard_operation(
            path, next_peak_bytes=parquet_peak_bytes(table), operation="corpus native capture"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with pq.ParquetFile(path) as reader:
                if not reader.read().equals(table, check_metadata=True):
                    raise ValueError("retained corpus native data differs")
        else:
            with path.open("xb") as handle:
                pq.write_table(table, handle, compression="zstd", use_compliant_nested_type=False)
            with pq.ParquetFile(path) as reader:
                if not reader.read().equals(table, check_metadata=True):
                    raise ValueError("native Parquet round trip differs")
        descriptors[name] = {
            "rows": table.num_rows,
            "schema": str(table.schema),
            "columns": table.column_names,
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }
    return descriptors


def exact_native(original: dict[str, pa.Table], restored: dict[str, pa.Table]) -> None:
    if set(original) != set(restored) or any(
        not table.equals(restored[name], check_metadata=True) for name, table in original.items()
    ):
        raise ValueError("offline restored provider native schema/order/value mismatch")
