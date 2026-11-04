"""Read-only, operation-bound current storage and canonical inventory authority."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.status import guard_operation, storage_status
from downforce_core.detailed.study_runner import _path, ready_inventory
from downforce_core.storage.parquet import file_sha256


class CorpusAuthorityStop(ValueError):
    """Stale corpus authority stops the whole provider operation, never just one row."""


def select_sessions(
    rows: Iterable[dict[str, Any]],
    *,
    season: int | None = None,
    from_season: int | None = None,
    session_ids: set[str] | None = None,
    session_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    if season is not None and from_season is not None:
        raise ValueError("season and from-season are mutually exclusive")
    current = datetime.now(UTC).year
    for value in (season, from_season):
        if value is not None and (type(value) is not int or not 2018 <= value <= current):
            raise ValueError("season filter is outside 2018-current")
    rows = list(rows)
    by_id = {row["session_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate canonical session identities")
    if session_ids is not None and (
        not session_ids
        or any(
            identity not in by_id or by_id[identity].get("state") != "completed"
            for identity in session_ids
        )
    ):
        raise ValueError("requested session is unknown, noncompleted or empty")
    known_types = {row["session_type"] for row in rows}
    if session_types is not None and (not session_types or not session_types.issubset(known_types)):
        raise ValueError("requested session type is unknown or empty")
    selected = [
        row
        for row in rows
        if row.get("state") == "completed"
        and 2018 <= row["season"] <= current
        and (season is None or row["season"] == season)
        and (from_season is None or row["season"] >= from_season)
        and (session_types is None or row["session_type"] in session_types)
    ]
    if len({row["session_id"] for row in selected}) != len(selected):
        raise ValueError("duplicate completed session identities")
    if session_ids is not None:
        if not session_ids.issubset({row["session_id"] for row in selected}):
            raise ValueError("requested session is outside 2018-current or conflicts with filters")
        selected = [row for row in selected if row["session_id"] in session_ids]
    return sorted(selected, key=lambda row: row["session_id"])


class CorpusGate:
    def __init__(self, paths: DetailedPaths) -> None:
        self.paths = paths
        self.bindings = self._bindings()
        status = storage_status(paths)  # Replays immutable proof; never trust saved safe_to_sync.
        projection = status.get("current_projection")
        if (
            status.get("projection_current") is not True
            or status.get("current_safety") != "verified"
            or not isinstance(projection, dict)
            or projection.get("safe_to_sync") is not True
            or projection.get("chosen_design") != "B"
            or projection.get("planned_samples_complete") is not True
        ):
            raise ValueError("a fresh verified storage gate for Design B is required")
        self.measurements = {row["session_id"]: row for row in projection["measurements"]}
        self.peak = max(2 * 1024**3, projection["peak_working_bytes"])
        self.catalog, self.sources = ready_inventory(paths.root)
        self.check()

    def _bindings(self) -> dict[str, str]:
        try:
            return {
                name: file_sha256(_path(self.paths.root, name))
                for name in (
                    "reconciled-inventory.json",
                    "study/sample-plan.json",
                    "stratified-projection.json",
                )
            }
        except OSError as exc:
            raise ValueError("a fresh verified storage gate is unavailable") from exc

    def check(self, entry: dict[str, Any] | None = None, *, peak: int = 0) -> None:
        try:
            bindings = self._bindings()
        except Exception as exc:
            raise CorpusAuthorityStop("storage gate bindings became unavailable") from exc
        if bindings != self.bindings:
            raise CorpusAuthorityStop(
                "storage gate catalog, plan or projection changed during operation"
            )
        try:
            catalog, sources = ready_inventory(self.paths.root)
        except Exception as exc:
            raise CorpusAuthorityStop(
                "current canonical inventory authority is stale or invalid"
            ) from exc
        if catalog != self.catalog or sources != self.sources:
            raise CorpusAuthorityStop(
                "current canonical inventory authority changed during operation"
            )
        if entry is not None:
            matching = [
                row for row in catalog["sessions"] if row["session_id"] == entry["session_id"]
            ]
            if matching != [entry] or entry.get("state") != "completed":
                raise CorpusAuthorityStop(
                    "session differs from current completed canonical inventory"
                )
        guard_operation(
            self.paths.root,
            next_peak_bytes=max(self.peak, peak),
            operation="production corpus next operation",
        )
