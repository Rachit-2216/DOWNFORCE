"""Reusable, quality-aware analytics over immutable archive observations."""

from __future__ import annotations

import json
import os
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from statistics import median
from threading import RLock
from typing import cast
from uuid import uuid4

from downforce_core.analytics.circuits import CircuitIdentityResolver
from downforce_core.analytics.contracts import (
    ANALYTICS_VERSION,
    AnalyticsEntity,
    AnalyticsQuery,
    ComparisonMode,
    Coverage,
    DriverRaceObservation,
    OutcomeCategory,
    RankingMetric,
    deterministic_key,
)
from downforce_core.analytics.storage import AnalyticsDerivedStore
from downforce_core.archive import ArchiveEventStatus, ArchiveTableName, HistoricalArchiveStore
from downforce_core.archive.contracts import HistoricalCatalog, HistoricalEvent
from downforce_core.exceptions import SessionNotFoundError


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    source_revision: str
    observations: tuple[DriverRaceObservation, ...]
    catalog: HistoricalCatalog
    digest: str
    built_at_utc: str
    provider_circuit_identities: int


def _optional_int(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(float, value))


def _outcome(
    status: object,
    classified: bool,
    laps_completed: int | None,
) -> OutcomeCategory:
    value = str(status or "").strip().casefold()
    if "disqual" in value or "excluded" in value:
        return OutcomeCategory.DSQ
    if value == "finished":
        return OutcomeCategory.FINISHED
    if classified or (value.startswith("+") and "lap" in value):
        return OutcomeCategory.CLASSIFIED
    explicit_non_start = any(
        term in value
        for term in (
            "did not start",
            "did not qualify",
            "did not prequalify",
            "not started",
        )
    )
    zero_lap_pre_start = (laps_completed is None or laps_completed == 0) and (
        value in {"withdrew", "injury", "injured", "illness", "safety", "safety concerns"}
        or value.startswith("withdraw")
    )
    if explicit_non_start or zero_lap_pre_start:
        return OutcomeCategory.DNS
    if value:
        return OutcomeCategory.DNF
    return OutcomeCategory.OTHER


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _average(values: Iterable[int | float]) -> float | None:
    items = list(values)
    return None if not items else sum(items) / len(items)


def _race_ids(observations: Iterable[DriverRaceObservation]) -> set[str]:
    return {item.session_id for item in observations}


