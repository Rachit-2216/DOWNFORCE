from conftest import HistoricalApi


def test_state_queries_by_time_and_reference_lap_without_final_result_leakage(
    historical_api: HistoricalApi,
) -> None:
    client = historical_api.client
    early = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/state",
        params={"time_ms": 20_000},
    )
    assert early.status_code == 200, early.text
    body = early.json()
    assert body["session_time_ms"] == 20_000
    assert body["weather"]["observed_at_ms"] == 10_000
    driver = next(item for item in body["drivers"] if item["driver_id"] == historical_api.driver_id)
    assert driver["status"] == "active"
    assert driver["laps_completed"] == 0
    assert "classified_position" not in early.text
    assert "points" not in early.text

    lap = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/state",
        params={"lap": 1, "phase": "end"},
    )
    assert lap.status_code == 200
    assert lap.json()["reference_lap"] == 1
    assert lap.json()["session_time_ms"] == 90_000


def test_timeline_is_significant_filterable_and_paginated(
    historical_api: HistoricalApi,
) -> None:
    client = historical_api.client
    timeline = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/timeline",
        params=[("types", "driver-lap-completed"), ("limit", "1")],
    )
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["items"][0]["event_type"] == "driver-lap-completed"
    assert body["items"][0]["payload"]["lap_number"] == 1


def test_state_and_timeline_cursor_errors_are_actionable(historical_api: HistoricalApi) -> None:
    client = historical_api.client
    missing_cursor = client.get(f"/api/v1/sessions/{historical_api.session_id}/state")
    assert missing_cursor.status_code == 422
    assert "exactly one" in missing_cursor.json()["error"]["message"]

    conflicting = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/state",
        params={"time_ms": 1, "lap": 1},
    )
    assert conflicting.status_code == 422

    beyond = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/state",
        params={"time_ms": historical_api.max_time_ms + 1},
    )
    assert beyond.status_code == 422, beyond.text
    assert beyond.json()["error"]["code"] == "invalid_replay_cursor"

    invalid_range = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/timeline",
        params={"from_ms": 100, "to_ms": 10},
    )
    assert invalid_range.status_code == 422
