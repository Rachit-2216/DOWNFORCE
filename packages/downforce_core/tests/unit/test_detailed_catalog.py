"""Discovery must describe provider evidence, not manufacture race weekends."""

from datetime import UTC, datetime

import pytest
from downforce_core.detailed.catalog import canonical_session_type, discover_schedule


@pytest.mark.parametrize(
    ("year", "label", "expected"),
    [
        (2021, "Sprint Qualifying", "sprint"),
        (2024, "Sprint Qualifying", "sprint-qualifying"),
        (2023, "Sprint Shootout", "sprint-shootout"),
        (2023, "Sprint Qualifying", "sprint-shootout"),
        (2018, "Practice 1", "practice-1"),
        (2026, "Qualifying", "qualifying"),
        (2026, "Mystery", "unknown"),
    ],
)
def test_provider_label_mapping_is_era_aware(year: int, label: str, expected: str) -> None:
    assert canonical_session_type(year, label) == expected


def test_discovery_keeps_only_actual_slots_and_never_calls_past_schedule_completed() -> None:
    rows = [
        {
            "RoundNumber": 10,
            "EventName": "British Grand Prix",
            "Country": "United Kingdom",
            "Location": "Silverstone",
            "FastF1Support": True,
            "Session1": "Practice 1",
            "Session1DateUtc": "2021-07-16T10:00:00",
            "Session2": "Sprint Qualifying",
            "Session2DateUtc": "2021-07-17T10:00:00Z",
            "Session3": "",
            "Session4": None,
            "Session5": "Race",
            "Session5DateUtc": "2021-07-18T14:00:00Z",
        }
    ]
    result = discover_schedule(2021, rows, now=datetime(2021, 7, 18, tzinfo=UTC))
    assert len(result) == 3
    assert [r.session_type for r in result] == ["practice-1", "sprint", "race"]
    assert [r.completion for r in result] == ["past-unverified", "past-unverified", "future"]
    assert result[1].provider_session_name == "Sprint Qualifying"
    assert result[1].session_id == "2021-10-sprint"
    assert result[1].event_id == "2021-10"
    assert result[1].scheduled_at == "2021-07-17T10:00:00+00:00"
    assert result[1].fastf1_slot == 2


def test_missing_date_unknown_label_and_testing_are_not_fabricated() -> None:
    result = discover_schedule(
        2026,
        [
            {"RoundNumber": 0, "Session1": "Practice 1"},
            {"RoundNumber": 1, "Session1": "Unknown Session", "Session2": "Race"},
        ],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert len(result) == 2
    assert result[0].session_type == "unknown"
    assert result[0].session_id == "2026-01-slot-1"
    assert all(r.completion == "unknown" for r in result)
    assert all(r.fastf1_supported is None for r in result)


def test_discovery_rejects_ambiguous_duplicate_identities() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        discover_schedule(
            2024,
            [{"RoundNumber": 1, "Session1": "Race", "Session2": "Race"}],
            now=datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_discovery_requires_timezone_aware_clock() -> None:
    with pytest.raises(ValueError, match="timezone"):
        discover_schedule(2024, [], now=datetime(2024, 1, 1))
