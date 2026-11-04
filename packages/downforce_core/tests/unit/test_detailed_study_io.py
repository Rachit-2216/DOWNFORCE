import json
from pathlib import Path

import pytest
from downforce_core.detailed.study_io import load_reconciled_inventory, publish_sample_plan


def test_sampling_plan_requires_finished_reconciliation(tmp_path: Path) -> None:
    (tmp_path / "reconciled-inventory.json").write_text(
        json.dumps({"complete": False, "sessions": []})
    )
    with pytest.raises(ValueError, match="complete"):
        load_reconciled_inventory(tmp_path)


def test_plan_is_published_before_download_and_cannot_be_reselected(tmp_path: Path) -> None:
    inventory = {
        "complete": True,
        "sessions": [
            {
                "session_id": f"2018-{n:02d}-race",
                "season": 2018,
                "event_name": "Test",
                "session_type": "race",
                "state": "completed",
            }
            for n in range(1, 8)
        ],
    }
    (tmp_path / "reconciled-inventory.json").write_text(json.dumps(inventory))
    first = publish_sample_plan(tmp_path, seed=2216)
    assert len(first["selected_sessions"]) == 3
    assert first["selected_before_download"] is True
    assert publish_sample_plan(tmp_path, seed=2216) == first
    with pytest.raises(ValueError, match="frozen"):
        publish_sample_plan(tmp_path, seed=7)


def test_unverified_rows_remain_visible_in_plan_context(tmp_path: Path) -> None:
    rows = [
        {
            "session_id": "2018-01-race",
            "season": 2018,
            "event_name": "Test",
            "session_type": "race",
            "state": "completed",
        },
        {
            "session_id": "2018-02-race",
            "season": 2018,
            "event_name": "Test",
            "session_type": "race",
            "state": "unverified",
        },
    ]
    (tmp_path / "reconciled-inventory.json").write_text(
        json.dumps({"complete": True, "sessions": rows})
    )
    plan = publish_sample_plan(tmp_path)
    assert len(plan["selected_sessions"]) == 1
    assert plan["inventory_states"]["unverified"] == 1


def test_plan_covers_openf1_2023_boundary_before_sizes_are_seen(tmp_path: Path) -> None:
    rows = [
        {
            "session_id": f"2021-{n:02d}-practice-1",
            "season": 2021,
            "event_name": "Test",
            "session_type": "practice-1",
            "state": "completed",
        }
        for n in range(1, 21)
    ]
    modern = {
        "session_id": "2023-01-practice-1",
        "season": 2023,
        "event_name": "Test",
        "session_type": "practice-1",
        "state": "completed",
        "openf1_session_key": 1,
    }
    (tmp_path / "reconciled-inventory.json").write_text(
        json.dumps({"complete": True, "sessions": rows + [modern]})
    )
    plan = publish_sample_plan(tmp_path)
    assert modern["session_id"] in {entry["session_id"] for entry in plan["selected_sessions"]}
    assert plan["openf1_coverage_required"] == [modern["session_id"]]


def test_storage_projection_refuses_missing_openf1_enrichment_measurement() -> None:
    from downforce_core.detailed.study_io import _calculate_measured_study

    rows = [
        {
            "session_id": "2024-01-race",
            "season": 2024,
            "session_type": "race",
            "event_name": "Test",
            "state": "completed",
            "openf1_session_key": 1,
        }
    ]
    result = _calculate_measured_study(rows, [], free_bytes=10**12)
    assert result["safe_to_sync"] is False
    assert result["openf1_measured"] is False


def test_projection_combines_measured_raw_and_canonical_without_double_cache_count() -> None:
    from downforce_core.detailed.study_io import _calculate_measured_study

    rows = [
        {
            "session_id": "2018-01-race",
            "season": 2018,
            "session_type": "race",
            "event_name": "Test",
            "state": "completed",
            "openf1_session_key": None,
        }
    ]
    measured = [
        {
            "session_id": "2018-01-race",
            "season": 2018,
            "session_type": "race",
            "raw_expanded_bytes": 100,
            "raw_evidence_bytes": 20,
            "canonical_original_bytes": 40,
            "canonical_bytes": 10,
            "metadata_bytes": 2,
            "temporary_peak_bytes": 80,
            "round_trip_exact": True,
            "semantic_identity": True,
        }
    ]
    result = _calculate_measured_study(rows, measured, free_bytes=10**12)
    assert result["options"]["A"]["expected_bytes"] == 142
    assert result["options"]["B"]["expected_bytes"] == 32
    assert result["safe_to_sync"] is True


def test_projection_refuses_non_round_tripped_measurements() -> None:
    from downforce_core.detailed.study_io import _calculate_measured_study

    with pytest.raises(ValueError, match="lossless"):
        _calculate_measured_study([], [{"round_trip_exact": False}], free_bytes=10**12)


def test_unmapped_modern_provider_does_not_mean_zero_enrichment() -> None:
    from downforce_core.detailed.study_io import _calculate_measured_study

    rows = [
        {
            "session_id": "2024-01-race",
            "season": 2024,
            "session_type": "race",
            "event_name": "Test",
            "state": "completed",
            "openf1_session_key": None,
        }
    ]
    measured = [
        {
            "session_id": "2024-01-race",
            "season": 2024,
            "session_type": "race",
            "raw_expanded_bytes": 100,
            "raw_evidence_bytes": 20,
            "canonical_original_bytes": 40,
            "canonical_bytes": 10,
            "metadata_bytes": 2,
            "temporary_peak_bytes": 80,
            "round_trip_exact": True,
            "semantic_identity": True,
        }
    ]
    result = _calculate_measured_study(rows, measured, free_bytes=10**12)
    assert not result["safe_to_sync"]
