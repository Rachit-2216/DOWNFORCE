from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from time import sleep

import downforce_core.archive.sync as archive_sync_module
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.archive import (
    ArchiveEventStatus,
    ArchiveTableName,
    CapabilityTier,
    DataQuality,
    HistoricalArchiveSync,
    HistoricalCatalog,
    HistoricalCatalogIndex,
    HistoricalEvent,
    HistoricalSeason,
    HistoricalSession,
    ProviderProvenance,
    QualityStatus,
    RaceDataCapabilities,
    SyncLifecycle,
)
from downforce_core.archive.jolpica import ArchiveSourceRace, JolpicaClient, JolpicaDumpDescriptor
from downforce_core.archive.quality import evaluate_archive_race
from downforce_core.archive.schemas import ARCHIVE_SCHEMAS
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import StorageIntegrityError


def _provenance() -> ProviderProvenance:
    return ProviderProvenance(
        provider="jolpica",
        provider_version="test",
        source="fixture",
        source_url="https://example.invalid",
        retrieved_at_utc="2026-08-27T00:00:00Z",
        raw_sha256="a" * 64,
    )


def _tables(session_id: str) -> dict[ArchiveTableName, pa.Table]:
    results = pa.Table.from_pylist(
        [
            {
                "session_id": session_id,
                "driver_id": "driver-one",
                "driver_code": "ONE",
                "driver_name": "Driver One",
                "team_id": "team-one",
                "team_name": "Team One",
                "car_number": 1,
                "grid_position": 2,
                "finish_position": 1,
                "points": 10.0,
                "laps_completed": 58,
                "status": "Finished",
                "classified": True,
                "total_time_ms": 5_000_000,
            }
        ],
        schema=ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS],
    )
    laps = pa.Table.from_pylist(
        [
            {
                "session_id": session_id,
                "driver_id": "driver-one",
                "lap_number": 1,
                "position": 1,
                "lap_time_ms": 91_000,
                "average_speed_kph": None,
                "is_fastest_lap": True,
            }
        ],
        schema=ARCHIVE_SCHEMAS[ArchiveTableName.LAPS],
    )
    pits = pa.Table.from_pylist([], schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS])
    return {
        ArchiveTableName.RESULTS: results,
        ArchiveTableName.LAPS: laps,
        ArchiveTableName.PIT_STOPS: pits,
    }


def test_jolpica_classic_raw_cache_repairs_corrupt_retained_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"MRData":{"RaceTable":{"Races":[]}}}'
    store = HistoricalArchiveStore(tmp_path)
    client = JolpicaClient(store, minimum_interval_seconds=0)
    monkeypatch.setattr(client, "_bytes", lambda _url: payload)

    _, digest = client._json("https://example.invalid/fixture")
    retained = store.raw_root / "jolpica-classic" / f"sha256-{digest}.json"
    retained.write_bytes(b"corrupt")

    parsed, repeated_digest = client._json("https://example.invalid/fixture")
    assert repeated_digest == digest
    assert parsed["MRData"] == {"RaceTable": {"Races": []}}
    assert retained.read_bytes() == payload


def _event(
    *,
    season: int = 2000,
    round_number: int = 1,
    capabilities: RaceDataCapabilities | None = None,
) -> HistoricalEvent:
    quality = DataQuality(
        QualityStatus.VERIFIED,
        (),
        {"result_rows": 1},
        "2026-08-27T00:00:00Z",
    )
    session = HistoricalSession(
        session_id=f"archive-{season}-round-{round_number:02d}-race",
        session_type="race",
        status=ArchiveEventStatus.COMPLETED,
        sync_status=SyncLifecycle.COMPLETE,
        capabilities=capabilities or RaceDataCapabilities(results=True, lap_times=True),
        quality=quality,
        provenance=(_provenance(),),
        row_counts={"results": 1, "laps": 1, "pit_stops": 0},
        data_revision="archive-revision-sha256-" + "b" * 64,
    )
    return HistoricalEvent(
        event_id=f"event-{season}-round-{round_number:02d}",
        season=season,
        round_number=round_number,
        name="Australian Grand Prix",
        official_name="Australian Grand Prix",
        event_date=f"{season}-03-12",
        circuit_name="Albert Park Grand Prix Circuit",
        locality="Melbourne",
        country="Australia",
        country_code="AUS",
        status=ArchiveEventStatus.COMPLETED,
        sessions=(session,),
        drivers=("Michael Schumacher",),
        teams=("Ferrari",),
    )


