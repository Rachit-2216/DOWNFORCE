from datetime import UTC, datetime

import pytest
from downforce_core.detailed.reconciliation import classify_completion, match_openf1


def test_reclassification_keeps_source_evidence_and_requires_formal_archive() -> None:
    from downforce_core.detailed.reconciliation import reclassify_evidence

    entry = {"completion": "past-unverified", "fastf1_supported": True}
    evidence = {"state": "unverified", "statuses": ["Inactive", "Started"], "error": None}
    assert reclassify_evidence(entry, evidence)["state"] == "unverified"
    finalized = reclassify_evidence(entry, evidence, {"Status": "Complete"})
    assert finalized["state"] == "completed"
    assert finalized["source_classification"] == "unverified"
    assert evidence["state"] == "unverified"
    assert reclassify_evidence(entry, evidence, {"Status": "Generating"})["state"] == "unverified"


def test_provider_inventory_separates_testing_future_and_cancelled_without_extra_sessions() -> None:
    from downforce_core.detailed.reconciliation import reconcile_provider_inventory

    canonical = [
        {
            "session_id": "2024-01-race",
            "season": 2024,
            "session_type": "race",
            "scheduled_at": "2024-03-01T12:00:00+00:00",
            "state": "completed",
            "timing_key": 10,
        }
    ]
    providers = [
        {
            "session_key": 10,
            "year": 2024,
            "session_name": "Race",
            "date_start": "2024-03-01T12:00:00+00:00",
        },
        {
            "session_key": 20,
            "year": 2024,
            "session_name": "Day 1",
            "date_start": "2024-02-01T12:00:00+00:00",
        },
        {
            "session_key": 30,
            "year": 2024,
            "session_name": "Race",
            "date_start": "2024-04-01T12:00:00+00:00",
            "is_cancelled": True,
        },
        {
            "session_key": 40,
            "year": 2024,
            "session_name": "Race",
            "date_start": "2024-12-01T12:00:00+00:00",
        },
    ]
    report = reconcile_provider_inventory(
        canonical, providers + [providers[0]], now=datetime(2024, 10, 1, tzinfo=UTC)
    )
    assert len(report["sessions"]) == 1
    assert report["sessions"][0]["openf1_session_key"] == 10
    assert report["duplicate_provider_entries"] == 1
    assert report["provider_states"] == {"completed": 1, "testing": 1, "cancelled": 1, "future": 1}
    assert report["unmatched_unverified"] == []


def test_provider_inventory_rejects_conflicting_keys_and_many_to_one_mapping() -> None:
    from downforce_core.detailed.reconciliation import reconcile_provider_inventory

    provider = {
        "session_key": 10,
        "year": 2024,
        "session_name": "Race",
        "date_start": "2024-03-01T12:00:00+00:00",
    }
    clock = datetime(2024, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="conflicting"):
        reconcile_provider_inventory([], [provider, {**provider, "year": 2025}], now=clock)
    entry = {
        "session_id": "2024-01-race",
        "season": 2024,
        "session_type": "race",
        "scheduled_at": provider["date_start"],
        "state": "completed",
        "timing_key": 10,
    }
    with pytest.raises(ValueError, match="multiple canonical"):
        reconcile_provider_inventory(
            [entry, {**entry, "session_id": "2024-02-race"}], [provider], now=clock
        )


def test_inventory_finalization_requires_all_source_evidence_and_preserves_it() -> None:
    from downforce_core.detailed.reconciliation import finalize_inventory

    entry = {
        "session_id": "2020-02-practice-3",
        "season": 2020,
        "session_type": "practice-3",
        "completion": "past-unverified",
        "fastf1_supported": True,
        "scheduled_at": "2020-07-11T10:00:00+00:00",
    }
    catalog = {"complete_inventory": True, "sessions": [entry]}
    evidence = {
        entry["session_id"]: {
            "session_id": entry["session_id"],
            "state": "unverified",
            "statuses": ["Inactive", "Finalised", "Ends"],
            "error": None,
        }
    }
    clock = datetime(2024, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="evidence"):
        finalize_inventory(catalog, {}, [], now=clock)
    result = finalize_inventory(catalog, evidence, [], now=clock)
    assert result["complete"] is True
    assert result["summary"] == {"abandoned-no-data": 1}
    assert evidence[entry["session_id"]]["state"] == "unverified"
    with pytest.raises(ValueError, match="discovery"):
        finalize_inventory({**catalog, "complete_inventory": False}, evidence, [], now=clock)


