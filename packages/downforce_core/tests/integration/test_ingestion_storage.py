from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import downforce_core.storage.repository as repository_module
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.cli.main import main as cli_main
from downforce_core.exceptions import SchemaVersionError, StorageIntegrityError
from downforce_core.ingestion import ingest_session
from downforce_core.normalization import normalize_session
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    ProviderSession,
    ProviderTable,
    SessionRef,
)
from downforce_core.replay import ReplayEngine
from downforce_core.storage.raw import commit_raw_snapshot
from downforce_core.storage.repository import DownforceRepository
from downforce_core.storage.schemas import CANONICAL_SCHEMAS, CanonicalTableName


def _table(name: DatasetName, rows: list[dict[str, object]]) -> ProviderTable:
    keys = sorted({key for row in rows for key in row})
    data = pa.Table.from_pylist([{key: row.get(key) for key in keys} for row in rows])
    return ProviderTable(
        name=name,
        availability=(DatasetAvailability.AVAILABLE if rows else DatasetAvailability.EMPTY),
        data=data,
    )


def _provider_session(*, retrieved_at: datetime | None = None) -> ProviderSession:
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
            }
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
                "position": 1.0,
            }
        ],
        DatasetName.WEATHER: [],
    }
    tables: dict[DatasetName, ProviderTable] = {}
    for name in DatasetName:
        if name in rows:
            tables[name] = _table(name, rows[name])
        else:
            tables[name] = ProviderTable(
                name=name,
                availability=DatasetAvailability.NOT_REQUESTED,
            )
    return ProviderSession(
        session=SessionRef(2024, 12, "R"),
        provider_name="fixture",
        provider_version="1.0.0",
        retrieved_at=retrieved_at or datetime(2024, 7, 8, tzinfo=UTC),
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
            "provider_source_id": "fixture-2024-12-race",
            "coordinate_scale_to_m": 0.1,
        },
        tables=tables,
    )


class _FixtureProvider:
    name = "fixture"
    version = "1.0.0"
    capabilities = ProviderCapabilities(
        drivers=True,
        laps=True,
        weather=True,
        race_control=False,
        race_positions=False,
        track_positions=False,
        car_telemetry=False,
        live=False,
    )

    def __init__(self, session: ProviderSession) -> None:
        self.session = session
        self.calls = 0

    async def load_session(self, reference: SessionRef, options: object = None) -> ProviderSession:
        del reference, options
        self.calls += 1
        return self.session


def test_raw_snapshot_and_canonical_parquet_round_trip_are_deterministic(
    tmp_path: Path,
) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    normalized_id = str(normalize_session(source).metadata.session_id)
    first_raw = commit_raw_snapshot(repository.layout, normalized_id, source)
    later_source = _provider_session(retrieved_at=datetime(2024, 7, 9, tzinfo=UTC))
    second_raw = commit_raw_snapshot(repository.layout, normalized_id, later_source)

    assert first_raw.snapshot_id == second_raw.snapshot_id
    assert second_raw.reused is True
    assert second_raw.session.retrieved_at == datetime(2024, 7, 8, tzinfo=UTC)

    normalized = normalize_session(first_raw.session)
    first_manifest = repository.write_session(normalized, first_raw.snapshot_id)
    second_manifest = repository.write_session(normalized, first_raw.snapshot_id)

    assert first_manifest.dataset_id == second_manifest.dataset_id
    assert first_manifest.tables["weather"].materialized is True
    assert first_manifest.tables["weather"].row_count == 0
    assert first_manifest.tables["track_positions"].materialized is False
    for name in CanonicalTableName:
        table = repository.load_table(first_manifest.session_id, name)
        assert table.schema.equals(CANONICAL_SCHEMAS[name], check_metadata=False)

    loaded = repository.load_session(first_manifest.session_id)
    assert loaded.metadata == normalized.metadata
    assert loaded.drivers == normalized.drivers
    assert loaded.laps == normalized.laps
    assert loaded.weather == ()
    assert loaded.track_positions == ()
    assert (
        repository.resolve_session_id(str(source.session.session_id)) == first_manifest.session_id
    )
    assert repository.active_dataset_identity(str(source.session.session_id)) == (
        first_manifest.session_id,
        first_manifest.dataset_id,
    )


def test_replay_session_load_omits_dense_track_positions(tmp_path: Path) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    tables = dict(source.tables)
    tables[DatasetName.TRACK_POSITIONS] = _table(
        DatasetName.TRACK_POSITIONS,
        [
            {
                "driver_number": "0",
                "session_time": timedelta(seconds=1),
                "x": 1000.0,
                "y": 2000.0,
                "z": 50.0,
                "status": "OnTrack",
                "source": "pos",
            }
        ],
    )
    source = replace(source, tables=tables)
    result = asyncio.run(
        ingest_session(repository, source.session, lambda: _FixtureProvider(source))
    )

    full = repository.load_session(result.session_id)
    replay = repository.load_session(result.session_id, include_track_positions=False)

    assert full.track_positions.table.num_rows > 0
    assert replay.track_positions.table.num_rows == 0
    assert replay.completeness[DatasetName.TRACK_POSITIONS] is DatasetAvailability.AVAILABLE