def _archive_manifest(session_id: str, *, validated_at: str) -> dict[str, object]:
    return {
        "event_id": "event-2000-round-01",
        "season": 2000,
        "round_number": 1,
        "event_name": "Australian Grand Prix",
        "official_name": "Australian Grand Prix",
        "event_date": "2000-03-12",
        "circuit_name": "Albert Park Grand Prix Circuit",
        "locality": "Melbourne",
        "country": "Australia",
        "country_code": "AUS",
        "drivers": ["Driver One"],
        "teams": ["Team One"],
        "legacy_session_id": None,
        "capabilities": RaceDataCapabilities(results=True, lap_times=True).to_dict(),
        "quality": DataQuality(
            QualityStatus.VERIFIED,
            (),
            {"result_rows": 1, "lap_rows": 1, "pit_stop_rows": 0},
            validated_at,
        ).to_dict(),
        "provenance": [_provenance().to_dict()],
        "fixture_session_id": session_id,
    }


def test_capability_tiers_are_evidence_driven() -> None:
    assert RaceDataCapabilities(results=True).tier is CapabilityTier.ARCHIVE
    assert RaceDataCapabilities(lap_times=True).tier is CapabilityTier.LAP_DATA
    assert RaceDataCapabilities(lap_times=True, pit_stops=True).tier is CapabilityTier.LAP_AND_PIT
    assert RaceDataCapabilities(lap_times=True, weather=True).tier is CapabilityTier.DETAILED_TIMING
    assert RaceDataCapabilities(track_positions=True).tier is CapabilityTier.TELEMETRY
    full = RaceDataCapabilities(
        telemetry=True,
        track_positions=True,
        weather=True,
        race_control=True,
        compounds=True,
        ml_intelligence=True,
        strategy_simulation=True,
    )
    assert full.tier is CapabilityTier.FULL_DOWNFORCE


def test_quality_treats_pre_pit_stop_era_as_supported_lap_data() -> None:
    tables = _tables("archive-2000-round-01-race")
    capabilities, quality = evaluate_archive_race(
        tables[ArchiveTableName.RESULTS],
        tables[ArchiveTableName.LAPS],
        tables[ArchiveTableName.PIT_STOPS],
        season=2000,
        validated_at_utc="2026-08-27T00:00:00Z",
    )
    assert quality.status is QualityStatus.VERIFIED
    assert capabilities.lap_times
    assert not capabilities.pit_stops
    assert capabilities.tier is CapabilityTier.LAP_DATA


def test_quality_keeps_position_only_laps_and_pits_truthful() -> None:
    tables = _tables("archive-2026-round-12-race")
    laps = tables[ArchiveTableName.LAPS].set_column(
        4,
        "lap_time_ms",
        pa.array([None], type=pa.int64()),
    )
    pits = pa.Table.from_pylist(
        [
            {
                "session_id": "archive-2026-round-12-race",
                "driver_id": "driver-one",
                "stop_number": 1,
                "lap_number": 20,
                "duration_ms": 24_000,
                "local_time": None,
            }
        ],
        schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS],
    )
    capabilities, quality = evaluate_archive_race(
        tables[ArchiveTableName.RESULTS],
        laps,
        pits,
        season=2026,
        validated_at_utc="2026-08-27T00:00:00Z",
    )
    assert capabilities.lap_positions
    assert not capabilities.lap_times
    assert capabilities.pit_stops
    assert capabilities.tier is CapabilityTier.LAP_AND_PIT
    assert quality.status is QualityStatus.GOOD
    assert "lap_times_not_present" in quality.reasons


