from datetime import UTC, datetime, timedelta

import pytest
from downforce_core.domain.time import (
    duration_to_milliseconds,
    ensure_utc,
    milliseconds_to_duration,
    utc_datetime_to_session_time_ms,
)


@pytest.mark.parametrize(
    ("duration", "expected_ms"),
    [
        (timedelta(0), 0),
        (timedelta(microseconds=499), 0),
        (timedelta(microseconds=500), 1),
        (timedelta(microseconds=1_499), 1),
        (timedelta(microseconds=1_500), 2),
        (timedelta(seconds=1, microseconds=234_500), 1_235),
    ],
)
def test_duration_conversion_uses_deterministic_half_up_integer_milliseconds(
    duration: timedelta, expected_ms: int
) -> None:
    result = duration_to_milliseconds(duration)
    assert result == expected_ms
    assert isinstance(result, int)


def test_canonical_duration_rejects_negative_time() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        duration_to_milliseconds(timedelta(microseconds=-1))
    with pytest.raises(ValueError, match="nonnegative"):
        milliseconds_to_duration(-1)


def test_milliseconds_to_duration_requires_an_integer_not_bool() -> None:
    with pytest.raises(TypeError):
        milliseconds_to_duration(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        milliseconds_to_duration(True)


def test_utc_datetime_conversion_is_aware_and_integer_milliseconds() -> None:
    origin = datetime(2024, 7, 7, 14, tzinfo=UTC)
    timestamp = datetime(2024, 7, 7, 14, 0, 1, 234_500, tzinfo=UTC)

    assert utc_datetime_to_session_time_ms(timestamp, origin) == 1_235
    assert ensure_utc(timestamp).tzinfo is UTC


def test_datetime_conversion_rejects_naive_or_pre_session_timestamps() -> None:
    aware = datetime(2024, 7, 7, 14, tzinfo=UTC)
    naive = datetime(2024, 7, 7, 14)

    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_datetime_to_session_time_ms(naive, aware)
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_datetime_to_session_time_ms(aware, naive)
    with pytest.raises(ValueError, match="nonnegative"):
        utc_datetime_to_session_time_ms(aware - timedelta(milliseconds=1), aware)
