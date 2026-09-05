"""Cached lightweight catalog queries; no Parquet scans on discovery routes."""

from __future__ import annotations

from threading import RLock

from downforce_core.archive.contracts import (
    ArchiveEventStatus,
    HistoricalCatalog,
    HistoricalEvent,
)
from downforce_core.archive.storage import HistoricalArchiveStore


class HistoricalCatalogIndex:
    def __init__(self, store: HistoricalArchiveStore) -> None:
        self.store = store
        self._lock = RLock()
        self._catalog: HistoricalCatalog | None = None
        self._catalog_mtime_ns: int | None = None
        self._events: dict[str, HistoricalEvent] = {}

    def catalog(self) -> HistoricalCatalog:
        try:
            mtime = self.store.catalog_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self._lock:
            if self._catalog is None or mtime != self._catalog_mtime_ns:
                catalog = self.store.load_catalog()
                self._catalog = catalog
                self._catalog_mtime_ns = mtime
                self._events = {item.event_id: item for item in catalog.events}
            return self._catalog

    def seasons(self) -> list[dict[str, object]]:
        catalog = self.catalog()
        return [
            {
                "year": season.year,
                "event_count": len(season.events),
                "completed_event_count": season.completed_event_count,
                "latest_event_date": max(
                    (event.event_date for event in season.events), default=None
                ),
            }
            for season in reversed(catalog.seasons)
        ]

    def event(self, event_id: str) -> HistoricalEvent | None:
        self.catalog()
        return self._events.get(event_id)

    def query(
        self,
        *,
        season: int | None = None,
        circuit: str | None = None,
        driver: str | None = None,
        team: str | None = None,
        capability: str | None = None,
        status: ArchiveEventStatus | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[HistoricalEvent], int]:
        events = self.catalog().events
        circuit_term = circuit.casefold() if circuit else None
        driver_term = driver.casefold() if driver else None
        team_term = team.casefold() if team else None
        query_term = query.casefold() if query else None

        def included(event: HistoricalEvent) -> bool:
            race = event.race_session
            if season is not None and event.season != season:
                return False
            if status is not None and event.status is not status:
                return False
            if circuit_term and circuit_term not in event.circuit_name.casefold():
                return False
            if driver_term and not any(driver_term in item.casefold() for item in event.drivers):
                return False
            if team_term and not any(team_term in item.casefold() for item in event.teams):
                return False
            if capability and not race.capabilities.to_dict().get(capability, False):
                return False
            if query_term:
                haystack = " ".join(
                    (
                        event.name,
                        event.official_name,
                        event.circuit_name,
                        event.country or "",
                        *event.drivers,
                        *event.teams,
                    )
                ).casefold()
                if query_term not in haystack:
                    return False
            return True

        selected = sorted(
            (item for item in events if included(item)),
            key=lambda item: (item.season, item.round_number),
            reverse=True,
        )
        return selected[offset : offset + limit], len(selected)


__all__ = ["HistoricalCatalogIndex"]
