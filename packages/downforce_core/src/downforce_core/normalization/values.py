"""Deterministic scalar conversion at the raw Arrow-to-domain boundary."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite, isnan
from numbers import Integral, Real

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.enums import DriverStatus
from downforce_core.domain.time import (
    duration_to_milliseconds,
    ensure_utc,
    utc_datetime_to_session_time_ms,
)


def normalize_missing(value: object) -> object | None:
    """Return a plain Python scalar, mapping all provider null sentinels to ``None``."""

    if isinstance(value, pa.Scalar):
        value = value.as_py()
    if value is None:
        return None
    value_type = type(value)
    if value_type.__module__.startswith("pandas.") and value_type.__name__ in {
        "NAType",
        "NaTType",
    }:
        return None
    if isinstance(value, float) and isnan(value):
        return None
    if isinstance(value, Decimal) and value.is_nan():
        return None
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return normalize_missing(converted)
    return value


def as_text(value: object) -> str | None:
    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if isinstance(normalized, bytes):
        text = normalized.decode("utf-8")
    elif isinstance(normalized, str):
        text = normalized
    elif isinstance(normalized, (Integral, Real, Decimal)):
        text = str(normalized)
    else:
        raise TypeError(f"cannot convert {type(normalized).__name__} to text")
    trimmed = text.strip()
    return trimmed if trimmed else None


def as_int(value: object) -> int | None:
    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        raise TypeError("boolean values are not integers")
    if isinstance(normalized, Integral):
        return int(normalized)
    if isinstance(normalized, Real):
        numeric = float(normalized)
        if isfinite(numeric) and numeric.is_integer():
            return int(numeric)
        raise ValueError(f"{normalized!r} is not an integral finite value")
    if isinstance(normalized, (str, Decimal)):
        try:
            decimal = Decimal(normalized)
        except InvalidOperation as error:
            raise ValueError(f"{normalized!r} is not an integer") from error
        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            raise ValueError(f"{normalized!r} is not an integral finite value")
        return int(decimal)
    raise TypeError(f"cannot convert {type(normalized).__name__} to integer")


def as_float(value: object) -> float | None:
    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        raise TypeError("boolean values are not numbers")
    if isinstance(normalized, (Real, Decimal, str)):
        try:
            result = float(normalized)
        except ValueError as error:
            raise ValueError(f"{normalized!r} is not numeric") from error
        if not isfinite(result):
            return None
        return result
    raise TypeError(f"cannot convert {type(normalized).__name__} to float")


def as_bool(value: object) -> bool | None:
    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        return normalized
    raise TypeError(f"cannot convert {type(normalized).__name__} to bool")


def as_utc_datetime(value: object) -> datetime | None:
    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if not isinstance(normalized, datetime):
        raise TypeError(f"cannot convert {type(normalized).__name__} to datetime")
    return ensure_utc(normalized)


def as_session_time_ms(value: object, *, origin: datetime | None = None) -> int | None:
    """Convert a raw duration or aware instant to deterministic integer milliseconds."""

    normalized = normalize_missing(value)
    if normalized is None:
        return None
    if isinstance(normalized, timedelta):
        return duration_to_milliseconds(normalized)
    if isinstance(normalized, datetime):
        if origin is None:
            raise ValueError("origin is required to convert an absolute datetime")
        return utc_datetime_to_session_time_ms(normalized, origin)
    if isinstance(normalized, bool):
        raise TypeError("boolean values are not durations")
    if isinstance(normalized, Integral):
        milliseconds = int(normalized)
    elif isinstance(normalized, (Real, Decimal, str)):
        try:
            decimal = Decimal(str(normalized))
        except InvalidOperation as error:
            raise ValueError(f"{normalized!r} is not a duration") from error
        if not decimal.is_finite():
            return None
        milliseconds = int(decimal.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        raise TypeError(f"cannot convert {type(normalized).__name__} to session time")
    if milliseconds < 0:
        raise ValueError("canonical session time must be nonnegative")
    return milliseconds


def normalize_status(
    classified_position: str | None,
    raw_status: str | None,
) -> DriverStatus:
    """Map only explicit classification/status evidence; keep broad DNF text unknown."""

    classified = classified_position.strip().upper() if classified_position else None
    if classified == "R":
        return DriverStatus.RETIRED
    if classified in {"D", "E"}:
        return DriverStatus.DISQUALIFIED
    if classified == "W":
        return DriverStatus.DID_NOT_START
    if classified in {"F", "N"}:
        return DriverStatus.NOT_CLASSIFIED
    mapped = DriverStatus.from_raw(raw_status)
    if mapped is not DriverStatus.UNKNOWN:
        return mapped
    if raw_status is not None and re.fullmatch(
        r"\+\s*\d+\s+laps?",
        raw_status.strip(),
        flags=re.IGNORECASE,
    ):
        return DriverStatus.FINISHED
    return DriverStatus.UNKNOWN


__all__ = [
    "as_bool",
    "as_float",
    "as_int",
    "as_session_time_ms",
    "as_text",
    "as_utc_datetime",
    "normalize_missing",
    "normalize_status",
]
