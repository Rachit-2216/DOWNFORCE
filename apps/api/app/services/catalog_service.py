"""Low-latency catalog and archive table queries."""

from __future__ import annotations

from typing import cast

import pyarrow.compute as pc  # type: ignore[import-untyped]
from downforce_core.archive import (
    ArchiveEventStatus,
    ArchiveTableName,
    HistoricalArchiveStore,
    HistoricalCatalogIndex,
)
from downforce_core.archive.contracts import HistoricalEvent
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import SessionDataIncompleteError, SessionNotFoundError


class CatalogService:
    def __init__(self, store: HistoricalArchiveStore) -> None:
        self.store = store
        self.index = HistoricalCatalogIndex(store)

    def seasons(self) -> dict[str, object]:
        items = self.index.seasons()
        catalog = self.index.catalog()
        return {
            "items": items,
            "total": len(items),
            "event_count": len(catalog.events),
            "completed_event_count": sum(
                item.status is ArchiveEventStatus.COMPLETED for item in catalog.events
            ),
            "archive_start_year": catalog.archive_start_year,
            "latest_completed_event_id": catalog.latest_completed_event_id,
            "latest_completed_event_date": catalog.latest_completed_event_date,
        }

    def events(
        self,
        *,
        season: int | None,
        circuit: str | None,
        driver: str | None,
        team: str | None,
        capability: str | None,
        status: ArchiveEventStatus | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        items, total = self.index.query(
            season=season,
            circuit=circuit,
            driver=driver,
            team=team,
            capability=capability,
            status=status,
            query=query,
            offset=offset,
            limit=limit,
        )
        return {
            "items": [item.to_dict() for item in items],
            "offset": offset,
            "limit": limit,
            "total": total,
        }

    def event(self, event_id: str) -> dict[str, object]:
        validate_safe_identifier(event_id, field_name="event_id")
        event = self.index.event(event_id)
        if event is None:
            raise SessionNotFoundError("historical event is not in the catalog")
        return event.to_dict()

    def capabilities(self, session_id: str) -> dict[str, object]:
        event = self._event_for_session(session_id)
        session = event.race_session
        return {
            "session_id": session.session_id,
            "event_id": event.event_id,
            "status": session.status.value,
            "sync_status": session.sync_status.value,
            "capability_tier": session.capabilities.tier.value,
            "capabilities": session.capabilities.to_dict(),
            "quality": session.quality.to_dict(),
            "row_counts": dict(session.row_counts),
            "legacy_session_id": session.legacy_session_id,
        }

    def table(
        self,
        session_id: str,
        table_name: ArchiveTableName,
        *,
        driver_id: str | None,
        from_lap: int | None,
        to_lap: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        event = self._event_for_session(session_id)
        session = event.race_session
        if session.data_revision is None or session.status is not ArchiveEventStatus.COMPLETED:
            raise SessionDataIncompleteError("archive session is not materialized")
        table = self.store.load_table(session_id, table_name)
        if driver_id is not None:
            table = table.filter(pc.equal(table.column("driver_id"), driver_id))
        if table_name is ArchiveTableName.LAPS:
            if from_lap is not None:
                table = table.filter(pc.greater_equal(table.column("lap_number"), from_lap))
            if to_lap is not None:
                table = table.filter(pc.less_equal(table.column("lap_number"), to_lap))
        total = cast(int, table.num_rows)
        return {
            "items": cast(list[dict[str, object]], table.slice(offset, limit).to_pylist()),
            "offset": offset,
            "limit": limit,
            "total": total,
        }

    def storage(self) -> dict[str, object]:
        report = self.store.storage_report()
        report.pop("root", None)
        return {
            **report,
            "sync": self.store.load_sync_state(),
        }

    def _event_for_session(self, session_id: str) -> HistoricalEvent:
        validate_safe_identifier(session_id, field_name="archive_session_id")
        for event in self.index.catalog().events:
            if event.race_session.session_id == session_id:
                return event
        raise SessionNotFoundError("archive session is not in the catalog")


__all__ = ["CatalogService"]