def test_quality_degrades_structural_domain_defects() -> None:
    tables = _tables("archive-2020-round-01-race")
    duplicate_laps = pa.concat_tables(
        [tables[ArchiveTableName.LAPS], tables[ArchiveTableName.LAPS]]
    )
    _, quality = evaluate_archive_race(
        tables[ArchiveTableName.RESULTS],
        duplicate_laps,
        tables[ArchiveTableName.PIT_STOPS],
        season=2020,
        validated_at_utc="2026-08-27T00:00:00Z",
    )
    assert quality.status is QualityStatus.DEGRADED
    assert "duplicate_driver_lap" in quality.reasons
    assert quality.metrics["duplicate_driver_laps"] == 1

    wrong_session = tables[ArchiveTableName.LAPS].set_column(
        0,
        "session_id",
        pa.array(["archive-2020-round-99-race"], type=pa.string()),
    )
    _, identity_quality = evaluate_archive_race(
        tables[ArchiveTableName.RESULTS],
        wrong_session,
        tables[ArchiveTableName.PIT_STOPS],
        season=2020,
        expected_session_id="archive-2020-round-01-race",
        validated_at_utc="2026-08-27T00:00:00Z",
    )
    assert identity_quality.status is QualityStatus.DEGRADED
    assert "unexpected_session_identity" in identity_quality.reasons


def test_quality_declares_provider_pit_sequence_gaps_without_discarding_race() -> None:
    tables = _tables("archive-2020-round-01-race")
    pits = pa.Table.from_pylist(
        [
            {
                "session_id": "archive-2020-round-01-race",
                "driver_id": "driver-one",
                "stop_number": 2,
                "lap_number": 20,
                "duration_ms": 24_000,
                "local_time": None,
            }
        ],
        schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS],
    )
    _, quality = evaluate_archive_race(
        tables[ArchiveTableName.RESULTS],
        tables[ArchiveTableName.LAPS],
        pits,
        season=2020,
        validated_at_utc="2026-08-27T00:00:00Z",
    )
    assert quality.status is QualityStatus.GOOD
    assert "pit_stop_sequence_gap" in quality.reasons
    assert quality.metrics["pit_stop_sequence_gaps"] == 1


def test_archive_store_is_immutable_and_idempotent(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    tables = _tables(session_id)
    first = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    active_path = store.sessions_root / session_id / "active.json"
    active_mtime = active_path.stat().st_mtime_ns
    sleep(0.02)
    second = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    assert first == second
    assert active_path.stat().st_mtime_ns == active_mtime
    assert (
        store.load_table(session_id, ArchiveTableName.LAPS).to_pylist()
        == tables[ArchiveTableName.LAPS].to_pylist()
    )
    assert store.storage_report()["session_count"] == 1


def test_archive_store_revisions_semantic_metadata_but_not_lifecycle_time(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    tables = _tables(session_id)
    manifest = {
        "event_id": "event-2000-round-01",
        "quality": {
            "status": "verified",
            "reasons": [],
            "metrics": {"result_rows": 1},
            "validated_at_utc": "2026-08-27T00:00:00Z",
        },
        "provenance": [_provenance().to_dict()],
    }
    first = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest=manifest,
    )
    later = json.loads(json.dumps(manifest))
    later["quality"]["validated_at_utc"] = "2026-08-28T00:00:00Z"
    later["provenance"][0]["retrieved_at_utc"] = "2026-08-28T00:00:00Z"
    second = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest=later,
    )
    changed = json.loads(json.dumps(later))
    changed["quality"]["metrics"]["result_rows"] = 2
    third = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest=changed,
    )
    assert first == second
    assert third != second


