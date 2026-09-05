from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.analytics import (
    AnalyticsEngine,
    AnalyticsEntity,
    AnalyticsQuery,
    ComparisonMode,
    OutcomeCategory,
    RankingMetric,
)
from downforce_core.analytics.engine import _outcome
from downforce_core.analytics.storage import AnalyticsDerivedStore
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


def _row(
    session_id: str,
    driver_id: str,
    name: str,
    *,
    grid: int | None,
    finish: int | None,
    points: float,
    status: str,
    classified: bool,
    team: str = "team-one",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "driver_id": driver_id,
        "driver_code": driver_id[:3].upper(),
        "driver_name": name,
        "team_id": team,
        "team_name": team.replace("-", " ").title(),
        "car_number": 1,
        "grid_position": grid,
        "finish_position": finish,
        "points": points,
        "laps_completed": 50 if classified else 20,
        "status": status,
        "classified": classified,
        "total_time_ms": 5_000_000 if classified else None,
    }


def _fixture_store(tmp_path: Path, *, degraded_third_race: bool = False) -> HistoricalArchiveStore:
    store = HistoricalArchiveStore(tmp_path)
    provenance = ProviderProvenance(
        "jolpica", "fixture", "fixture", "https://example.invalid", "2026-08-31T00:00:00Z", "a" * 64
    )
    events: list[HistoricalEvent] = []
    race_rows = [
        [
            _row(
                "archive-2000-round-01-race",
                "driver-a",
                "Driver A",
                grid=3,
                finish=1,
                points=10,
                status="Finished",
                classified=True,
            ),
            _row(
                "archive-2000-round-01-race",
                "driver-b",
                "Driver B",
                grid=1,
                finish=None,
                points=0,
                status="Engine",
                classified=False,
                team="team-two",
            ),
            _row(
                "archive-2000-round-01-race",
                "driver-c",
                "Driver C",
                grid=None,
                finish=None,
                points=0,
                status="Did not start",
                classified=False,
                team="team-two",
            ),
            _row(
                "archive-2000-round-01-race",
                "driver-d",
                "Driver D",
                grid=4,
                finish=None,
                points=0,
                status="Disqualified",
                classified=False,
            ),
        ],
        [
            _row(
                "archive-2000-round-02-race",
                "driver-a",
                "Driver A",
                grid=1,
                finish=2,
                points=6,
                status="+1 Lap",
                classified=True,
            ),
            _row(
                "archive-2000-round-02-race",
                "driver-b",
                "Driver B",
                grid=2,
                finish=1,
                points=10,
                status="Finished",
                classified=True,
                team="team-two",
            ),
        ],
    ]
    if degraded_third_race:
        race_rows.append(
            [
                _row(
                    "archive-2000-round-03-race",
                    "driver-a",
                    "Driver A",
                    grid=1,
                    finish=1,
                    points=100,
                    status="Finished",
                    classified=True,
                )
            ]
        )
    for index, results_rows in enumerate(race_rows, start=1):
        session_id = f"archive-2000-round-{index:02d}-race"
        pit_supported = index == 2
        laps_rows = [
            {
                "session_id": session_id,
                "driver_id": str(row["driver_id"]),
                "lap_number": 1,
                "position": row["finish_position"],
                "lap_time_ms": 90_000 + index,
                "average_speed_kph": None,
                "is_fastest_lap": str(row["driver_id"]) == "driver-a",
            }
            for row in results_rows
            if row["status"] != "Did not start"
        ]
        pit_rows = (
            [
                {
                    "session_id": session_id,
                    "driver_id": "driver-a",
                    "stop_number": 1,
                    "lap_number": 25,
                    "duration_ms": 22_000,
                    "local_time": None,
                }
            ]
            if pit_supported
            else []
        )
        tables = {
            ArchiveTableName.RESULTS: pa.Table.from_pylist(
                results_rows, schema=ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS]
            ),
            ArchiveTableName.LAPS: pa.Table.from_pylist(
                laps_rows, schema=ARCHIVE_SCHEMAS[ArchiveTableName.LAPS]
            ),
            ArchiveTableName.PIT_STOPS: pa.Table.from_pylist(
                pit_rows, schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS]
            ),
        }
        revision = store.write_session(
            session_id,
            tables,
            source_revision=str(index) * 64,
            manifest={"event_id": f"event-2000-round-{index:02d}"},
        )
        quality_status = QualityStatus.DEGRADED if index == 3 else QualityStatus.VERIFIED
        quality = DataQuality(
            quality_status,
            (() if index != 3 else ("fixture degraded",)),
            {"result_rows": len(results_rows)},
            "2026-08-31T00:00:00Z",
        )
        capabilities = RaceDataCapabilities(
            results=True, grid=True, lap_times=True, lap_positions=True, pit_stops=pit_supported
        )
        session = HistoricalSession(
            session_id,
            "race",
            ArchiveEventStatus.COMPLETED,
            SyncLifecycle.COMPLETE,
            capabilities,
            quality,
            (provenance,),
            {"results": len(results_rows), "laps": len(laps_rows), "pit_stops": len(pit_rows)},
            revision,
        )
        events.append(
            HistoricalEvent(
                f"event-2000-round-{index:02d}",
                2000,
                index,
                f"Fixture Grand Prix {index}",
                f"Fixture Grand Prix {index}",
                f"2000-03-{index:02d}",
                "Fixture Circuit",
                "Fixture",
                "Test",
                "TST",
                ArchiveEventStatus.COMPLETED,
                (session,),
                tuple(str(row["driver_name"]) for row in results_rows),
                tuple(sorted({str(row["team_name"]) for row in results_rows})),
            )
        )
    store.save_catalog(
        HistoricalCatalog(
            "1.0.0",
            "2026-08-31T00:00:00Z",
            2000,
            events[-1].event_id,
            events[-1].event_date,
            "archive-source-sha256-" + "f" * 64,
            (HistoricalSeason(2000, tuple(events)),),
        )
    )
    return store


