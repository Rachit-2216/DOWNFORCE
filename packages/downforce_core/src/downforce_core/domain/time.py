"""Canonical UTC and integer session-time conversions."""

from datetime import UTC, datetime, timedelta


def ensure_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Require a timezone-aware datetime and normalize it to ``datetime.UTC``."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def duration_to_milliseconds(value: timedelta, *, allow_negative: bool = False) -> int:
    """Round a duration to integer milliseconds, with exact half-up semantics."""

    if not isinstance(value, timedelta):
        raise TypeError("duration must be a timedelta")
    total_microseconds = (
        value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    )
    if total_microseconds < 0 and not allow_negative:
        raise ValueError("canonical session time must be nonnegative")
    if total_microseconds >= 0:
        return (total_microseconds + 500) // 1_000
    return -((-total_microseconds + 500) // 1_000)


def milliseconds_to_duration(milliseconds: int, *, allow_negative: bool = False) -> timedelta:
    """Convert integer milliseconds to an exact duration."""

    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
        raise TypeError("milliseconds must be an integer")
    if milliseconds < 0 and not allow_negative:
        raise ValueError("canonical session time must be nonnegative")
    return timedelta(milliseconds=milliseconds)


def utc_datetime_to_session_time_ms(timestamp: datetime, session_origin: datetime) -> int:
    """Convert an aware UTC instant to nonnegative integer milliseconds from session origin."""

    normalized_timestamp = ensure_utc(timestamp, field_name="timestamp")
    normalized_origin = ensure_utc(session_origin, field_name="session_origin")
    return duration_to_milliseconds(normalized_timestamp - normalized_origin)


__all__ = [
    "duration_to_milliseconds",
    "ensure_utc",
    "milliseconds_to_duration",
    "utc_datetime_to_session_time_ms",
]
