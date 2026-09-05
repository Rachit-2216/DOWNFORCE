from pathlib import Path
from types import SimpleNamespace

from app.schemas.strategy import CounterfactualRequest
from app.services.strategy_service import StrategyService
from conftest import HistoricalApi


def _request(api: HistoricalApi) -> dict[str, object]:
    return {
        "cursor_time_ms": 100_000,
        "driver_id": api.driver_id,
        "strategy": {"strategy_id": "stay", "label": "Stay out", "actions": []},
        "scenario": {"scheduled_total_laps": 10},
        "simulation_count": 100,
        "seed": 2216,
    }


def test_strategy_status_and_simulation_fail_closed_without_artifact(
    historical_api: HistoricalApi,
) -> None:
    status = historical_api.client.get("/api/v1/strategy/status")
    assert status.status_code == 200
    assert status.json()["availability"] == "unavailable"
    assert status.json()["reason"] == "missing_or_corrupt_model"

    response = historical_api.client.post(
        f"/api/v1/sessions/{historical_api.session_id}/strategy/simulate",
        json=_request(historical_api),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["availability_reason"] == "missing_or_corrupt_model"
    assert response.json()["outcome"] is None


def test_strategy_api_rejects_invalid_compounds_and_duplicate_candidates(
    historical_api: HistoricalApi,
) -> None:
    invalid = _request(historical_api)
    strategy = invalid["strategy"]
    assert isinstance(strategy, dict)
    strategy["actions"] = [{"type": "pit", "lap": 5, "compound": "intermediate"}]
    response = historical_api.client.post(
        f"/api/v1/sessions/{historical_api.session_id}/strategy/simulate",
        json=invalid,
    )
    assert response.status_code == 422

    valid = _request(historical_api)
    comparison = {
        **valid,
        "strategies": [valid["strategy"], valid["strategy"]],
    }
    comparison.pop("strategy")
    duplicate = historical_api.client.post(
        f"/api/v1/sessions/{historical_api.session_id}/strategy/compare",
        json=comparison,
    )
    assert duplicate.status_code == 422

    unordered = _request(historical_api)
    unordered_strategy = unordered["strategy"]
    assert isinstance(unordered_strategy, dict)
    unordered_strategy["actions"] = [
        {"type": "pit", "lap": 6, "compound": "medium"},
        {"type": "pit", "lap": 5, "compound": "soft"},
    ]
    invalid_order = historical_api.client.post(
        f"/api/v1/sessions/{historical_api.session_id}/strategy/simulate",
        json=unordered,
    )
    assert invalid_order.status_code == 422


def test_strategy_api_accepts_canonical_scheduled_distance_default(
    historical_api: HistoricalApi,
) -> None:
    request = _request(historical_api)
    request["scenario"] = {}
    response = historical_api.client.post(
        f"/api/v1/sessions/{historical_api.session_id}/strategy/simulate",
        json=request,
    )
    assert response.status_code == 200
    assert response.json()["availability_reason"] == "missing_or_corrupt_model"


def test_counterfactual_reads_observed_future_only_after_simulation() -> None:
    trace: list[str] = []

    class Engine:
        def simulate(self, **_kwargs: object) -> dict[str, object]:
            trace.append("simulation_finalized")
            return {
                "status": "available",
                "availability_reason": None,
                "session_id": "fixture",
                "driver_id": "driver",
                "cursor": {"time_ms": 100_000, "lap": 1},
                "simulation_version": "1.1.0",
                "model_version": "fixture",
                "dataset_digest": "fixture",
                "assumptions": [],
                "outcome": {},
            }

    class Repository:
        def load_session(
            self, _session_id: str, *, include_track_positions: bool
        ) -> SimpleNamespace:
            assert include_track_positions is False
            trace.append("observed_future_loaded")
            return SimpleNamespace(classifications=(), pit_stops=())

    service = StrategyService(Repository(), Path("."))  # type: ignore[arg-type]
    service._engine = Engine()  # type: ignore[assignment]
    request = CounterfactualRequest.model_validate(
        {
            "cursor_time_ms": 100_000,
            "driver_id": "driver",
            "strategy": {"strategy_id": "stay", "label": "Stay out", "actions": []},
            "scenario": {"scheduled_total_laps": 10},
            "simulation_count": 10,
            "seed": 2216,
            "include_observed_result": True,
        }
    )
    result = service.counterfactual("fixture", request)
    assert trace == ["simulation_finalized", "observed_future_loaded"]
    assert result["observed_historical_result"] == {
        "label": "Observed historical fact — not a simulation input",
        "classified_position": None,
        "status": None,
        "future_pit_events": [],
    }