@pytest.mark.parametrize("target", ["laps.parquet", "manifest.json", "manifest-list"])
def test_archive_store_repairs_corrupt_existing_revision(tmp_path: Path, target: str) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    tables = _tables(session_id)
    revision = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    filename = "manifest.json" if target == "manifest-list" else target
    corrupt = store.sessions_root / session_id / "revisions" / revision / filename
    corrupt.write_bytes(b"[]" if target == "manifest-list" else b"corrupt")
    repaired = store.write_session(
        session_id,
        tables,
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    assert repaired == revision
    assert (
        store.load_table(session_id, ArchiveTableName.LAPS).to_pylist()
        == tables[ArchiveTableName.LAPS].to_pylist()
    )


def test_archive_store_serializes_concurrent_same_session_writes(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    tables = _tables(session_id)

    def write() -> str:
        return store.write_session(
            session_id,
            tables,
            source_revision="a" * 64,
            manifest={"event_id": "event-2000-round-01"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        revisions = list(pool.map(lambda _: write(), range(24)))
    assert len(set(revisions)) == 1
    assert len(list((store.sessions_root / session_id / "revisions").iterdir())) == 1
    assert store.load_table(session_id, ArchiveTableName.LAPS).num_rows == 1


def test_archive_store_keeps_valid_replacement_when_quarantine_cleanup_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    destination.mkdir()
    (staging / "value.txt").write_text("new", encoding="utf-8")
    (destination / "value.txt").write_text("old", encoding="utf-8")

    def blocked_cleanup(_: Path) -> None:
        raise PermissionError("held by reader")

    monkeypatch.setattr("downforce_core.archive.storage.shutil.rmtree", blocked_cleanup)
    HistoricalArchiveStore._replace_revision(staging, destination)

    assert (destination / "value.txt").read_text("utf-8") == "new"


def test_archive_store_rejects_stale_schema_and_malformed_active_pointer(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    revision = store.write_session(
        session_id,
        _tables(session_id),
        source_revision="a" * 64,
        manifest={"event_id": "event-2000-round-01"},
    )
    manifest_path = store.sessions_root / session_id / "revisions" / revision / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["archive_schema_version"] = "0.9.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StorageIntegrityError, match="schema version is stale"):
        store.load_manifest(session_id)
    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(StorageIntegrityError, match="manifest is unreadable"):
        store.load_manifest(session_id)
    active_path = store.sessions_root / session_id / "active.json"
    active_path.write_text('{"data_revision":"../outside"}', encoding="utf-8")
    with pytest.raises(StorageIntegrityError, match="active revision is invalid"):
        store.active_revision(session_id)
    active_path.write_text("[]", encoding="utf-8")
    with pytest.raises(StorageIntegrityError, match="active pointer is unreadable"):
        store.active_revision(session_id)


def test_catalog_rebuild_uses_published_manifests_without_provider_io(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    session_id = "archive-2000-round-01-race"
    store.write_session(
        session_id,
        _tables(session_id),
        source_revision="a" * 64,
        manifest=_archive_manifest(session_id, validated_at="2026-08-27T00:00:00Z"),
    )
    catalog = HistoricalArchiveSync(tmp_path, store=store).rebuild_catalog()
    event = catalog.events[0]
    manifest = store.load_manifest(session_id)
    assert len(catalog.events) == 1
    assert event.race_session.quality.to_dict() == manifest["quality"]
    assert event.race_session.capabilities.to_dict() == manifest["capabilities"]
    assert [item.to_dict() for item in event.race_session.provenance] == manifest["provenance"]


def test_targeted_sync_repairs_only_requested_session_and_preserves_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = []
    for round_number in (1, 2):
        session_id = f"archive-2000-round-{round_number:02d}-race"
        tables = _tables(session_id)
        sources.append(
            ArchiveSourceRace(
                season=2000,
                round_number=round_number,
                name=f"Fixture Grand Prix {round_number}",
                event_date=f"2000-03-{10 + round_number:02d}",
                circuit_name=f"Fixture Circuit {round_number}",
                locality="Fixture City",
                country="Fixture Country",
                country_code="FIX",
                results=tables[ArchiveTableName.RESULTS],
                laps=tables[ArchiveTableName.LAPS],
                pit_stops=tables[ArchiveTableName.PIT_STOPS],
                drivers=("Driver One",),
                teams=("Team One",),
                provenance=_provenance(),
            )
        )

    class FakeReader:
        def __init__(self, *_: object) -> None:
            pass

        def races(self, *, start_year: int, end_year: int) -> object:
            return iter(source for source in sources if start_year <= source.season <= end_year)

    class FakeClient:
        def __init__(self) -> None:
            self.file_hash = "a" * 64

        def dump_descriptor(self) -> JolpicaDumpDescriptor:
            return JolpicaDumpDescriptor(self.file_hash, 1, "2026-08-27", "fixture://dump", 0)

        def fetch_dump(self, _: JolpicaDumpDescriptor) -> Path:
            return tmp_path / "unused.zip"

    monkeypatch.setattr(archive_sync_module, "JolpicaDumpReader", FakeReader)
    store = HistoricalArchiveStore(tmp_path)
    client = FakeClient()
    sync = HistoricalArchiveSync(tmp_path, store=store, client=client)  # type: ignore[arg-type]
    first = sync.sync(start_year=2000, end_year=2000, today=date(2001, 1, 1))
    assert len(first.catalog.events) == 2

    target = "archive-2000-round-01-race"
    unaffected = "archive-2000-round-02-race"
    unaffected_active = store.sessions_root / unaffected / "active.json"
    unaffected_mtime = unaffected_active.stat().st_mtime_ns
    target_manifest = store.load_manifest(target)
    target_revision = str(target_manifest["data_revision"])
    target_laps = store.sessions_root / target / "revisions" / target_revision / "laps.parquet"
    target_laps.write_bytes(b"corrupt")
    sleep(0.02)

    repaired = sync.sync(
        start_year=2000,
        end_year=2000,
        today=date(2001, 1, 1),
        session_id=target,
    )
    assert repaired.written == 1
    assert repaired.cache_hits == 0
    assert len(repaired.catalog.events) == 2
    assert unaffected_active.stat().st_mtime_ns == unaffected_mtime
    assert store.load_table(target, ArchiveTableName.LAPS).num_rows == 1

    target_active = store.sessions_root / target / "active.json"
    target_active.write_text("[]", encoding="utf-8")
    pointer_repaired = sync.sync(
        start_year=2000,
        end_year=2000,
        today=date(2001, 1, 1),
        session_id=target,
    )
    assert pointer_repaired.written == 1
    assert pointer_repaired.cache_hits == 0
    assert unaffected_active.stat().st_mtime_ns == unaffected_mtime
    assert store.load_table(target, ArchiveTableName.LAPS).num_rows == 1

    target_revision = store.active_revision(target)
    assert target_revision is not None
    target_manifest = store.sessions_root / target / "revisions" / target_revision / "manifest.json"
    target_manifest.write_text("[]", encoding="utf-8")
    manifest_repaired = sync.sync(
        start_year=2000,
        end_year=2000,
        today=date(2001, 1, 1),
        session_id=target,
    )
    assert manifest_repaired.written == 1
    assert store.load_manifest(target)["session_id"] == target

    client.file_hash = "b" * 64
    sources[0] = replace(
        sources[0],
        provenance=replace(sources[0].provenance, raw_sha256="b" * 64),
    )
    mixed = sync.sync(
        start_year=2000,
        end_year=2000,
        today=date(2001, 1, 1),
        session_id=target,
    )
    assert mixed.source_revision == mixed.catalog.source_revision
    assert mixed.source_revision.startswith("archive-mixed-source-sha256-")
    assert mixed.provider_source_revision != mixed.source_revision
    sync_state = store.load_sync_state()
    assert sync_state is not None
    assert sync_state["source_revision"] == mixed.source_revision
    assert sync_state["provider_source_revision"] == mixed.provider_source_revision
    quality_report = json.loads(store.quality_report_path.read_text("utf-8"))
    assert quality_report["source_revision"] == mixed.source_revision


def test_archive_repair_requires_session_id_before_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_module = importlib.import_module("downforce_core.cli.main")

    def unexpected_sync(*_: object, **__: object) -> None:
        pytest.fail("repair without --session-id must fail before sync construction")

    monkeypatch.setattr(cli_module, "HistoricalArchiveSync", unexpected_sync)
    args = cli_module._parser().parse_args(["--root", str(tmp_path), "archive", "repair"])

    with pytest.raises(ValueError, match="archive repair requires --session-id"):
        cli_module._run(args)


def test_catalog_round_trip_search_and_corruption_boundary(tmp_path: Path) -> None:
    store = HistoricalArchiveStore(tmp_path)
    event = _event()
    catalog = HistoricalCatalog(
        catalog_version="1.0.0",
        generated_at_utc="2026-08-27T00:00:00Z",
        archive_start_year=2000,
        latest_completed_event_id=event.event_id,
        latest_completed_event_date=event.event_date,
        source_revision="archive-source-sha256-" + "c" * 64,
        seasons=(HistoricalSeason(2000, (event,)),),
    )
    store.save_catalog(catalog)
    index = HistoricalCatalogIndex(store)
    found, total = index.query(driver="Schumacher", team="Ferrari", capability="lap_times")
    assert total == 1
    assert found[0].event_id == event.event_id
    assert index.seasons()[0]["completed_event_count"] == 1

    store.catalog_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(StorageIntegrityError):
        HistoricalCatalogIndex(store).catalog()
