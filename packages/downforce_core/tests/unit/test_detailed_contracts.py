"""Observed detailed data must never acquire invented numeric meaning."""

import pytest
from downforce_core.detailed.contracts import channel_quality, parse_interval, session_completion


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("+1 LAP", "laps-down", 1),
        ("+2 LAPS", "laps-down", 2),
        ("2.345", "seconds", 2.345),
        (0, "seconds", 0),
        (None, "unavailable", None),
        ("unknown", "unknown", None),
        (float("nan"), "unknown", None),
        (-2, "unknown", None),
    ],
)
def test_intervals_preserve_units(raw: object, kind: str, value: float | None) -> None:
    interval = parse_interval(raw)
    assert interval.kind == kind
    assert interval.value == value


def test_completion_needs_end_evidence_and_never_overrides_future() -> None:
    assert (
        session_completion("past-unverified", ["Started", "Finished", "Finalised"]) == "completed"
    )
    assert session_completion("past-unverified", ["Started", "Aborted"]) == "past-unverified"
    assert session_completion("future", ["Finalised"]) == "future"


def test_legacy_completion_helper_does_not_accept_no_start_or_late_restart() -> None:
    assert (
        session_completion("past-unverified", ["Inactive", "Finalised", "Ends"])
        == "past-unverified"
    )
    assert (
        session_completion("past-unverified", ["Started", "Finalised", "Started"])
        == "past-unverified"
    )


def test_quality_does_not_confuse_absent_with_zero() -> None:
    assert channel_quality("speed", [None, None]).status == "unavailable"
    assert channel_quality("speed", [0, 100]).status == "verified"
    assert channel_quality("speed", [float("nan"), 100]).status == "partial"


@pytest.mark.parametrize(
    ("channel", "values"),
    [
        ("gear", [3, 3.5]),
        ("brake", [0, 50]),
        ("throttle", [0, 120]),
        ("speed", [-1, 100]),
        ("rpm", [-1, 10000]),
    ],
)
def test_invalid_channel_values_are_flagged_not_dropped(channel: str, values: list[float]) -> None:
    quality = channel_quality(channel, values)
    assert quality.status == "degraded"
    assert quality.samples == 2
    assert quality.invalid == 1


def test_unknown_drs_code_remains_evidence_but_is_flagged() -> None:
    quality = channel_quality("drs", [0, 8, 12, 99])
    assert quality.invalid == 1
    assert quality.samples == 4
