from pathlib import Path

import pytest
from downforce_core.detailed.rate_budget import RequestBudget


def test_budget_survives_restart_and_cannot_be_reset_by_new_worker(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = RequestBudget(path, limit=2, interval_seconds=60)
    assert budget.reserve(now=100) == 0
    assert budget.reserve(now=101) == 0
    restarted = RequestBudget(path, limit=2, interval_seconds=60)
    assert restarted.reserve(now=102) == 58
    assert restarted.reserve(now=160) == 0


def test_attempt_before_capacity_returns_delay_without_consuming_extra_slot(tmp_path: Path) -> None:
    budget = RequestBudget(tmp_path / "budget.json", limit=1, interval_seconds=60)
    budget.reserve(now=100)
    assert budget.reserve(now=110) == 50
    assert budget.reserve(now=111) == 49


def test_unknown_existing_worker_gets_explicit_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = RequestBudget(path, limit=2, interval_seconds=60)
    budget.defer_until(160)
    assert RequestBudget(path, limit=2, interval_seconds=60).reserve(now=100) == 60
    assert budget.reserve(now=160) == 0


def test_invalid_or_changed_policy_does_not_erase_saved_history(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    RequestBudget(path, limit=2, interval_seconds=60).reserve(now=100)
    with pytest.raises(ValueError):
        RequestBudget(path, limit=100, interval_seconds=60).reserve(now=101)
