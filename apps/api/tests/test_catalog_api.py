from __future__ import annotations

from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
from app.core.config import Settings
from app.main import create_app
from downforce_core.archive import (
    ArchiveEventStatus,
    ArchiveTableName,
    DataQuality,
    HistoricalArchiveStore,
    HistoricalCatalog,
    HistoricalEvent,
    HistoricalSeason,
    HistoricalSession,
    ProviderProvenance,
    QualityStatus,
    RaceDataCapabilities,
    SyncLifecycle,
)
from downforce_core.archive.schemas import ARCHIVE_SCHEMAS
from downforce_core.storage import DownforceRepository
from fastapi.testclient import TestClient


def _catalog_client(tmp_path: Path) -> TestClient:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    tables = {
        ArchiveTableName.RESULTS: pa.Table.from_pylist(
            [
                {
                    "session_id": session_id,
                    "driver_id": "michael-schumacher",
                    "driver_code": "MSC",
                    "driver_name": "Michael Schumacher",
                    "team_id": "ferrari",
                    "team_name": "Ferrari",
                    "car_number": 3,
                    "grid_position": 3,
                    "finish_position": 1,
                    "points": 10.0,
                    "laps_completed": 58,
                    "status": "Finished",
                    "classified": True,
                    "total_time_ms": 5_641_987,
                }
            ],
            schema=ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS],
        ),
        ArchiveTableName.LAPS: pa.Table.from_pylist(
            [
                {
                    "session_id": session_id,
                    "driver_id": "michael-schumacher",
                    "lap_number": 1,
                    "position": 3,
                    "lap_time_ms": 101_838,
                    "average_speed_kph": None,
                    "is_fastest_lap": False,
                }
            ],
            schema=ARCHIVE_SCHEMAS[ArchiveTableName.LAPS],
        ),
        ArchiveTableName.PIT_STOPS: pa.Table.from_pylist(
            [], schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS]
        ),
    }
    revision = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    provenance = ProviderProvenance(
        "jolpica",
        "test",
        "fixture",
        "https://example.invalid",
        "2026-08-27T00:00:00Z",
        "a" * 64,
    )
    quality = DataQuality(
        QualityStatus.VERIFIED,
        (),
        {"result_rows": 1, "lap_rows": 1},
        "2026-08-27T00:00:00Z",
    )
    session = HistoricalSession(
        session_id,
        "race",
        ArchiveEventStatus.COMPLETED,
        SyncLifecycle.COMPLETE,
        RaceDataCapabilities(results=True, grid=True, lap_times=True, lap_positions=True),
        quality,
        (provenance,),
        {"results": 1, "laps": 1, "pit_stops": 0},
        revision,
    )
    event = HistoricalEvent(
        "event-2000-round-01",
        2000,
        1,
        "Australian Grand Prix",
        "Australian Grand Prix",
        "2000-03-12",
        "Albert Park Grand Prix Circuit",
        "Melbourne",
        "Australia",
        "AUS",
        ArchiveEventStatus.COMPLETED,
        (session,),
        ("Michael Schumacher",),
        ("Ferrari",),
    )
    store.save_catalog(
        HistoricalCatalog(
            "1.0.0",
            "2026-08-27T00:00:00Z",
            2000,
            event.event_id,
            event.event_date,
            "archive-source-sha256-" + "b" * 64,
            (HistoricalSeason(2000, (event,)),),
        )
    )
    settings = Settings(environment="test", log_level="CRITICAL", project_root=tmp_path)
    return TestClient(create_app(settings, DownforceRepository(tmp_path)))


def test_catalog_discovery_capabilities_and_archive_pages(tmp_path: Path) -> None:
    client = _catalog_client(tmp_path)
    seasons = client.get("/api/v1/catalog/seasons")
    assert seasons.status_code == 200
    assert seasons.json()["latest_completed_event_id"] == "event-2000-round-01"
    assert seasons.json()["event_count"] == 1
    assert seasons.json()["completed_event_count"] == 1

    events = client.get(
        "/api/v1/catalog/events",
        params={"driver": "Schumacher", "team": "Ferrari", "capability": "lap_times"},
    )
    assert events.status_code == 200
    assert events.json()["total"] == 1
    assert events.json()["items"][0]["sessions"][0]["capability_tier"] == "lap_data"

    capability = client.get("/api/v1/catalog/sessions/archive-2000-round-01-race/capabilities")
    assert capability.status_code == 200
    assert capability.json()["capabilities"]["telemetry"] is False

    laps = client.get(
        "/api/v1/catalog/sessions/archive-2000-round-01-race/laps",
        params={"driver_id": "michael-schumacher", "from_lap": 1, "to_lap": 1},
    )
    assert laps.status_code == 200
    assert laps.json()["items"][0]["lap_time_ms"] == 101_838


def test_catalog_validation_and_missing_items_are_bounded(tmp_path: Path) -> None:
    client = _catalog_client(tmp_path)
    invalid_range = client.get(
        "/api/v1/catalog/sessions/archive-2000-round-01-race/laps",
        params={"from_lap": 3, "to_lap": 1},
    )
    assert invalid_range.status_code == 422

    missing = client.get("/api/v1/catalog/events/event-2000-round-99")
    assert missing.status_code == 404

    unsafe = client.get("/api/v1/catalog/events/%2E%2E")
    assert unsafe.status_code in {404, 422}

    storage = client.get("/api/v1/catalog/storage")
    assert storage.status_code == 200
    assert "root" not in storage.json()
    assert ".downforce" not in storage.text

    huge_lap = client.get(
        "/api/v1/catalog/sessions/archive-2000-round-01-race/laps",
        params={"from_lap": "9" * 101},
    )
    assert huge_lap.status_code == 422
    assert huge_lap.json()["error"]["code"] == "validation_error"

    for invalid_identifier in ("event--bad", "event-bad-", "con"):
        invalid = client.get(f"/api/v1/catalog/events/{invalid_identifier}")
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"

    openapi = client.get("/openapi.json").json()
    parameters = openapi["paths"]["/api/v1/catalog/events/{event_id}"]["get"]["parameters"]
    event_id_schema = next(item["schema"] for item in parameters if item["name"] == "event_id")
    assert event_id_schema["pattern"] == r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