def test_ingestion_cache_hit_never_initializes_provider_and_force_reuses_ids(
    tmp_path: Path,
) -> None:
    repository = DownforceRepository(tmp_path)
    provider = _FixtureProvider(_provider_session())
    reference = provider.session.session
    first = asyncio.run(ingest_session(repository, reference, lambda: provider))
    persisted = repository.load_manifest(first.session_id)
    events = repository.load_events(first.session_id)
    engine = ReplayEngine.from_repository(repository, first.session_id)
    assert persisted.timeline_version is not None
    assert persisted.replay_version is not None
    assert events
    final_state = engine.state_at(events[-1].session_time_ms)
    assert next(iter(final_state.drivers.values())).laps_completed == 1

    def forbidden_factory() -> _FixtureProvider:
        raise AssertionError("cache hit initialized the provider")

    cached = asyncio.run(ingest_session(repository, reference, forbidden_factory))
    assert provider.calls == 1
    assert cached.cache_hit is True
    assert cached.provider_called is False
    assert cached.dataset_id == first.dataset_id

    refreshed_provider = _FixtureProvider(
        _provider_session(retrieved_at=datetime(2024, 7, 10, tzinfo=UTC))
    )
    refreshed = asyncio.run(
        ingest_session(
            repository,
            reference,
            lambda: refreshed_provider,
            force=True,
        )
    )
    assert refreshed_provider.calls == 1
    assert refreshed.raw_snapshot_reused is True
    assert refreshed.snapshot_id == first.snapshot_id
    assert refreshed.dataset_id == first.dataset_id


def test_repository_detects_parquet_tampering(tmp_path: Path) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    normalized = normalize_session(source)
    raw = commit_raw_snapshot(
        repository.layout,
        str(normalized.metadata.session_id),
        source,
    )
    manifest = repository.write_session(normalize_session(raw.session), raw.snapshot_id)
    artifact = manifest.tables[CanonicalTableName.LAPS.value]
    assert artifact.path is not None
    table_path = (
        repository.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        / artifact.path
    )
    with table_path.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(StorageIntegrityError, match="checksum"):
        repository.load_manifest(manifest.session_id)


def test_cli_inspect_uses_verified_repository_without_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    raw = commit_raw_snapshot(
        repository.layout,
        str(normalize_session(source).metadata.session_id),
        source,
    )
    manifest = repository.write_session(normalize_session(raw.session), raw.snapshot_id)

    result = cli_main(["--root", str(tmp_path), "inspect", "--session-id", manifest.session_id])
    output = capsys.readouterr()

    assert result == 0
    assert manifest.dataset_id in output.out
    assert ".downforce" not in output.out
    assert output.err == ""


def test_cli_state_queries_repository_backed_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = DownforceRepository(tmp_path)
    provider = _FixtureProvider(_provider_session())
    result = asyncio.run(ingest_session(repository, provider.session.session, lambda: provider))
    events = repository.load_events(result.session_id)

    exit_code = cli_main(
        [
            "--root",
            str(tmp_path),
            "state",
            "--session-id",
            result.session_id,
            "--time-ms",
            str(events[-1].session_time_ms),
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 0
    assert '"laps_completed": 1' in output.out
    assert '"replay_version": "1.0.0"' in output.out
    assert output.err == ""


def test_repository_distinguishes_version_mismatch_from_corruption(tmp_path: Path) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    raw = commit_raw_snapshot(
        repository.layout,
        str(normalize_session(source).metadata.session_id),
        source,
    )
    manifest = repository.write_session(normalize_session(raw.session), raw.snapshot_id)
    path = (
        repository.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        / "manifest.json"
    )
    text = path.read_text(encoding="utf-8").replace(
        '"canonical_schema_version":"1.0.0"',
        '"canonical_schema_version":"0.0.0"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(SchemaVersionError, match="schema version"):
        repository.load_manifest(manifest.session_id)


@pytest.mark.parametrize("unsafe", ["../escape", "..", "session/child", "session\\child"])
def test_repository_rejects_unsafe_session_ids(tmp_path: Path, unsafe: str) -> None:
    repository = DownforceRepository(tmp_path)
    with pytest.raises(ValueError):
        repository.load_manifest(unsafe)


def test_failed_canonical_write_never_publishes_active_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = DownforceRepository(tmp_path)
    source = _provider_session()
    normalized = normalize_session(source)

    def fail_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected parquet failure")

    monkeypatch.setattr(repository_module, "write_parquet", fail_write)
    with pytest.raises(OSError, match="injected"):
        repository.write_session(normalized, "snapshot-sha256-" + "a" * 64)

    assert not repository.layout.active_pointer(str(normalized.metadata.session_id)).exists()
    assert repository.list_sessions() == ()
