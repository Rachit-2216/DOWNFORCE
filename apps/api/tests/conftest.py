from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from app.core.config import Settings
from app.main import create_app
from downforce_core.normalization import normalize_session
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderSession,
    ProviderTable,
    SessionRef,
)
from downforce_core.replay import ReplayEngine, build_timeline
from downforce_core.storage import DownforceRepository
from downforce_core.storage.raw import commit_raw_snapshot
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(environment="test", log_level="CRITICAL")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _table(name: DatasetName, rows: list[dict[str, object]]) -> ProviderTable:
    keys = sorted({key for row in rows for key in row})
    data = pa.Table.from_pylist([{key: row.get(key) for key in keys} for row in rows])
    return ProviderTable(
        name=name,
        availability=(DatasetAvailability.AVAILABLE if rows else DatasetAvailability.EMPTY),
        data=data,
    )


def _historical_provider_session() -> ProviderSession:
    rows: dict[DatasetName, list[dict[str, object]]] = {
        DatasetName.DRIVERS: [
            {
                "driver_number": "0",
                "abbreviation": "AAA",
                "full_name": "Ada Apex",
                "team_name": "Analytical GP",
                "country_code": "GBR",
                "classified_position": "1",
                "status": "Finished",
                "points": 25.0,
            },
            {
                "driver_number": "7",
                "abbreviation": "BBB",
                "full_name": "Bob Brake",
                "team_name": "Boundary Racing",
                "country_code": "FRA",
                "classified_position": "2",
                "status": "Finished",
                "points": 18.0,
            },
        ],
        DatasetName.LAPS: [
            {
                "driver_number": "0",
                "lap_number": 1.0,
                "time": timedelta(seconds=90),
                "lap_start_time": timedelta(0),
                "lap_time": timedelta(seconds=90),
                "stint": 1.0,
                "compound": "SOFT",
                "tyre_life": 1.0,
                "pit_in_time": timedelta(seconds=85),
            },
            {
                "driver_number": "0",
                "lap_number": 2.0,
                "time": timedelta(seconds=181),
                "lap_start_time": timedelta(seconds=90),
                "lap_time": timedelta(seconds=91),
                "stint": 2.0,
                "compound": "MEDIUM",
                "tyre_life": 1.0,
                "pit_out_time": timedelta(seconds=95),
            },
            {
                "driver_number": "7",
                "lap_number": 1.0,
                "time": timedelta(seconds=91),
                "lap_start_time": timedelta(0),
                "lap_time": timedelta(seconds=91),
                "stint": 1.0,
                "compound": "HARD",
                "tyre_life": 2.0,
            },
        ],
        DatasetName.WEATHER: [
            {
                "time": timedelta(seconds=10),
                "air_temp": 20.0,
                "track_temp": 30.0,
                "rainfall": False,
            }
        ],
        DatasetName.RACE_CONTROL: [
            {
                "source_kind": "track_status",
                "session_time": timedelta(seconds=5),
                "status": "1",
            }
        ],
        DatasetName.RACE_POSITIONS: [
            {
                "driver_number": "0",
                "time": timedelta(seconds=90),
                "lap_number": 1.0,
                "position": 1.0,
            },
            {
                "driver_number": "7",
                "time": timedelta(seconds=91),
                "lap_number": 1.0,
                "position": 2.0,
            },
            {
                "driver_number": "0",
                "time": timedelta(seconds=181),
                "lap_number": 2.0,
                "position": 1.0,
            },
        ],
        DatasetName.TRACK_POSITIONS: [
            {
                "driver_number": "0",
                "session_time": timedelta(seconds=1),
                "x": 1000.0,
                "y": 2000.0,
                "z": 50.0,
                "status": "OnTrack",
                "source": "pos",
            },
            {
                "driver_number": "0",
                "session_time": timedelta(seconds=2),
                "x": 1010.0,
                "y": 2010.0,
                "z": 50.0,
                "status": "OnTrack",
                "source": "pos",
            },
        ],
        DatasetName.CAR_TELEMETRY: [
            {
                "driver_number": "0",
                "start_time": timedelta(seconds=1),
                "end_time": timedelta(seconds=181),
                "data_key": "fixture-car-0",
                "channel_names": ["Speed", "RPM"],
                "sample_count": 100,
            }
        ],
    }
    return ProviderSession(
        session=SessionRef(2024, 12, "R"),
        provider_name="fixture",
        provider_version="1.0",
        retrieved_at=datetime(2024, 7, 8, tzinfo=UTC),
        metadata={
            "event_name": "British Grand Prix",
            "session_name": "Race",
            "season": 2024,
            "round_number": 12,
            "country_code": "GBR",
            "circuit_name": "Silverstone Circuit",
            "scheduled_start_utc": datetime(2024, 7, 7, 14, tzinfo=UTC),
            "session_origin_utc": datetime(2024, 7, 7, 14, tzinfo=UTC),
            "session_start_time_ms": 0,
            "coordinate_scale_to_m": 0.1,
        },
        tables={name: _table(name, rows[name]) for name in DatasetName},
    )


@dataclass(frozen=True, slots=True)
class HistoricalApi:
    client: TestClient
    session_id: str
    alias_id: str
    driver_id: str
    max_time_ms: int


@pytest.fixture
def historical_api(tmp_path: Path) -> Iterator[HistoricalApi]:
    source = _historical_provider_session()
    repository = DownforceRepository(tmp_path)
    initial = normalize_session(source)
    raw = commit_raw_snapshot(repository.layout, str(initial.metadata.session_id), source)
    normalized = normalize_session(raw.session)
    timeline = build_timeline(normalized)
    manifest = repository.write_session(
        normalized,
        raw.snapshot_id,
        events=timeline.events,
    )
    ReplayEngine.from_repository(repository, manifest.session_id)
    settings = Settings(environment="test", log_level="CRITICAL", project_root=tmp_path)
    with TestClient(create_app(settings, repository)) as test_client:
        yield HistoricalApi(
            client=test_client,
            session_id=manifest.session_id,
            alias_id=str(source.session.session_id),
            driver_id=str(normalized.drivers[0].driver_id),
            max_time_ms=timeline.max_time_ms,
        )
