import pytest
from downforce_core.detailed.study import choose_sample, stratified_projection


def test_projection_weights_only_completed_inventory_by_era_and_type() -> None:
    inventory = [
        {"season": 2018, "session_type": "race", "state": "completed"},
        {"season": 2018, "session_type": "race", "state": "completed"},
        {"season": 2024, "session_type": "practice-1", "state": "completed"},
        {"season": 2024, "session_type": "race", "state": "future"},
    ]
    samples = [
        {"season": 2018, "session_type": "race", "bytes": 100},
        {"season": 2018, "session_type": "race", "bytes": 200},
        {"season": 2024, "session_type": "practice-1", "bytes": 20},
    ]
    result = stratified_projection(
        inventory,
        samples,
        free_bytes=2000,
        peak_working_bytes=100,
        headroom_bytes=500,
        reserve_bytes=200,
    )
    assert result["expected_bytes"] == 320
    assert result["stress_bytes"] == 420
    assert result["safe_to_sync"]
    assert result["completed_sessions"] == 3
    assert result["weak_strata"]


def test_unrepresented_stratum_and_unverified_inventory_fail_closed() -> None:
    inventory = [{"season": 2018, "session_type": "race", "state": "completed"}]
    result = stratified_projection(inventory, [], free_bytes=10**12, peak_working_bytes=1)
    assert not result["safe_to_sync"]
    assert result["missing_strata"] == ["2018-2020/race"]
    inventory.append({"season": 2018, "session_type": "race", "state": "unverified"})
    result = stratified_projection(
        inventory,
        [{"season": 2018, "session_type": "race", "bytes": 1}],
        free_bytes=10**12,
        peak_working_bytes=1,
    )
    assert not result["safe_to_sync"]
    assert result["unverified_sessions"] == 1


def test_preferred_headroom_and_peak_are_distinct_from_hard_reserve() -> None:
    rows = [{"season": 2018, "session_type": "race", "state": "completed"}]
    samples = [{"season": 2018, "session_type": "race", "bytes": 100}]
    result = stratified_projection(
        rows, samples, free_bytes=450, peak_working_bytes=100, headroom_bytes=300, reserve_bytes=200
    )
    assert result["hard_reserve_safe"]
    assert not result["safe_to_sync"]


def test_bootstrap_is_reproducible() -> None:
    rows = [{"season": 2018, "session_type": "race", "state": "completed"}]
    samples = [{"season": 2018, "session_type": "race", "bytes": n} for n in (10, 20, 30)]
    assert stratified_projection(
        rows, samples, free_bytes=10**12, peak_working_bytes=1
    ) == stratified_projection(rows, samples, free_bytes=10**12, peak_working_bytes=1)


def test_sampling_is_deterministic_and_reuses_existing_measurements() -> None:
    rows = [
        {
            "session_id": f"2018-{n:02d}-race",
            "season": 2018,
            "session_type": "race",
            "state": "completed",
        }
        for n in range(1, 10)
    ]
    a = choose_sample(rows, existing={"2018-01-race"}, seed=2216)
    b = choose_sample(list(reversed(rows)), existing={"2018-01-race"}, seed=2216)
    assert a == b
    assert "2018-01-race" in a
    assert len(a) == 3


@pytest.mark.parametrize("size", [-1, float("nan"), float("inf")])
def test_invalid_storage_evidence_is_rejected(size: float) -> None:
    with pytest.raises(ValueError):
        stratified_projection(
            [],
            [{"season": 2018, "session_type": "race", "bytes": size}],
            free_bytes=100,
            peak_working_bytes=1,
        )


def test_partial_reconciliation_cannot_authorize_sync() -> None:
    rows = [{"season": 2018, "session_type": "race", "state": "completed"}]
    result = stratified_projection(
        rows,
        [{"season": 2018, "session_type": "race", "bytes": 1}],
        free_bytes=10**12,
        peak_working_bytes=1,
        inventory_complete=False,
    )
    assert not result["safe_to_sync"]
    assert not result["hard_reserve_safe"]


def test_existing_sample_must_be_a_completed_inventory_member() -> None:
    with pytest.raises(ValueError, match="completed"):
        choose_sample([], existing={"2018-01-race"})


def test_headroom_cannot_be_less_than_hard_reserve() -> None:
    with pytest.raises(ValueError, match="headroom"):
        stratified_projection(
            [], [], free_bytes=100, peak_working_bytes=0, headroom_bytes=10, reserve_bytes=20
        )
