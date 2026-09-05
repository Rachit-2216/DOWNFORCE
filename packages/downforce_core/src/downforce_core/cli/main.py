"""Small dependency-free command interface for ingestion and repository inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

from downforce_core.analytics import AnalyticsEngine
from downforce_core.archive import (
    ArchiveEventStatus,
    ArchiveTableName,
    HistoricalArchiveStore,
    HistoricalArchiveSync,
)
from downforce_core.exceptions import DownforceError
from downforce_core.ingestion import IngestionResult, ingest_session
from downforce_core.ml import (
    ArtifactStore,
    MLInferenceEngine,
    build_dataset,
    load_corpus,
    train_bundle,
    write_dataset,
)
from downforce_core.providers import RaceDataProvider, SessionRef
from downforce_core.replay import ReplayEngine, state_to_dict
from downforce_core.storage.repository import DownforceRepository
from downforce_core.strategy import PitAction, ScenarioAssumptions, Strategy, StrategyEngine


def _event_selector(value: str) -> int | str:
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError("event must be a round number or nonempty event name")
    return int(stripped) if stripped.isdecimal() else stripped


def _pit_action(value: str) -> PitAction:
    try:
        lap_value, compound_value = value.split(":", maxsplit=1)
        from downforce_core.domain.enums import TyreCompound

        return PitAction(int(lap_value), TyreCompound(compound_value.casefold()))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("pit must use LAP:soft|medium|hard") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="downforce", description="DOWNFORCE historical data CLI")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="project root containing .downforce (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest or reuse a historical session")
    ingest.add_argument("--provider", choices=("fastf1",), default="fastf1")
    ingest.add_argument("--season", type=int, required=True)
    ingest.add_argument("--event", type=_event_selector, required=True)
    ingest.add_argument("--session", required=True)
    ingest.add_argument("--force", action="store_true")
    inspect = commands.add_parser("inspect", help="inspect a verified canonical session")
    inspect.add_argument("--session-id", required=True)
    state = commands.add_parser("state", help="query deterministic RaceState")
    state.add_argument("--session-id", required=True)
    cursor = state.add_mutually_exclusive_group(required=True)
    cursor.add_argument("--time-ms", type=int)
    cursor.add_argument("--lap", type=int)
    state.add_argument("--phase", choices=("start", "end"), default="end")
    ml = commands.add_parser("ml", help="build, train, evaluate, or inspect historical ML")
    ml.add_argument(
        "action",
        choices=("audit", "dataset", "train", "evaluate", "status", "pipeline"),
    )
    ml.add_argument(
        "--corpus",
        type=Path,
        default=Path("docs/ml/benchmark-corpus.json"),
    )
    ml.add_argument("--seed", type=int, default=2216)
    strategy = commands.add_parser("strategy", help="run hypothetical strategy engineering")
    strategy_actions = strategy.add_subparsers(dest="strategy_action", required=True)
    strategy_actions.add_parser("status", help="inspect strategy engine availability")
    for action in ("simulate", "compare", "backtest"):
        strategy_command = strategy_actions.add_parser(action)
        strategy_command.add_argument("--session-id", required=True)
        strategy_command.add_argument("--driver-id", required=True)
        strategy_command.add_argument("--time-ms", required=True, type=int)
        strategy_command.add_argument(
            "--total-laps",
            type=int,
            help="explicit scheduled-distance override; defaults to causal published metadata",
        )
        strategy_command.add_argument("--simulations", type=int, default=500)
        strategy_command.add_argument("--seed", type=int, default=2216)
        strategy_command.add_argument(
            "--pit",
            type=_pit_action,
            action="append",
            default=[],
            help="pit action as LAP:soft|medium|hard; repeat for multiple stops",
        )
    archive = commands.add_parser("archive", help="sync and inspect the broad race archive")
    archive.add_argument(
        "action",
        choices=("sync", "status", "validate", "rebuild", "repair"),
    )
    archive.add_argument("--start-year", type=int, default=2000)
    archive.add_argument("--end-year", type=int)
    archive.add_argument("--completed-only", action="store_true")
    archive.add_argument("--session-id")
    analytics = commands.add_parser(
        "analytics", help="build and inspect deterministic historical analytics"
    )
    analytics.add_argument("action", choices=("rebuild", "status", "coverage"))
    return parser


def _result_dict(result: IngestionResult) -> dict[str, object]:
    return {
        "session_id": result.session_id,
        "snapshot_id": result.snapshot_id,
        "dataset_id": result.dataset_id,
        "cache_hit": result.cache_hit,
        "provider_called": result.provider_called,
        "raw_snapshot_reused": result.raw_snapshot_reused,
        "table_counts": dict(result.table_counts),
        "warnings": list(result.warnings),
        "timings_ms": {name: round(value, 3) for name, value in result.timings_ms.items()},
    }


def _run(args: argparse.Namespace) -> dict[str, object]:
    root = Path(args.root).resolve()
    repository = DownforceRepository(root)
    if args.command == "analytics":
        engine = AnalyticsEngine(HistoricalArchiveStore(root))
        if args.action == "rebuild":
            return engine.rebuild_manifest()
        if args.action == "coverage":
            return engine.coverage_report()
        return engine.status()
    if args.command == "archive":
        if args.action == "repair" and args.session_id is None:
            raise ValueError("archive repair requires --session-id")
        archive_store = HistoricalArchiveStore(root)
        archive_sync = HistoricalArchiveSync(root, store=archive_store)
        if args.action == "rebuild":
            catalog = archive_sync.rebuild_catalog()
            return {
                "catalog_version": catalog.catalog_version,
                "season_count": len(catalog.seasons),
                "event_count": len(catalog.events),
                "completed_event_count": sum(
                    event.status is ArchiveEventStatus.COMPLETED for event in catalog.events
                ),
                "source_revision": catalog.source_revision,
                "provider_called": False,
            }
        if args.action in {"sync", "repair"}:
            target_session = args.session_id if args.action == "repair" else None
            start_year = args.start_year
            end_year = args.end_year
            if target_session is not None:
                match = re.fullmatch(r"archive-(\d{4})-round-\d{2}-race", target_session)
                if match is None:
                    raise ValueError("archive session ID is invalid")
                start_year = end_year = int(match.group(1))
            result = archive_sync.sync(
                start_year=start_year,
                end_year=end_year,
                include_upcoming=not args.completed_only,
                session_id=target_session,
            )
            output = result.to_dict()
            if args.action == "repair" and args.session_id is not None:
                output["repaired_session"] = args.session_id
                output["active_revision"] = archive_store.active_revision(args.session_id)
            return output
        if args.action == "status":
            catalog = archive_store.load_catalog()
            return {
                "catalog_version": catalog.catalog_version,
                "source_revision": catalog.source_revision,
                "season_count": len(catalog.seasons),
                "event_count": len(catalog.events),
                "completed_event_count": sum(
                    event.status is ArchiveEventStatus.COMPLETED for event in catalog.events
                ),
                "latest_completed_event_id": catalog.latest_completed_event_id,
                "latest_completed_event_date": catalog.latest_completed_event_date,
                "sync": archive_store.load_sync_state(),
                "storage": archive_store.storage_report(),
            }
        catalog = archive_store.load_catalog()
        failures: list[dict[str, str]] = []
        verified = 0
        matched = 0
        for event in catalog.events:
            archive_session = event.race_session
            if archive_session.status is not ArchiveEventStatus.COMPLETED:
                continue
            if args.session_id is not None and archive_session.session_id != args.session_id:
                continue
            matched += 1
            try:
                archive_manifest = archive_store.load_manifest(archive_session.session_id)
                if archive_manifest.get("data_revision") != archive_session.data_revision:
                    raise ValueError("catalog and active revision differ")
                expected_metadata: dict[str, object] = {
                    "event_id": event.event_id,
                    "season": event.season,
                    "round_number": event.round_number,
                    "event_name": event.name,
                    "official_name": event.official_name,
                    "event_date": event.event_date,
                    "circuit_name": event.circuit_name,
                    "locality": event.locality,
                    "country": event.country,
                    "country_code": event.country_code,
                    "drivers": list(event.drivers),
                    "teams": list(event.teams),
                    "legacy_session_id": archive_session.legacy_session_id,
                    "capabilities": archive_session.capabilities.to_dict(),
                    "quality": archive_session.quality.to_dict(),
                    "provenance": [item.to_dict() for item in archive_session.provenance],
                }
                for name, expected in expected_metadata.items():
                    if archive_manifest.get(name) != expected:
                        raise ValueError(f"catalog and manifest differ for {name}")
                for table_name in ArchiveTableName:
                    table = archive_store.load_table(archive_session.session_id, table_name)
                    if table.num_rows != archive_session.row_counts[table_name.value]:
                        raise ValueError(f"row count differs for {table_name.value}")
                verified += 1
            except (DownforceError, OSError, ValueError, KeyError) as exc:
                failures.append(
                    {
                        "session_id": archive_session.session_id,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        if args.session_id is not None and matched == 0:
            failures.append(
                {
                    "session_id": args.session_id,
                    "error": "ValueError",
                    "message": "archive session is not a completed catalog session",
                }
            )
        return {
            "status": "verified" if not failures else "failed",
            "verified_sessions": verified,
            "failed_sessions": len(failures),
            "failures": failures,
        }
    if args.command == "ingest":
        reference = SessionRef(args.season, args.event, args.session)

        def provider_factory() -> RaceDataProvider:
            from downforce_core.providers.fastf1_provider import FastF1Provider

            return FastF1Provider(root)

        ingestion_result = asyncio.run(
            ingest_session(
                repository,
                reference,
                provider_factory,
                force=bool(args.force),
            )
        )
        return _result_dict(ingestion_result)
    if args.command == "inspect":
        manifest = repository.load_manifest(args.session_id)
        serialized = manifest.to_dict()
        return {
            "session_id": manifest.session_id,
            "snapshot_id": manifest.snapshot_id,
            "dataset_id": manifest.dataset_id,
            "session": serialized["session"],
            "provider": serialized["provider"],
            "completeness": serialized["completeness"],
            "table_counts": {
                name: artifact.row_count for name, artifact in manifest.tables.items()
            },
            "warnings": list(manifest.warnings),
        }
    if args.command == "state":
        replay_engine = ReplayEngine.from_repository(repository, args.session_id)
        if args.time_ms is not None:
            return state_to_dict(replay_engine.state_at(args.time_ms))
        return state_to_dict(replay_engine.state_at_lap(args.lap, phase=args.phase))
    if args.command == "strategy":
        strategy_engine = StrategyEngine(repository, root)
        if args.strategy_action == "status":
            return strategy_engine.status()
        scenario = ScenarioAssumptions(scheduled_total_laps=args.total_laps)
        strategy = Strategy(
            "cli-candidate",
            "CLI candidate",
            tuple(args.pit),
        )
        if args.strategy_action in {"simulate", "backtest"}:
            simulation_result = strategy_engine.simulate(
                session_id=args.session_id,
                driver_id=args.driver_id,
                cursor_ms=args.time_ms,
                strategy=strategy,
                scenario=scenario,
                simulations=args.simulations,
                seed=args.seed,
            )
            if (
                args.strategy_action == "backtest"
                and simulation_result.get("status") == "available"
            ):
                session = repository.load_session(args.session_id, include_track_positions=False)
                observed = next(
                    (
                        row
                        for row in session.classifications
                        if str(row.driver_id) == args.driver_id
                    ),
                    None,
                )
                return {
                    **simulation_result,
                    "observed_historical_result": {
                        "label": "Observed after simulation; never a simulation input",
                        "classified_position": (
                            None if observed is None else observed.classified_position
                        ),
                        "status": None if observed is None else observed.status.value,
                    },
                }
            return simulation_result
        state = strategy_engine.build_state(
            args.session_id, args.driver_id, args.time_ms, scenario, args.seed
        )
        current = next(item for item in state.drivers if item.driver_id == args.driver_id)
        candidates = strategy_engine.generate_candidates(
            driver_laps_completed=current.laps_completed,
            scheduled_total_laps=state.scheduled_total_laps,
            current_compound=current.compound,
        )
        return strategy_engine.compare(
            session_id=args.session_id,
            driver_id=args.driver_id,
            cursor_ms=args.time_ms,
            strategies=candidates,
            scenario=scenario,
            simulations=args.simulations,
            seed=args.seed,
        )
    if args.command == "ml":
        store = ArtifactStore(root)
        if args.action == "status":
            status = MLInferenceEngine(repository, root).status()
            if status["availability"] == "unavailable":
                return status
            bundle = store.load()
            return {**status, "split_counts": bundle.get("split_counts")}
        corpus_path = args.corpus if args.corpus.is_absolute() else root / args.corpus
        corpus = load_corpus(corpus_path)
        build_started = perf_counter()
        dataset = build_dataset(repository, corpus)
        dataset_build_ms = (perf_counter() - build_started) * 1_000
        dataset_path = root / ".downforce" / "ml" / "datasets" / f"{dataset.digest}.json"
        if args.action == "audit":
            return {
                "dataset_digest": dataset.digest,
                "sessions": len(corpus),
                "pace_examples": len(dataset.pace),
                "pit_loss_examples": len(dataset.pit_loss),
                "source_datasets": [list(item) for item in dataset.source_datasets],
                "row_rejections": dict(dataset.row_rejections),
                "dataset_build_ms": round(dataset_build_ms, 3),
            }
        write_started = perf_counter()
        write_dataset(dataset, dataset_path)
        dataset_write_ms = (perf_counter() - write_started) * 1_000
        if args.action == "dataset":
            return {
                "dataset_digest": dataset.digest,
                "path": str(dataset_path),
                "pace_examples": len(dataset.pace),
                "pit_loss_examples": len(dataset.pit_loss),
                "row_rejections": dict(dataset.row_rejections),
                "timings_ms": {
                    "dataset_build": round(dataset_build_ms, 3),
                    "dataset_write": round(dataset_write_ms, 3),
                },
            }
        training_started = perf_counter()
        bundle = train_bundle(dataset, seed=args.seed)
        training_ms = (perf_counter() - training_started) * 1_000
        if args.action == "evaluate":
            return {
                **bundle,
                "timings_ms": {
                    "dataset_build": round(dataset_build_ms, 3),
                    "dataset_write": round(dataset_write_ms, 3),
                    "training": round(training_ms, 3),
                },
            }
        publish_started = perf_counter()
        bundle_id, digest = store.publish(bundle)
        artifact_publish_ms = (perf_counter() - publish_started) * 1_000
        return {
            "dataset_digest": dataset.digest,
            "dataset_path": str(dataset_path),
            "bundle_id": bundle_id,
            "bundle_sha256": digest,
            "split_counts": bundle["split_counts"],
            "timings_ms": {
                "dataset_build": round(dataset_build_ms, 3),
                "dataset_write": round(dataset_write_ms, 3),
                "training": round(training_ms, 3),
                "artifact_publish": round(artifact_publish_ms, 3),
            },
            "test": {
                "pace": bundle["pace"],
                "tyre_degradation": bundle["tyre_degradation"],
                "pit_loss": bundle["pit_loss"],
            },
        }
    raise AssertionError("argparse accepted an unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        output = _run(args)
    except (DownforceError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["main"]