def test_completed_requires_finalisation_not_elapsed_schedule() -> None:
    assert classify_completion("past-unverified", [], supported=True) == "unverified"
    assert (
        classify_completion("past-unverified", ["Started", "Finished"], supported=True)
        == "unverified"
    )
    assert (
        classify_completion("past-unverified", ["Started", "Finalised"], supported=True)
        == "completed"
    )
    assert (
        classify_completion("past-unverified", ["Started", "Aborted"], supported=True)
        == "abandoned-no-data"
    )
    assert classify_completion("future", ["Finalised"], supported=True) == "future"
    assert classify_completion("past-unverified", [], supported=False) == "unsupported"


def test_finalisation_before_restart_is_not_final_completion() -> None:
    assert (
        classify_completion("past-unverified", ["Finalised", "Started"], supported=True)
        == "unverified"
    )
    assert (
        classify_completion("past-unverified", ["Started"], supported=True, cancelled=True)
        == "cancelled"
    )


def test_openf1_match_uses_provider_key_and_does_not_double_count() -> None:
    rows = [
        {
            "session_key": 10,
            "session_name": "Race",
            "year": 2024,
            "date_start": "2024-03-01T12:00:00+00:00",
        },
        {
            "session_key": 99,
            "session_name": "Day 1",
            "year": 2024,
            "date_start": "2024-02-01T12:00:00+00:00",
        },
    ]
    matched = match_openf1(
        rows,
        season=2024,
        category="race",
        provider_key=10,
        scheduled=datetime(2024, 3, 1, 12, tzinfo=UTC),
    )
    assert matched is not None and matched["session_key"] == 10
    assert (
        match_openf1(
            rows,
            season=2024,
            category="practice-1",
            provider_key=None,
            scheduled=datetime(2024, 2, 1, 12, tzinfo=UTC),
        )
        is None
    )


def test_time_fallback_fails_closed_on_ambiguous_provider_matches() -> None:
    rows = [
        {
            "session_key": n,
            "session_name": "Race",
            "year": 2024,
            "date_start": "2024-03-01T12:00:00+00:00",
        }
        for n in [1, 2]
    ]
    assert (
        match_openf1(
            rows,
            season=2024,
            category="race",
            provider_key=None,
            scheduled=datetime(2024, 3, 1, 12, tzinfo=UTC),
        )
        is None
    )


def test_2023_openf1_sprint_qualifying_maps_to_the_same_shootout_session() -> None:
    row = {
        "session_key": 100,
        "session_name": "Sprint Qualifying",
        "year": 2023,
        "date_start": "2023-04-29T08:30:00+00:00",
    }
    match = match_openf1(
        [row],
        season=2023,
        category="sprint-shootout",
        provider_key=100,
        scheduled=datetime(2023, 4, 29, 8, 30, tzinfo=UTC),
    )
    assert match is not None and match["session_key"] == 100


def test_formally_finalised_without_start_is_no_data_not_completed() -> None:
    assert (
        classify_completion("past-unverified", ["Inactive", "Finalised", "Ends"], supported=True)
        == "abandoned-no-data"
    )


def test_archive_finalisation_can_resolve_truncated_status_stream() -> None:
    assert (
        classify_completion(
            "past-unverified", ["Inactive", "Started"], supported=True, archive_complete=True
        )
        == "completed"
    )
    assert (
        classify_completion("past-unverified", [], supported=True, archive_complete=True)
        == "unverified"
    )
    assert (
        classify_completion("future", ["Started"], supported=True, archive_complete=True)
        == "future"
    )