def test_metric_contract_and_outcomes_use_recorded_facts(tmp_path: Path) -> None:
    engine = AnalyticsEngine(_fixture_store(tmp_path))
    profile = engine.driver("driver-a", AnalyticsQuery(2000, 2000))
    summary = cast(dict[str, object], profile["summary"])
    assert summary["points"] == 16.0  # Recorded 10+6; no modern scoring reconstruction.
    assert summary["wins"] == 1
    assert summary["positions_gained"] == 1
    observations = {
        item.driver_id: item for item in engine.snapshot().observations if item.round_number == 1
    }
    assert observations["driver-b"].outcome is OutcomeCategory.DNF
    assert observations["driver-c"].outcome is OutcomeCategory.DNS
    assert observations["driver-d"].outcome is OutcomeCategory.DSQ
    assert observations["driver-b"].positions_gained is None
    assert summary["average_finish_samples"] == 2
    assert summary["positions_gained_samples"] == 2


@pytest.mark.parametrize(
    ("status", "classified", "laps", "expected"),
    [
        ("Finished", False, 58, OutcomeCategory.FINISHED),
        ("Retired", True, 55, OutcomeCategory.CLASSIFIED),
        ("Withdrew", True, 53, OutcomeCategory.CLASSIFIED),
        ("Withdrew", False, 0, OutcomeCategory.DNS),
        ("Withdrew", False, 48, OutcomeCategory.DNF),
        ("Illness", False, 0, OutcomeCategory.DNS),
        ("Illness", False, 47, OutcomeCategory.DNF),
        ("Safety concerns", False, 0, OutcomeCategory.DNS),
        ("Collision", False, 0, OutcomeCategory.DNF),
        ("Did not start", False, 0, OutcomeCategory.DNS),
        ("Disqualified", True, 58, OutcomeCategory.DSQ),
    ],
)
def test_outcome_mapping_preserves_provider_classification_and_explicit_starts(
    status: str,
    classified: bool,
    laps: int,
    expected: OutcomeCategory,
) -> None:
    assert _outcome(status, classified, laps) is expected


def test_comparison_rankings_pit_coverage_and_pagination(tmp_path: Path) -> None:
    engine = AnalyticsEngine(_fixture_store(tmp_path))
    comparison = engine.compare(
        AnalyticsEntity.DRIVER,
        "driver-a",
        "driver-b",
        AnalyticsQuery(2000, 2000),
        mode=ComparisonMode.COMMON_RACES,
    )
    assert comparison["common_race_count"] == 2
    assert comparison["head_to_head"] == {
        "a_finished_ahead": 0,
        "b_finished_ahead": 1,
        "tied": 0,
        "excluded_non_comparable": 1,
        "teammate_races": 0,
        "denominator": 1,
    }
    constructor_comparison = engine.compare(
        AnalyticsEntity.CONSTRUCTOR,
        "team-one",
        "team-two",
        AnalyticsQuery(2000, 2000),
    )
    assert constructor_comparison["head_to_head"] == {
        "a_finished_ahead": 1,
        "b_finished_ahead": 1,
        "tied": 0,
        "excluded_non_comparable": 0,
        "teammate_races": 0,
        "denominator": 2,
    }
    constructors = engine.constructors(AnalyticsQuery(2000, 2000))
    team_one = next(
        item
        for item in cast(list[dict[str, object]], constructors["items"])
        if item["entity_id"] == "team-one"
    )
    assert team_one["starts"] == 2
    assert constructors["coverage"]["sample_count"] == 6
    ranking = engine.rankings(
        AnalyticsEntity.DRIVER,
        RankingMetric.POINTS,
        AnalyticsQuery(2000, 2000),
        minimum_starts=2,
        offset=0,
        limit=1,
    )
    assert ranking["total"] == 2
    assert len(cast(list[object], ranking["items"])) == 1
    season = engine.season(2000)
    assert len(cast(list[object], season["constructor_points_progression"])) == 2
    coverage = cast(dict[str, dict[str, object]], season["coverage"])
    assert coverage["pits"]["race_count"] == 1
    assert coverage["pits"]["sample_count"] == 1


