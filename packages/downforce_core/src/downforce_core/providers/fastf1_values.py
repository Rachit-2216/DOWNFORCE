"""Provider-local scalar and metadata conversion for the FastF1 adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Protocol, cast

import pandas as pd  # type: ignore[import-untyped]

from downforce_core.providers.base import SessionRef


class _StringLookup(Protocol):
    def get(self, key: str, default: object = None) -> object: ...


def extract_metadata(
    session: object,
    reference: SessionRef,
    *,
    load_laps: bool,
    load_telemetry: bool,
    warnings: list[str],
) -> tuple[dict[str, object], datetime | None]:
    """Copy documented scalar metadata out of a loaded FastF1 session."""

    metadata: dict[str, object] = {
        "season": reference.season,
        "requested_event": reference.event,
    }
    event, event_error = read_attribute(session, "event")
    if event_error is not None:
        warnings.append(f"FastF1 event metadata unavailable: {exception_detail(event_error)}")
        event = None
    session_info, info_error = read_attribute(session, "session_info")
    if info_error is not None:
        warnings.append(f"FastF1 session metadata unavailable: {exception_detail(info_error)}")
        session_info = None

    meeting = _lookup(session_info, "Meeting")
    country = _lookup(meeting, "Country")
    circuit = _lookup(meeting, "Circuit")
    scalar_candidates: tuple[tuple[str, object], ...] = (
        (
            "event_name",
            _first_present(
                _lookup(event, "OfficialEventName"),
                _lookup(meeting, "OfficialName"),
                _lookup(event, "EventName"),
            ),
        ),
        ("session_name", safe_attribute_value(session, "name")),
        (
            "round_number",
            _first_present(
                _lookup(event, "RoundNumber"),
                reference.event if isinstance(reference.event, int) else None,
            ),
        ),
        ("country_code", _lookup(country, "Code")),
        (
            "country_name",
            _first_present(_lookup(country, "Name"), _lookup(event, "Country")),
        ),
        (
            "circuit_name",
            _first_present(_lookup(circuit, "ShortName"), _lookup(circuit, "Name")),
        ),
        (
            "location",
            _first_present(_lookup(meeting, "Location"), _lookup(event, "Location")),
        ),
        (
            "provider_source_id",
            _first_present(
                safe_attribute_value(session, "api_path"),
                _lookup(session_info, "Key"),
            ),
        ),
    )
    for key, candidate in scalar_candidates:
        scalar = _metadata_scalar(candidate)
        if scalar is not None:
            metadata[key] = scalar

    scheduled = _utc_datetime_or_none(safe_attribute_value(session, "date"))
    if scheduled is not None:
        metadata["scheduled_start_utc"] = scheduled

    session_origin: datetime | None = None
    # FastF1 3.8.3 creates ``t0_date`` only while loading telemetry. Do not probe the
    # property after lap-only loads: it raises DataNotLoadedError and absence is expected.
    if load_telemetry:
        origin_value, origin_error = read_attribute(session, "t0_date")
        if origin_error is not None:
            warnings.append(f"FastF1 session origin unavailable: {exception_detail(origin_error)}")
        else:
            session_origin = _utc_datetime_or_none(origin_value)
            if session_origin is not None:
                metadata["session_origin_utc"] = session_origin
            else:
                warnings.append("FastF1 session origin unavailable: t0_date was missing or invalid")

    if load_laps:
        start_value, start_error = read_attribute(session, "session_start_time")
        if start_error is not None:
            warnings.append(
                f"FastF1 session start time unavailable: {exception_detail(start_error)}"
            )
        else:
            start_duration = _duration_iso_or_none(start_value)
            if start_duration is not None:
                metadata["session_start_time"] = start_duration
            start_milliseconds = _duration_milliseconds_or_none(start_value)
            if start_milliseconds is not None:
                metadata["session_start_time_ms"] = start_milliseconds
        total_value, total_error = read_attribute(session, "total_laps")
        if total_error is None:
            total_laps = _integer_or_none(total_value)
            if total_laps is not None:
                metadata["total_laps"] = total_laps

    api_value = safe_attribute_value(session, "f1_api_support")
    if type(api_value) is bool:
        metadata["f1_api_support"] = api_value
    else:
        warnings.append("FastF1 did not expose a boolean f1_api_support value")
    if reference.season >= 2020:
        metadata["coordinate_scale_to_m"] = 0.1
    return metadata, session_origin


def read_attribute(target: object, attribute: str) -> tuple[object, Exception | None]:
    """Read a provider property without allowing its exception to escape implicitly."""

    try:
        value: object = getattr(target, attribute)
    except Exception as exc:
        return None, exc
    return value, None


def safe_attribute_value(target: object, attribute: str) -> object:
    value, error = read_attribute(target, attribute)
    return None if error is not None else value


def exception_detail(error: BaseException) -> str:
    message = clean_message(str(error))
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def clean_message(message: str) -> str:
    return " ".join(message.split()).strip()


def _lookup(container: object, key: str) -> object:
    if container is None:
        return None
    try:
        return cast(_StringLookup, container).get(key, None)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _first_present(*values: object) -> object:
    for value in values:
        if not _is_missing(value):
            return value
    return None


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        missing: object = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return missing is True or type(missing).__name__ == "bool_" and bool(missing)


def _metadata_scalar(value: object) -> None | bool | int | float | str:
    if _is_missing(value):
        return None
    if isinstance(value, str):
        return value
    if type(value) is bool:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            item: object = item_method()
        except (TypeError, ValueError):
            return None
        if type(item) is bool:
            return item
        if isinstance(item, int) and not isinstance(item, bool):
            return item
        if isinstance(item, float):
            return item if isfinite(item) else None
    return None


def _utc_datetime_or_none(value: object) -> datetime | None:
    if _is_missing(value):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return cast(datetime, timestamp.to_pydatetime()).astimezone(UTC)


def _duration_iso_or_none(value: object) -> str | None:
    if _is_missing(value):
        return None
    try:
        duration = pd.Timedelta(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(duration):
        return None
    return cast(str, duration.isoformat())


def _duration_milliseconds_or_none(value: object) -> int | None:
    if _is_missing(value):
        return None
    try:
        duration = pd.Timedelta(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(duration) or duration.value < 0:
        return None
    return (int(duration.value) + 500_000) // 1_000_000


def _integer_or_none(value: object) -> int | None:
    scalar = _metadata_scalar(value)
    if isinstance(scalar, int) and not isinstance(scalar, bool):
        return scalar
    if isinstance(scalar, float) and scalar.is_integer():
        return int(scalar)
    return None


__all__ = [
    "clean_message",
    "exception_detail",
    "extract_metadata",
    "read_attribute",
    "safe_attribute_value",
]
