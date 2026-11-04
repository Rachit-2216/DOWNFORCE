"""Explicit production corpus selection and read-only publication inspection."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]

from downforce_core.detailed import corpus_ingest, study_runner
from downforce_core.detailed.corpus_assessment import assess_corpus
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.status import guard_operation
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256


def add_command(commands: Any) -> None:
    parser = commands.add_parser("detailed", help="sync and inspect the detailed native corpus")
    actions = parser.add_subparsers(dest="detailed_action", required=True)
    sync = actions.add_parser("sync", help="resume a verified completed-session scope")
    scope = sync.add_mutually_exclusive_group()
    scope.add_argument("--all-completed", action="store_true")
    scope.add_argument("--session", action="append")
    years = sync.add_mutually_exclusive_group()
    years.add_argument("--season", type=int)
    years.add_argument("--from-season", type=int)
    sync.add_argument("--session-type", action="append")
    sync.add_argument("--limit", type=int)
    repair = actions.add_parser("repair", help="resume one failed or interrupted verified session")
    repair.add_argument("--session", action="append", required=True)
    repair.set_defaults(season=None, from_season=None, session_type=None, limit=None)
    catalog = actions.add_parser("catalog", help="rebuild a verified production publication index")
    catalog.add_argument("catalog_action", choices=("rebuild",))
    for command in (
        sync,
        repair,
        catalog,
        actions.add_parser("discover"),
        actions.add_parser("status"),
        actions.add_parser("validate"),
    ):
        command.add_argument("--data-root", type=Path)
    for command in (sync, repair):
        command.add_argument("--provider", choices=("fastf1", "openf1"), default="fastf1")
        command.add_argument(
            "--release-cache",
            action="store_true",
            help="release only verified recoverable production-owned staging copies",
        )


def _progress(row: dict[str, Any]) -> None:
    print(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False),
        file=sys.stderr,
        flush=True,
    )


def _inspect(project: Path, paths: DetailedPaths, *, validate: bool) -> dict[str, Any]:
    return assess_corpus(project, paths, validate=validate)


def corpus_command(args: argparse.Namespace) -> dict[str, Any]:
    project = Path(args.root).resolve()
    paths = DetailedPaths(project, override=args.data_root)
    if args.detailed_action == "discover":
        catalog, _sources = study_runner.ready_inventory(paths.root)
        return {
            "discovered": len(catalog["sessions"]),
            "states": dict(Counter(row["state"] for row in catalog["sessions"])),
            "sessions": catalog["sessions"],
            "provenance": "verified-captured-source-evidence",
            "provider_called": False,
        }
    if args.detailed_action == "catalog":
        catalog, _sources = study_runner.ready_inventory(paths.root)
        authority = file_sha256(_path(paths.root, "reconciled-inventory.json"))
        value = _inspect(project, paths, validate=True)
        if value["failures"]:
            raise ValueError("catalog rebuild refused: production artifact validation failed")
        expected = {row["session_id"]: row for row in catalog["sessions"]}
        store = DetailedStore(project, detailed_root=paths.root)
        for row in value["sessions"]:
            manifest = store.manifest(row["session_id"], revision=row["revision"])
            metadata = manifest["metadata"]
            if metadata["session"] != expected[row["session_id"]]:
                raise ValueError("production session differs from current source identity")
            row.update(
                session=metadata["session"],
                channels=metadata["channels"],
                availability=metadata["channel_availability"],
            )
        refreshed, _sources = study_runner.ready_inventory(paths.root)
        if catalog != refreshed or authority != file_sha256(
            _path(paths.root, "reconciled-inventory.json")
        ):
            raise ValueError("catalog source authority changed during rebuild")
        value.update(schema_version="detailed-production-catalog-v1", inventory_sha256=authority)
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
        target = _path(paths.root, "corpus-catalog.json")
        if not target.exists() or target.read_text() != encoded:
            guard_operation(
                target,
                next_peak_bytes=2 * len(encoded.encode()) + 4096,
                operation="production catalog rebuild",
            )
            write_text_atomic(target, encoded)
        return value
    if args.detailed_action not in {"sync", "repair"}:
        return _inspect(project, paths, validate=args.detailed_action == "validate")
    filters: dict[str, Any] = {
        "detailed_root": paths.root,
        "season": args.season,
        "from_season": args.from_season,
        "session_ids": set(args.session) if args.session else None,
        "session_types": set(args.session_type) if args.session_type else None,
        "limit": args.limit,
        "report": _progress,
    }
    try:
        if args.provider == "openf1":
            if args.release_cache:
                raise ValueError("cache release is only supported for the primary native corpus")
            from downforce_core.detailed import corpus_openf1

            return corpus_openf1.run_openf1_corpus(project, **filters)
        return corpus_ingest.run_corpus(
            project,
            **filters,
            release_cache=args.release_cache,
        )
    except RateLimitExceededError as exc:
        raise ValueError("provider rate limit reached; operation stopped without retry") from exc