def test_quality_filter_and_derived_reload_are_deterministic(tmp_path: Path) -> None:
    store = _fixture_store(tmp_path, degraded_third_race=True)
    engine = AnalyticsEngine(store)
    manifest = engine.rebuild_manifest()
    derived = cast(dict[str, object], manifest["derived_store"])
    assert derived["observation_rows"] == 6
    profile = engine.driver("driver-a", AnalyticsQuery(2000, 2000))
    assert cast(dict[str, object], profile["summary"])["points"] == 16.0
    report = engine.coverage_report()
    metrics = cast(dict[str, dict[str, object]], report["metrics"])
    assert metrics["result_metrics"]["quality_exclusions"] == 1
    reloaded = AnalyticsEngine(store).snapshot()
    assert reloaded.digest == engine.snapshot().digest
    assert len(reloaded.observations) == 6


def test_result_cache_is_bounded(tmp_path: Path) -> None:
    engine = AnalyticsEngine(_fixture_store(tmp_path))
    query = AnalyticsQuery(2000, 2000)
    for offset in range(100):
        engine.drivers(query, offset=offset, limit=1)
    assert len(engine._cache) == engine._cache_limit == 64


def test_derived_store_rejects_revision_version_and_corruption_then_repairs(
    tmp_path: Path,
) -> None:
    archive = _fixture_store(tmp_path)
    engine = AnalyticsEngine(archive)
    engine.rebuild_manifest()
    derived = AnalyticsDerivedStore(tmp_path)
    source_revision = archive.load_catalog().source_revision
    assert derived.load(source_revision) is not None

    manifest = json.loads(derived.manifest_path.read_text("utf-8"))
    derived.manifest_path.write_text(
        json.dumps({**manifest, "analytics_version": "0.0.0"}),
        encoding="utf-8",
    )
    assert derived.load(source_revision) is None
    derived.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    parquet = derived.root / "driver-race.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"corrupt")
    assert derived.load(source_revision) is None
    repaired = AnalyticsEngine(archive).rebuild_manifest()
    assert repaired["driver_race_observations"] == 6
    assert derived.load(source_revision) is not None

    revised_catalog = replace(
        archive.load_catalog(),
        source_revision="archive-source-sha256-" + "e" * 64,
    )
    archive.save_catalog(revised_catalog)
    assert derived.load(revised_catalog.source_revision) is None
    rebuilt = AnalyticsEngine(archive).snapshot()
    assert rebuilt.source_revision == revised_catalog.source_revision
    assert len(rebuilt.observations) == 6


def test_failed_derived_publish_restores_previous_active_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _fixture_store(tmp_path)
    engine = AnalyticsEngine(archive)
    engine.rebuild_manifest()
    derived = engine.derived_store
    source_revision = archive.load_catalog().source_revision
    original_manifest = derived.manifest_path.read_bytes()
    real_replace = os.replace

    def fail_staging_activation(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.endswith(".tmp") and destination_path == derived.root:
            raise OSError("fixture activation interruption")
        real_replace(source, destination)

    monkeypatch.setattr("downforce_core.analytics.storage.os.replace", fail_staging_activation)
    snapshot = engine.snapshot(force=True)
    with pytest.raises(OSError, match="activation interruption"):
        derived.publish(
            snapshot.observations,
            source_revision=source_revision,
            snapshot_digest=snapshot.digest,
            built_at_utc=snapshot.built_at_utc,
            provider_circuit_identities=snapshot.provider_circuit_identities,
        )
    assert derived.manifest_path.read_bytes() == original_manifest
    assert derived.load(source_revision) is not None
