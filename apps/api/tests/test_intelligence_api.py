from conftest import HistoricalApi
from downforce_core.domain import SessionId, make_driver_id


def test_ml_status_is_structured_when_artifacts_are_missing(historical_api: HistoricalApi) -> None:
    response = historical_api.client.get("/api/v1/ml/status")
    assert response.status_code == 200
    assert response.json() == {
        "availability": "unavailable",
        "reason": "ML artifact registry is unavailable",
        "dataset_digest": None,
        "model_version": None,
        "models": [],
    }


def test_intelligence_is_read_only_typed_and_unknown_driver_is_404(
    historical_api: HistoricalApi,
) -> None:
    response = historical_api.client.get(
        f"/api/v1/sessions/{historical_api.session_id}/drivers/"
        f"{historical_api.driver_id}/intelligence",
        params={"time_ms": 20_000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["availability"] == "unavailable"
    assert body["pace"] is None
    assert body["tyre_degradation"] is None
    assert body["pit_loss"] is None

    missing_driver = make_driver_id(SessionId(historical_api.session_id), 999)
    missing = historical_api.client.get(
        f"/api/v1/sessions/{historical_api.session_id}/drivers/{missing_driver}/intelligence",
        params={"time_ms": 20_000},
    )
    assert missing.status_code == 404