class AnalyticsEngine:
    """Build one derived driver-race table and reuse it for every analytics surface."""

    def __init__(
        self,
        store: HistoricalArchiveStore,
        *,
        circuit_resolver: CircuitIdentityResolver | None = None,
    ) -> None:
        self.store = store
        self.circuit_resolver = circuit_resolver or CircuitIdentityResolver.from_store(store)
        self.derived_store = AnalyticsDerivedStore(store.project_root)
        self._lock = RLock()
        self._snapshot: AnalyticsSnapshot | None = None
        self._catalog_mtime_ns: int | None = None
        self._cache: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._cache_limit = 64

    def snapshot(self, *, force: bool = False) -> AnalyticsSnapshot:
        try:
            catalog_mtime_ns = self.store.catalog_path.stat().st_mtime_ns
        except OSError:
            catalog_mtime_ns = None
        with self._lock:
            if (
                not force
                and self._snapshot is not None
                and self._catalog_mtime_ns == catalog_mtime_ns
            ):
                return self._snapshot
        catalog = self.store.load_catalog()
        with self._lock:
            if (
                not force
                and self._snapshot is not None
                and self._snapshot.source_revision == catalog.source_revision
            ):
                self._catalog_mtime_ns = catalog_mtime_ns
                return self._snapshot
            stored = None if force else self.derived_store.load(catalog.source_revision)
            snapshot = (
                self._build_snapshot(catalog)
                if stored is None
                else AnalyticsSnapshot(
                    source_revision=catalog.source_revision,
                    observations=stored.observations,
                    catalog=catalog,
                    digest=stored.digest,
                    built_at_utc=stored.built_at_utc,
                    provider_circuit_identities=stored.provider_circuit_identities,
                )
            )
            self._snapshot = snapshot
            self._catalog_mtime_ns = catalog_mtime_ns
            self._cache.clear()
            return snapshot

    def _build_snapshot(self, catalog: HistoricalCatalog) -> AnalyticsSnapshot:
        observations: list[DriverRaceObservation] = []
        for event in catalog.events:
            if event.status is not ArchiveEventStatus.COMPLETED:
                continue
            if event.race_session.quality.status.value not in {"verified", "good"}:
                continue
            observations.extend(self._event_observations(event))
        observations.sort(
            key=lambda item: (item.season, item.round_number, item.finish_position or 10_000)
        )
        digest_payload = "\n".join(
            json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":"))
            for item in observations
        ).encode("utf-8")
        return AnalyticsSnapshot(
            source_revision=catalog.source_revision,
            observations=tuple(observations),
            catalog=catalog,
            digest=sha256(digest_payload).hexdigest(),
            built_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            provider_circuit_identities=self.circuit_resolver.provider_identity_count(),
        )

    def _event_observations(self, event: HistoricalEvent) -> list[DriverRaceObservation]:
        session = event.race_session
        results = cast(
            list[dict[str, object]],
            self.store.load_table(session.session_id, ArchiveTableName.RESULTS).to_pylist(),
        )
        laps = cast(
            list[dict[str, object]],
            self.store.load_table(session.session_id, ArchiveTableName.LAPS).to_pylist(),
        )
        pits = cast(
            list[dict[str, object]],
            self.store.load_table(session.session_id, ArchiveTableName.PIT_STOPS).to_pylist(),
        )
        laps_by_driver: dict[str, list[dict[str, object]]] = defaultdict(list)
        pits_by_driver: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in laps:
            laps_by_driver[str(row["driver_id"])].append(row)
        for row in pits:
            pits_by_driver[str(row["driver_id"])].append(row)
        circuit_id = self.circuit_resolver.resolve(
            event.season, event.round_number, event.circuit_name
        )
        observations: list[DriverRaceObservation] = []
        for row in results:
            driver_id = str(row["driver_id"])
            driver_laps = laps_by_driver.get(driver_id, [])
            timed_laps = [
                int(cast(int, item["lap_time_ms"]))
                for item in driver_laps
                if item.get("lap_time_ms") is not None
            ]
            driver_pits = pits_by_driver.get(driver_id, [])
            pit_durations = [
                int(cast(int, item["duration_ms"]))
                for item in driver_pits
                if item.get("duration_ms") is not None
            ]
            classified = bool(row.get("classified", False))
            laps_completed = _optional_int(row.get("laps_completed"))
            outcome = _outcome(row.get("status"), classified, laps_completed)
            grid = _optional_int(row.get("grid_position"))
            finish = _optional_int(row.get("finish_position"))
            positions_gained = (
                grid - finish
                if grid is not None
                and finish is not None
                and grid > 0
                and finish > 0
                and outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                else None
            )
            observations.append(
                DriverRaceObservation(
                    event_id=event.event_id,
                    session_id=session.session_id,
                    season=event.season,
                    round_number=event.round_number,
                    event_name=event.name,
                    event_date=event.event_date,
                    circuit_id=circuit_id,
                    circuit_name=event.circuit_name,
                    driver_id=driver_id,
                    driver_name=str(row["driver_name"]),
                    constructor_id=(None if row.get("team_id") is None else str(row["team_id"])),
                    constructor_name=(
                        None if row.get("team_name") is None else str(row["team_name"])
                    ),
                    grid_position=grid,
                    finish_position=finish,
                    points=_optional_float(row.get("points")) or 0.0,
                    laps_completed=laps_completed,
                    outcome=outcome,
                    classified=classified,
                    positions_gained=positions_gained,
                    recorded_lap_count=len(driver_laps),
                    timed_lap_count=len(timed_laps),
                    raw_mean_lap_ms=_average(timed_laps),
                    raw_median_lap_ms=(None if not timed_laps else float(median(timed_laps))),
                    best_recorded_lap_ms=min(timed_laps, default=None),
                    fastest_lap_recorded=any(
                        bool(item.get("is_fastest_lap", False)) for item in driver_laps
                    ),
                    pit_stop_count=(len(driver_pits) if session.capabilities.pit_stops else None),
                    median_pit_duration_ms=(
                        None if not pit_durations else float(median(pit_durations))
                    ),
                    pit_durations_ms=tuple(pit_durations),
                    lap_data_available=(
                        session.capabilities.lap_times or session.capabilities.lap_positions
                    ),
                    pit_data_available=session.capabilities.pit_stops,
                    quality_status=session.quality.status.value,
                )
            )
        return observations

    def _query(self, query: AnalyticsQuery) -> tuple[DriverRaceObservation, ...]:
        return tuple(
            item
            for item in self.snapshot().observations
            if query.start_season <= item.season <= query.end_season
            and (query.driver_id is None or item.driver_id == query.driver_id)
            and (query.constructor_id is None or item.constructor_id == query.constructor_id)
            and (query.circuit_id is None or item.circuit_id == query.circuit_id)
        )

    def _cached(
        self,
        kind: str,
        payload: object,
        builder: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        snapshot = self.snapshot()
        key = deterministic_key(kind, snapshot.source_revision, payload)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return deepcopy(cached)
        value = builder()
        with self._lock:
            self._cache[key] = deepcopy(value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_limit:
                self._cache.popitem(last=False)
        return value

    def _coverage(
        self,
        eligible: Iterable[DriverRaceObservation],
        included: Iterable[DriverRaceObservation],
        *,
        sample_count: int,
    ) -> Coverage:
        snapshot = self.snapshot()
        eligible_items = tuple(eligible)
        included_items = tuple(included)
        eligible_races = _race_ids(eligible_items)
        included_races = _race_ids(included_items)
        quality_by_race = {item.session_id: item.quality_status for item in included_items}
        return Coverage(
            sample_count=sample_count,
            race_count=len(included_races),
            eligible_race_count=len(eligible_races),
            missing_count=max(0, len(eligible_races) - len(included_races)),
            verified_count=sum(value == "verified" for value in quality_by_race.values()),
            good_count=sum(value == "good" for value in quality_by_race.values()),
            quality_exclusions=self._quality_exclusion_count(eligible_items),
            analytics_version=ANALYTICS_VERSION,
            archive_source_revision=snapshot.source_revision,
        )

    def _quality_exclusion_count(self, eligible: tuple[DriverRaceObservation, ...]) -> int:
        if eligible:
            start_season = min(item.season for item in eligible)
            end_season = max(item.season for item in eligible)
        else:
            start_season = self.snapshot().catalog.archive_start_year
            end_season = max(
                (season.year for season in self.snapshot().catalog.seasons),
                default=start_season,
            )
        return sum(
            event.race_session.row_counts.get("results", 0)
            for event in self.snapshot().catalog.events
            if start_season <= event.season <= end_season
            and event.status is ArchiveEventStatus.COMPLETED
            and event.race_session.quality.status.value not in {"verified", "good"}
        )

    @staticmethod
    def _summary(observations: Iterable[DriverRaceObservation]) -> dict[str, object]:
        items = tuple(observations)
        starts = [item for item in items if item.outcome is not OutcomeCategory.DNS]
        classified = [
            item
            for item in items
            if item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
        ]
        grids = [item.grid_position for item in starts if item.grid_position not in {None, 0}]
        finishes = [item.finish_position for item in classified if item.finish_position is not None]
        position_values = [
            item.positions_gained for item in classified if item.positions_gained is not None
        ]
        pit_values = [item.pit_stop_count for item in items if item.pit_stop_count is not None]
        return {
            "starts": len(starts),
            "finishes": len(classified),
            "wins": sum(item.finish_position == 1 for item in classified),
            "podiums": sum(
                item.finish_position is not None and item.finish_position <= 3
                for item in classified
            ),
            "points": round(sum(item.points for item in items), 2),
            "dnf": sum(item.outcome is OutcomeCategory.DNF for item in items),
            "dns": sum(item.outcome is OutcomeCategory.DNS for item in items),
            "dsq": sum(item.outcome is OutcomeCategory.DSQ for item in items),
            "average_grid": _round(_average(cast(Iterable[int], grids))),
            "average_grid_samples": len(grids),
            "average_finish": _round(_average(cast(Iterable[int], finishes))),
            "average_finish_samples": len(finishes),
            "positions_gained": sum(cast(Iterable[int], position_values)),
            "positions_gained_samples": len(position_values),
            "laps_completed": sum(item.laps_completed or 0 for item in items),
            "pit_stops": sum(cast(Iterable[int], pit_values)) if pit_values else None,
            "pit_coverage_races": len(
                _race_ids(item for item in items if item.pit_stop_count is not None)
            ),
            "best_finish": min(finishes, default=None),
            "race_count": len(_race_ids(items)),
        }

    @classmethod
    def _constructor_summary(
        cls, observations: Iterable[DriverRaceObservation]
    ) -> dict[str, object]:
        summary = cls._summary(observations)
        summary["driver_entries"] = summary["starts"]
        summary["starts"] = summary["race_count"]
        return summary

    def status(self) -> dict[str, object]:
        snapshot = self.snapshot()
        return {
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": snapshot.source_revision,
            "snapshot_digest": snapshot.digest,
            "driver_race_observations": len(snapshot.observations),
            "completed_races": len(_race_ids(snapshot.observations)),
            "provider_circuit_identities": snapshot.provider_circuit_identities,
            "built_at_utc": snapshot.built_at_utc,
        }

    @staticmethod
    def _groups(
        observations: Iterable[DriverRaceObservation],
        key: Callable[[DriverRaceObservation], str | None],
    ) -> dict[str, list[DriverRaceObservation]]:
        grouped: dict[str, list[DriverRaceObservation]] = defaultdict(list)
        for item in observations:
            value = key(item)
            if value is not None:
                grouped[value].append(item)
        return dict(grouped)

    def season(self, year: int) -> dict[str, object]:
        return self._cached("season", {"year": year}, lambda: self._season(year))

    def _season(self, year: int) -> dict[str, object]:
        observations = self._query(AnalyticsQuery(year, year))
        season = next((item for item in self.snapshot().catalog.seasons if item.year == year), None)
        if season is None:
            raise SessionNotFoundError("analytics season is not in the archive")
        driver_groups = self._groups(observations, lambda item: item.driver_id)
        constructor_groups = self._groups(observations, lambda item: item.constructor_id)
        drivers = [
            {
                "driver_id": driver_id,
                "driver_name": items[-1].driver_name,
                **self._summary(items),
            }
            for driver_id, items in driver_groups.items()
        ]
        drivers.sort(
            key=lambda item: (
                -float(cast(float, item["points"])),
                -int(cast(int, item["wins"])),
                str(item["driver_name"]),
            )
        )
        constructors = [
            {
                "constructor_id": constructor_id,
                "constructor_name": items[-1].constructor_name or constructor_id,
                "driver_count": len({item.driver_id for item in items}),
                **self._constructor_summary(items),
            }
            for constructor_id, items in constructor_groups.items()
        ]
        constructors.sort(
            key=lambda item: (
                -float(cast(float, item["points"])),
                -int(cast(int, item["wins"])),
                str(item["constructor_name"]),
            )
        )
        top_driver_ids = {str(item["driver_id"]) for item in drivers[:5]}
        cumulative: dict[str, float] = defaultdict(float)
        progression: dict[str, list[dict[str, int | float]]] = {
            driver_id: [] for driver_id in top_driver_ids
        }
        rounds = sorted({item.round_number for item in observations})
        for round_number in rounds:
            for item in observations:
                if item.round_number == round_number:
                    cumulative[item.driver_id] += item.points
            for driver_id in top_driver_ids:
                progression[driver_id].append(
                    {"round_number": round_number, "value": round(cumulative[driver_id], 2)}
                )
        top_constructor_ids = {str(item["constructor_id"]) for item in constructors[:5]}
        constructor_cumulative: dict[str, float] = defaultdict(float)
        constructor_progression: dict[str, list[dict[str, int | float]]] = {
            constructor_id: [] for constructor_id in top_constructor_ids
        }
        for round_number in rounds:
            for item in observations:
                if item.round_number == round_number and item.constructor_id is not None:
                    constructor_cumulative[item.constructor_id] += item.points
            for constructor_id in top_constructor_ids:
                constructor_progression[constructor_id].append(
                    {
                        "round_number": round_number,
                        "value": round(constructor_cumulative[constructor_id], 2),
                    }
                )
        races: list[dict[str, object]] = []
        for session_id, items in self._groups(observations, lambda item: item.session_id).items():
            ordered = sorted(items, key=lambda item: item.finish_position or 10_000)
            winner = next(
                (
                    item
                    for item in ordered
                    if item.finish_position == 1
                    and item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                ),
                None,
            )
            pit_supported = any(item.pit_data_available for item in items)
            races.append(
                {
                    "session_id": session_id,
                    "event_id": items[0].event_id,
                    "round_number": items[0].round_number,
                    "event_name": items[0].event_name,
                    "event_date": items[0].event_date,
                    "circuit_id": items[0].circuit_id,
                    "circuit_name": items[0].circuit_name,
                    "winner_driver_id": None if winner is None else winner.driver_id,
                    "winner_name": None if winner is None else winner.driver_name,
                    "pit_stop_count": (
                        sum(item.pit_stop_count or 0 for item in items) if pit_supported else None
                    ),
                    "dnf_count": sum(item.outcome is OutcomeCategory.DNF for item in items),
                }
            )
        races.sort(key=lambda item: int(cast(int, item["round_number"])))
        classified = [
            item
            for item in observations
            if item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
        ]
        constructor_points = [float(cast(float, item["points"])) for item in constructors]
        total_constructor_points = sum(constructor_points)
        concentration = (
            None
            if total_constructor_points <= 0
            else round(
                sum((value / total_constructor_points) ** 2 for value in constructor_points),
                3,
            )
        )
        lap_items = [item for item in observations if item.lap_data_available]
        pit_items = [item for item in observations if item.pit_data_available]
        return {
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
            "season": year,
            "summary": {
                "scheduled_races": len(season.events),
                "completed_races": season.completed_event_count,
                "driver_count": len(driver_groups),
                "constructor_count": len(constructor_groups),
                "result_observations": len(observations),
                "laps_completed": sum(item.laps_completed or 0 for item in observations),
                "recorded_laps": sum(item.recorded_lap_count for item in observations),
                "pit_stops": (
                    sum(item.pit_stop_count or 0 for item in pit_items) if pit_items else None
                ),
                "points_leader": drivers[0] if drivers else None,
            },
            "competitiveness": {
                "different_winners": len(
                    {item.driver_id for item in classified if item.finish_position == 1}
                ),
                "different_podium_finishers": len(
                    {
                        item.driver_id
                        for item in classified
                        if item.finish_position is not None and item.finish_position <= 3
                    }
                ),
                "driver_points_spread": (
                    None
                    if not drivers
                    else round(
                        float(cast(float, drivers[0]["points"]))
                        - float(cast(float, drivers[-1]["points"])),
                        2,
                    )
                ),
                "constructor_points_concentration": concentration,
            },
            "drivers": drivers,
            "constructors": constructors,
            "races": races,
            "driver_points_progression": [
                {
                    "entity_id": item["driver_id"],
                    "entity_name": item["driver_name"],
                    "points": progression[str(item["driver_id"])],
                }
                for item in drivers[:5]
            ],
            "constructor_points_progression": [
                {
                    "entity_id": item["constructor_id"],
                    "entity_name": item["constructor_name"],
                    "points": constructor_progression[str(item["constructor_id"])],
                }
                for item in constructors[:5]
            ],
            "coverage": {
                "results": self._coverage(
                    observations, observations, sample_count=len(observations)
                ).to_dict(),
                "laps": self._coverage(
                    observations,
                    lap_items,
                    sample_count=sum(item.recorded_lap_count for item in lap_items),
                ).to_dict(),
                "pits": self._coverage(
                    observations,
                    pit_items,
                    sample_count=sum(item.pit_stop_count or 0 for item in pit_items),
                ).to_dict(),
            },
        }

    def drivers(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {**query.to_dict(), "search": search, "offset": offset, "limit": limit}
        return self._cached("drivers", payload, lambda: self._drivers(query, search, offset, limit))

    def _drivers(
        self, query: AnalyticsQuery, search: str | None, offset: int, limit: int
    ) -> dict[str, object]:
        eligible = self._query(query)
        groups = self._groups(eligible, lambda item: item.driver_id)
        term = search.casefold().strip() if search else None
        rows: list[dict[str, object]] = []
        included: list[DriverRaceObservation] = []
        for driver_id, items in groups.items():
            name = items[-1].driver_name
            if term and term not in name.casefold() and term not in driver_id.casefold():
                continue
            included.extend(items)
            rows.append(
                {
                    "entity_id": driver_id,
                    "entity_name": name,
                    "start_season": min(item.season for item in items),
                    "end_season": max(item.season for item in items),
                    "constructors": sorted(
                        {item.constructor_name for item in items if item.constructor_name}
                    ),
                    **self._summary(items),
                }
            )
        rows.sort(key=lambda item: str(item["entity_name"]))
        return {
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "coverage": self._coverage(eligible, included, sample_count=len(included)).to_dict(),
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def driver(
        self,
        driver_id: str,
        query: AnalyticsQuery,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {**query.to_dict(), "driver_id": driver_id, "offset": offset, "limit": limit}
        return self._cached(
            "driver", payload, lambda: self._driver(driver_id, query, offset, limit)
        )

    def _driver(
        self, driver_id: str, query: AnalyticsQuery, offset: int, limit: int
    ) -> dict[str, object]:
        observations = tuple(item for item in self._query(query) if item.driver_id == driver_id)
        if not observations:
            raise SessionNotFoundError("analytics driver is not in the selected archive range")
        season_rows: list[dict[str, object]] = []
        for season, items in sorted(
            self._groups(observations, lambda item: str(item.season)).items()
        ):
            season_rows.append(
                {
                    "season": int(season),
                    "constructors": sorted(
                        {item.constructor_name for item in items if item.constructor_name}
                    ),
                    **self._summary(items),
                }
            )
        circuit_rows: list[dict[str, object]] = []
        for circuit_id, items in self._groups(observations, lambda item: item.circuit_id).items():
            circuit_rows.append(
                {
                    "circuit_id": circuit_id,
                    "circuit_name": items[-1].circuit_name,
                    **self._summary(items),
                }
            )
        circuit_rows.sort(
            key=lambda item: (-int(cast(int, item["starts"])), str(item["circuit_name"]))
        )
        races = sorted(
            (item.to_dict() for item in observations),
            key=lambda item: (int(cast(int, item["season"])), int(cast(int, item["round_number"]))),
            reverse=True,
        )
        lap_items = [item for item in observations if item.lap_data_available]
        pit_items = [item for item in observations if item.pit_data_available]
        return {
            "entity": {
                "driver_id": driver_id,
                "driver_name": observations[-1].driver_name,
                "active_start": min(item.season for item in observations),
                "active_end": max(item.season for item in observations),
                "constructors": sorted(
                    {item.constructor_name for item in observations if item.constructor_name}
                ),
            },
            "summary": self._summary(observations),
            "seasons": season_rows,
            "circuits": circuit_rows,
            "races": {
                "items": races[offset : offset + limit],
                "offset": offset,
                "limit": limit,
                "total": len(races),
            },
            "finish_distribution": {
                str(position): sum(item.finish_position == position for item in observations)
                for position in range(1, 21)
                if any(item.finish_position == position for item in observations)
            },
            "coverage": {
                "results": self._coverage(
                    observations, observations, sample_count=len(observations)
                ).to_dict(),
                "laps": self._coverage(
                    observations,
                    lap_items,
                    sample_count=sum(item.recorded_lap_count for item in lap_items),
                ).to_dict(),
                "pits": self._coverage(
                    observations,
                    pit_items,
                    sample_count=sum(item.pit_stop_count or 0 for item in pit_items),
                ).to_dict(),
            },
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def constructors(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {**query.to_dict(), "search": search, "offset": offset, "limit": limit}
        return self._cached(
            "constructors",
            payload,
            lambda: self._constructors(query, search, offset, limit),
        )

    def _constructors(
        self, query: AnalyticsQuery, search: str | None, offset: int, limit: int
    ) -> dict[str, object]:
        eligible = self._query(query)
        groups = self._groups(eligible, lambda item: item.constructor_id)
        term = search.casefold().strip() if search else None
        rows: list[dict[str, object]] = []
        included: list[DriverRaceObservation] = []
        for constructor_id, items in groups.items():
            name = items[-1].constructor_name or constructor_id
            if term and term not in name.casefold() and term not in constructor_id.casefold():
                continue
            included.extend(items)
            rows.append(
                {
                    "entity_id": constructor_id,
                    "entity_name": name,
                    "start_season": min(item.season for item in items),
                    "end_season": max(item.season for item in items),
                    "driver_count": len({item.driver_id for item in items}),
                    **self._constructor_summary(items),
                }
            )
        rows.sort(key=lambda item: str(item["entity_name"]))
        return {
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "coverage": self._coverage(eligible, included, sample_count=len(included)).to_dict(),
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def constructor(
        self,
        constructor_id: str,
        query: AnalyticsQuery,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {
            **query.to_dict(),
            "constructor_id": constructor_id,
            "offset": offset,
            "limit": limit,
        }
        return self._cached(
            "constructor",
            payload,
            lambda: self._constructor(constructor_id, query, offset, limit),
        )

    def _constructor(
        self, constructor_id: str, query: AnalyticsQuery, offset: int, limit: int
    ) -> dict[str, object]:
        observations = tuple(
            item for item in self._query(query) if item.constructor_id == constructor_id
        )
        if not observations:
            raise SessionNotFoundError("analytics constructor is not in the selected archive range")
        seasons = [
            {"season": int(season), **self._constructor_summary(items)}
            for season, items in sorted(
                self._groups(observations, lambda item: str(item.season)).items()
            )
        ]
        drivers = [
            {
                "driver_id": driver_id,
                "driver_name": items[-1].driver_name,
                **self._summary(items),
            }
            for driver_id, items in self._groups(observations, lambda item: item.driver_id).items()
        ]
        drivers.sort(key=lambda item: -int(cast(int, item["starts"])))
        circuits = [
            {
                "circuit_id": circuit_id,
                "circuit_name": items[-1].circuit_name,
                **self._summary(items),
            }
            for circuit_id, items in self._groups(
                observations, lambda item: item.circuit_id
            ).items()
        ]
        circuits.sort(key=lambda item: -int(cast(int, item["starts"])))
        race_groups = self._groups(observations, lambda item: item.session_id)
        races = []
        for items in race_groups.values():
            summary = self._summary(items)
            races.append(
                {
                    "session_id": items[0].session_id,
                    "event_id": items[0].event_id,
                    "season": items[0].season,
                    "round_number": items[0].round_number,
                    "event_name": items[0].event_name,
                    "circuit_id": items[0].circuit_id,
                    "circuit_name": items[0].circuit_name,
                    **summary,
                }
            )
        races.sort(
            key=lambda item: (int(cast(int, item["season"])), int(cast(int, item["round_number"]))),
            reverse=True,
        )
        pit_items = [item for item in observations if item.pit_data_available]
        return {
            "entity": {
                "constructor_id": constructor_id,
                "constructor_name": observations[-1].constructor_name or constructor_id,
                "active_start": min(item.season for item in observations),
                "active_end": max(item.season for item in observations),
            },
            "summary": {
                **self._constructor_summary(observations),
                "one_two_finishes": sum(
                    1
                    for items in race_groups.values()
                    if {
                        item.finish_position
                        for item in items
                        if item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                    }
                    >= {1, 2}
                ),
            },
            "seasons": seasons,
            "drivers": drivers,
            "circuits": circuits,
            "races": {
                "items": races[offset : offset + limit],
                "offset": offset,
                "limit": limit,
                "total": len(races),
            },
            "coverage": {
                "results": self._coverage(
                    observations, observations, sample_count=len(observations)
                ).to_dict(),
                "pits": self._coverage(
                    observations,
                    pit_items,
                    sample_count=sum(item.pit_stop_count or 0 for item in pit_items),
                ).to_dict(),
            },
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def circuits(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {**query.to_dict(), "search": search, "offset": offset, "limit": limit}
        return self._cached(
            "circuits", payload, lambda: self._circuits(query, search, offset, limit)
        )

    def _circuits(
        self, query: AnalyticsQuery, search: str | None, offset: int, limit: int
    ) -> dict[str, object]:
        eligible = self._query(query)
        groups = self._groups(eligible, lambda item: item.circuit_id)
        term = search.casefold().strip() if search else None
        rows: list[dict[str, object]] = []
        included: list[DriverRaceObservation] = []
        for circuit_id, items in groups.items():
            name = items[-1].circuit_name
            if term and term not in name.casefold() and term not in circuit_id.casefold():
                continue
            included.extend(items)
            rows.append(
                {
                    "entity_id": circuit_id,
                    "entity_name": name,
                    "start_season": min(item.season for item in items),
                    "end_season": max(item.season for item in items),
                    "race_count": len(_race_ids(items)),
                    "different_winners": len(
                        {
                            item.driver_id
                            for item in items
                            if item.finish_position == 1
                            and item.outcome
                            in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                        }
                    ),
                    "pit_coverage_races": len(
                        _race_ids(item for item in items if item.pit_data_available)
                    ),
                }
            )
        rows.sort(key=lambda item: str(item["entity_name"]))
        return {
            "items": rows[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "coverage": self._coverage(eligible, included, sample_count=len(included)).to_dict(),
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def circuit(
        self,
        circuit_id: str,
        query: AnalyticsQuery,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {**query.to_dict(), "circuit_id": circuit_id, "offset": offset, "limit": limit}
        return self._cached(
            "circuit", payload, lambda: self._circuit(circuit_id, query, offset, limit)
        )

    def _circuit(
        self, circuit_id: str, query: AnalyticsQuery, offset: int, limit: int
    ) -> dict[str, object]:
        observations = tuple(item for item in self._query(query) if item.circuit_id == circuit_id)
        if not observations:
            raise SessionNotFoundError("analytics circuit is not in the selected archive range")
        drivers = [
            {
                "driver_id": driver_id,
                "driver_name": items[-1].driver_name,
                **self._summary(items),
            }
            for driver_id, items in self._groups(observations, lambda item: item.driver_id).items()
        ]
        drivers.sort(
            key=lambda item: (
                -int(cast(int, item["wins"])),
                -int(cast(int, item["podiums"])),
                -float(cast(float, item["points"])),
                -int(cast(int, item["starts"])),
            )
        )
        constructors = [
            {
                "constructor_id": constructor_id,
                "constructor_name": items[-1].constructor_name or constructor_id,
                **self._constructor_summary(items),
            }
            for constructor_id, items in self._groups(
                observations, lambda item: item.constructor_id
            ).items()
        ]
        constructors.sort(
            key=lambda item: (-int(cast(int, item["wins"])), -float(cast(float, item["points"])))
        )
        races: list[dict[str, object]] = []
        for items in self._groups(observations, lambda item: item.session_id).values():
            ordered = sorted(items, key=lambda item: item.finish_position or 10_000)
            winner = next(
                (
                    item
                    for item in ordered
                    if item.finish_position == 1
                    and item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                ),
                None,
            )
            races.append(
                {
                    "session_id": items[0].session_id,
                    "event_id": items[0].event_id,
                    "season": items[0].season,
                    "round_number": items[0].round_number,
                    "event_name": items[0].event_name,
                    "winner_driver_id": None if winner is None else winner.driver_id,
                    "winner_name": None if winner is None else winner.driver_name,
                    "pit_stop_count": (
                        sum(item.pit_stop_count or 0 for item in items)
                        if any(item.pit_data_available for item in items)
                        else None
                    ),
                    "dnf_count": sum(item.outcome is OutcomeCategory.DNF for item in items),
                }
            )
        races.sort(key=lambda item: int(cast(int, item["season"])), reverse=True)
        pit_items = [item for item in observations if item.pit_data_available]
        pit_durations = [duration for item in pit_items for duration in item.pit_durations_ms]
        return {
            "entity": {
                "circuit_id": circuit_id,
                "circuit_name": observations[-1].circuit_name,
                "first_season": min(item.season for item in observations),
                "latest_season": max(item.season for item in observations),
            },
            "summary": {
                "race_count": len(_race_ids(observations)),
                "different_winners": len(
                    {item.driver_id for item in observations if item.finish_position == 1}
                ),
                "driver_count": len({item.driver_id for item in observations}),
                "constructor_count": len(
                    {item.constructor_id for item in observations if item.constructor_id}
                ),
                "dnf_rate": _round(
                    sum(item.outcome is OutcomeCategory.DNF for item in observations)
                    / sum(item.outcome is not OutcomeCategory.DNS for item in observations)
                    if any(item.outcome is not OutcomeCategory.DNS for item in observations)
                    else None,
                    3,
                ),
                "median_provider_pit_duration_ms": (
                    None if not pit_durations else float(median(pit_durations))
                ),
            },
            "races": {
                "items": races[offset : offset + limit],
                "offset": offset,
                "limit": limit,
                "total": len(races),
            },
            "drivers": drivers[:50],
            "constructors": constructors[:50],
            "pit_trend": [
                {
                    "season": int(season),
                    "pit_stops": sum(item.pit_stop_count or 0 for item in items),
                    "race_count": len(_race_ids(items)),
                }
                for season, items in sorted(
                    self._groups(pit_items, lambda item: str(item.season)).items()
                )
            ],
            "coverage": {
                "results": self._coverage(
                    observations, observations, sample_count=len(observations)
                ).to_dict(),
                "pits": self._coverage(
                    observations,
                    pit_items,
                    sample_count=len(pit_durations),
                ).to_dict(),
            },
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def race(self, session_id: str, *, driver_ids: tuple[str, ...] = ()) -> dict[str, object]:
        payload = {"session_id": session_id, "driver_ids": list(driver_ids)}
        return self._cached("race", payload, lambda: self._race(session_id, driver_ids))

    def _race(self, session_id: str, driver_ids: tuple[str, ...]) -> dict[str, object]:
        observations = tuple(
            item for item in self.snapshot().observations if item.session_id == session_id
        )
        if not observations:
            raise SessionNotFoundError("analytics race is not a completed archive session")
        ordered = sorted(observations, key=lambda item: item.finish_position or 10_000)
        selected = set(driver_ids) or {item.driver_id for item in ordered[:5]}
        lap_table = self.store.load_table(session_id, ArchiveTableName.LAPS)
        progression: dict[str, list[dict[str, int]]] = {driver_id: [] for driver_id in selected}
        for row in cast(list[dict[str, object]], lap_table.to_pylist()):
            driver_id = str(row["driver_id"])
            if driver_id not in selected or row.get("position") is None:
                continue
            progression[driver_id].append(
                {
                    "lap": int(cast(int, row["lap_number"])),
                    "position": int(cast(int, row["position"])),
                }
            )
        pit_items = [item for item in observations if item.pit_data_available]
        winner = next(
            (
                item
                for item in ordered
                if item.finish_position == 1
                and item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
            ),
            None,
        )
        podium = [
            item.to_dict()
            for item in ordered
            if item.finish_position is not None
            and item.finish_position <= 3
            and item.outcome in {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
        ]
        movers = sorted(
            (item for item in observations if item.positions_gained is not None),
            key=lambda item: cast(int, item.positions_gained),
            reverse=True,
        )
        return {
            "event": {
                "event_id": observations[0].event_id,
                "session_id": session_id,
                "season": observations[0].season,
                "round_number": observations[0].round_number,
                "event_name": observations[0].event_name,
                "event_date": observations[0].event_date,
                "circuit_id": observations[0].circuit_id,
                "circuit_name": observations[0].circuit_name,
                "quality_status": observations[0].quality_status,
            },
            "summary": {
                "winner": None if winner is None else winner.to_dict(),
                "podium": podium,
                "driver_count": len(observations),
                "recorded_laps": sum(item.recorded_lap_count for item in observations),
                "pit_stops": (
                    sum(item.pit_stop_count or 0 for item in pit_items) if pit_items else None
                ),
                "dnf_count": sum(item.outcome is OutcomeCategory.DNF for item in observations),
                "dns_count": sum(item.outcome is OutcomeCategory.DNS for item in observations),
            },
            "drivers": [item.to_dict() for item in ordered],
            "biggest_movers": [item.to_dict() for item in movers[:8]],
            "position_progression": [
                {
                    "driver_id": item.driver_id,
                    "driver_name": item.driver_name,
                    "points": progression[item.driver_id],
                }
                for item in ordered
                if item.driver_id in selected
            ],
            "coverage": {
                "results": self._coverage(
                    observations, observations, sample_count=len(observations)
                ).to_dict(),
                "laps": self._coverage(
                    observations,
                    [item for item in observations if item.lap_data_available],
                    sample_count=cast(int, lap_table.num_rows),
                ).to_dict(),
                "pits": self._coverage(
                    observations,
                    pit_items,
                    sample_count=sum(item.pit_stop_count or 0 for item in pit_items),
                ).to_dict(),
            },
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def compare(
        self,
        entity: AnalyticsEntity,
        entity_a: str,
        entity_b: str,
        query: AnalyticsQuery,
        *,
        mode: ComparisonMode = ComparisonMode.COMMON_RACES,
    ) -> dict[str, object]:
        payload = {
            "entity": entity.value,
            "entity_a": entity_a,
            "entity_b": entity_b,
            "mode": mode.value,
            **query.to_dict(),
        }
        return self._cached(
            "compare",
            payload,
            lambda: self._compare(entity, entity_a, entity_b, query, mode),
        )

    def _compare(
        self,
        entity: AnalyticsEntity,
        entity_a: str,
        entity_b: str,
        query: AnalyticsQuery,
        mode: ComparisonMode,
    ) -> dict[str, object]:
        if entity is AnalyticsEntity.CIRCUIT:
            raise ValueError("circuit head-to-head comparison is not supported")
        observations = self._query(query)
        if entity is AnalyticsEntity.DRIVER:
            a_items = tuple(item for item in observations if item.driver_id == entity_a)
            b_items = tuple(item for item in observations if item.driver_id == entity_b)
            a_name = a_items[-1].driver_name if a_items else entity_a
            b_name = b_items[-1].driver_name if b_items else entity_b
        else:
            a_items = tuple(item for item in observations if item.constructor_id == entity_a)
            b_items = tuple(item for item in observations if item.constructor_id == entity_b)
            a_name = a_items[-1].constructor_name or entity_a if a_items else entity_a
            b_name = b_items[-1].constructor_name or entity_b if b_items else entity_b
        if not a_items or not b_items:
            raise SessionNotFoundError(
                "both analytics entities must exist in the selected archive range"
            )
        common_races = _race_ids(a_items) & _race_ids(b_items)
        selected_a = (
            tuple(item for item in a_items if item.session_id in common_races)
            if mode is ComparisonMode.COMMON_RACES
            else a_items
        )
        selected_b = (
            tuple(item for item in b_items if item.session_id in common_races)
            if mode is ComparisonMode.COMMON_RACES
            else b_items
        )
        ahead_a = 0
        ahead_b = 0
        tied = 0
        excluded = 0
        teammate_races = 0
        if entity is AnalyticsEntity.DRIVER:
            by_session_a = {item.session_id: item for item in a_items}
            by_session_b = {item.session_id: item for item in b_items}
            for session_id in common_races:
                a_item = by_session_a[session_id]
                b_item = by_session_b[session_id]
                if (
                    a_item.constructor_id is not None
                    and a_item.constructor_id == b_item.constructor_id
                ):
                    teammate_races += 1
                comparable = {OutcomeCategory.FINISHED, OutcomeCategory.CLASSIFIED}
                if (
                    a_item.outcome not in comparable
                    or b_item.outcome not in comparable
                    or a_item.finish_position is None
                    or b_item.finish_position is None
                ):
                    excluded += 1
                elif a_item.finish_position < b_item.finish_position:
                    ahead_a += 1
                elif b_item.finish_position < a_item.finish_position:
                    ahead_b += 1
        else:
            constructor_by_session_a = self._groups(a_items, lambda item: item.session_id)
            constructor_by_session_b = self._groups(b_items, lambda item: item.session_id)
            for session_id in common_races:
                a_points = sum(item.points for item in constructor_by_session_a[session_id])
                b_points = sum(item.points for item in constructor_by_session_b[session_id])
                if a_points > b_points:
                    ahead_a += 1
                elif b_points > a_points:
                    ahead_b += 1
                else:
                    tied += 1
        return {
            "entity_type": entity.value,
            "mode": mode.value,
            "filters": query.to_dict(),
            "entity_a": {
                "entity_id": entity_a,
                "entity_name": a_name,
                "summary": (
                    self._summary(selected_a)
                    if entity is AnalyticsEntity.DRIVER
                    else self._constructor_summary(selected_a)
                ),
            },
            "entity_b": {
                "entity_id": entity_b,
                "entity_name": b_name,
                "summary": (
                    self._summary(selected_b)
                    if entity is AnalyticsEntity.DRIVER
                    else self._constructor_summary(selected_b)
                ),
            },
            "common_race_count": len(common_races),
            "head_to_head": {
                "a_finished_ahead": ahead_a,
                "b_finished_ahead": ahead_b,
                "tied": tied,
                "excluded_non_comparable": excluded,
                "teammate_races": teammate_races,
                "denominator": ahead_a + ahead_b + tied,
            },
            "coverage": self._coverage(
                (*a_items, *b_items),
                (*selected_a, *selected_b),
                sample_count=len(selected_a) + len(selected_b),
            ).to_dict(),
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def rankings(
        self,
        entity: AnalyticsEntity,
        metric: RankingMetric,
        query: AnalyticsQuery,
        *,
        minimum_starts: int = 5,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        payload = {
            "entity": entity.value,
            "metric": metric.value,
            "minimum_starts": minimum_starts,
            "offset": offset,
            "limit": limit,
            **query.to_dict(),
        }
        return self._cached(
            "rankings",
            payload,
            lambda: self._rankings(entity, metric, query, minimum_starts, offset, limit),
        )

    def _rankings(
        self,
        entity: AnalyticsEntity,
        metric: RankingMetric,
        query: AnalyticsQuery,
        minimum_starts: int,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        if entity is AnalyticsEntity.CIRCUIT:
            raise ValueError("rankings currently support drivers and constructors")
        observations = self._query(query)
        groups = self._groups(
            observations,
            (lambda item: item.driver_id)
            if entity is AnalyticsEntity.DRIVER
            else (lambda item: item.constructor_id),
        )
        rows: list[dict[str, object]] = []
        for entity_id, items in groups.items():
            summary = (
                self._summary(items)
                if entity is AnalyticsEntity.DRIVER
                else self._constructor_summary(items)
            )
            starts = int(cast(int, summary["starts"]))
            if starts < minimum_starts:
                continue
            value: int | float | None
            if metric is RankingMetric.DNF_RATE:
                denominator = int(cast(int, summary.get("driver_entries", summary["starts"])))
                value = (
                    round(int(cast(int, summary["dnf"])) / denominator, 3) if denominator else None
                )
            else:
                value = cast(int | float | None, summary.get(metric.value))
            if value is None:
                continue
            if metric is RankingMetric.AVERAGE_FINISH:
                sample_count = int(cast(int, summary["average_finish_samples"]))
            elif metric is RankingMetric.POSITIONS_GAINED:
                sample_count = int(cast(int, summary["positions_gained_samples"]))
            elif metric is RankingMetric.DNF_RATE:
                sample_count = denominator
            elif metric is RankingMetric.PIT_STOPS:
                sample_count = int(cast(int, summary["pit_coverage_races"]))
            else:
                sample_count = len(items)
            rows.append(
                {
                    "entity_id": entity_id,
                    "entity_name": (
                        items[-1].driver_name
                        if entity is AnalyticsEntity.DRIVER
                        else items[-1].constructor_name or entity_id
                    ),
                    "value": value,
                    "starts": starts,
                    "race_count": summary["race_count"],
                    "sample_count": sample_count,
                }
            )
        ascending = metric in {RankingMetric.AVERAGE_FINISH, RankingMetric.DNF_RATE}
        rows.sort(
            key=lambda item: (
                float(cast(float, item["value"]))
                if ascending
                else -float(cast(float, item["value"])),
                -int(cast(int, item["starts"])),
                str(item["entity_name"]),
            )
        )
        return {
            "entity_type": entity.value,
            "metric": metric.value,
            "minimum_starts": minimum_starts,
            "items": [
                {**row, "rank": offset + index + 1}
                for index, row in enumerate(rows[offset : offset + limit])
            ],
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "coverage": self._coverage(
                observations, observations, sample_count=len(observations)
            ).to_dict(),
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
        }

    def coverage_report(self) -> dict[str, object]:
        return self._cached("coverage", {}, self._coverage_report)

    def _coverage_report(self) -> dict[str, object]:
        observations = self.snapshot().observations
        lap_items = [item for item in observations if item.lap_data_available]
        timed_items = [item for item in observations if item.timed_lap_count > 0]
        pit_items = [item for item in observations if item.pit_data_available]
        metric_rows = {
            "result_metrics": self._coverage(
                observations, observations, sample_count=len(observations)
            ).to_dict(),
            "position_progression": self._coverage(
                observations,
                lap_items,
                sample_count=sum(item.recorded_lap_count for item in lap_items),
            ).to_dict(),
            "raw_lap_time_metrics": self._coverage(
                observations,
                timed_items,
                sample_count=sum(item.timed_lap_count for item in timed_items),
            ).to_dict(),
            "pit_stop_metrics": self._coverage(
                observations,
                pit_items,
                sample_count=sum(item.pit_stop_count or 0 for item in pit_items),
            ).to_dict(),
        }
        return {
            "analytics_version": ANALYTICS_VERSION,
            "archive_source_revision": self.snapshot().source_revision,
            "metrics": metric_rows,
            "eras": [
                self._era_coverage(2000, 2010),
                self._era_coverage(2011, 2017),
                self._era_coverage(2018, 2026),
            ],
        }

    def _era_coverage(self, start: int, end: int) -> dict[str, object]:
        items = self._query(AnalyticsQuery(start, end))
        return {
            "start_season": start,
            "end_season": end,
            "race_count": len(_race_ids(items)),
            "result_observations": len(items),
            "lap_races": len(_race_ids(item for item in items if item.lap_data_available)),
            "timed_lap_races": len(_race_ids(item for item in items if item.timed_lap_count > 0)),
            "pit_races": len(_race_ids(item for item in items if item.pit_data_available)),
        }

    def rebuild_manifest(self) -> dict[str, object]:
        snapshot = self.snapshot(force=True)
        derived_manifest = self.derived_store.publish(
            snapshot.observations,
            source_revision=snapshot.source_revision,
            snapshot_digest=snapshot.digest,
            built_at_utc=snapshot.built_at_utc,
            provider_circuit_identities=snapshot.provider_circuit_identities,
        )
        report = {
            **self.status(),
            "derived_store": derived_manifest,
            "coverage": self.coverage_report(),
        }
        destination = self.store.project_root / ".downforce" / "analytics" / "coverage-report.json"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
        return report


__all__ = ["AnalyticsEngine", "AnalyticsSnapshot"]
