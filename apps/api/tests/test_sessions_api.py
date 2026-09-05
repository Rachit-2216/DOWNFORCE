from conftest import HistoricalApi


def test_list_and_read_canonical_sessions(historical_api: HistoricalApi) -> None:
    client = historical_api.client
    listed = client.get("/api/v1/sessions")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["session_id"] == historical_api.session_id
    assert body["items"][0]["event_name"] == "British Grand Prix"

    loaded = client.get(f"/api/v1/sessions/{historical_api.alias_id}")
    assert loaded.status_code == 200
    session = loaded.json()
    assert session["session_id"] == historical_api.session_id
    assert session["provider"]["name"] == "fixture"
    assert session["timeline_version"] == "1.0.0"
    assert session["tables"]["events"]["materialized"] is True
    assert ".downforce" not in loaded.text


def test_drivers_laps_track_positions_and_telemetry_are_canonical_and_bounded(
    historical_api: HistoricalApi,
) -> None:
    client = historical_api.client
    drivers = client.get(f"/api/v1/sessions/{historical_api.session_id}/drivers")
    assert drivers.status_code == 200
    assert drivers.json()["total"] == 2
    assert "classified_position" not in drivers.text

    laps = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/laps",
        params={"driver_id": historical_api.driver_id, "from_lap": 2, "to_lap": 2},
    )
    assert laps.status_code == 200
    assert laps.json()["total"] == 1
    assert laps.json()["items"][0]["compound"] == "medium"

    positions = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/track-positions",
        params={"driver_id": historical_api.driver_id, "limit": 1},
    )
    assert positions.status_code == 200
    assert positions.json()["total"] == 2
    assert len(positions.json()["items"]) == 1
    assert positions.json()["items"][0]["x_m"] == 100.0

    telemetry = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/telemetry-index",
        params={"driver_id": historical_api.driver_id},
    )
    assert telemetry.status_code == 200
    assert telemetry.json()["total"] == 1
    assert telemetry.json()["items"][0]["channel_names"] == ["RPM", "Speed"]


def test_session_routes_validate_ranges_sizes_and_missing_sessions(
    historical_api: HistoricalApi,
) -> None:
    client = historical_api.client
    invalid_range = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/laps",
        params={"from_lap": 3, "to_lap": 1},
    )
    assert invalid_range.status_code == 422
    assert invalid_range.json()["error"]["message"] == "lap upper bound precedes lower bound"

    oversized = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/track-positions",
        params={"limit": 5001},
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] == "validation_error"

    missing = client.get("/api/v1/sessions/session-missing")
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_error"

    valid_missing = client.get("/api/v1/sessions/session-2024-round-99-type-race")
    assert valid_missing.status_code == 404
    assert valid_missing.json()["error"]["code"] == "session_not_found"

    unsafe_driver = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/laps",
        params={"driver_id": "con"},
    )
    assert unsafe_driver.status_code == 422

    huge_lap = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/laps",
        params={"to_lap": "9" * 101},
    )
    assert huge_lap.status_code == 422
    assert huge_lap.json()["error"]["code"] == "validation_error"

    huge_time = client.get(
        f"/api/v1/sessions/{historical_api.session_id}/track-positions",
        params={"from_ms": "9" * 101},
    )
    assert huge_time.status_code == 422
    assert huge_time.json()["error"]["code"] == "validation_error"
