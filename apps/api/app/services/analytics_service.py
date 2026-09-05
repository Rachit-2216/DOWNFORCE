"""Application boundary for cached historical analytics."""

from __future__ import annotations

from downforce_core.analytics import (
    AnalyticsEngine,
    AnalyticsEntity,
    AnalyticsQuery,
    ComparisonMode,
    RankingMetric,
)


class AnalyticsService:
    def __init__(self, engine: AnalyticsEngine) -> None:
        self.engine = engine

    def status(self) -> dict[str, object]:
        return self.engine.status()

    def coverage(self) -> dict[str, object]:
        return self.engine.coverage_report()

    def season(self, year: int) -> dict[str, object]:
        return self.engine.season(year)

    def drivers(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        return self.engine.drivers(query, search=search, offset=offset, limit=limit)

    def driver(
        self, driver_id: str, query: AnalyticsQuery, *, offset: int, limit: int
    ) -> dict[str, object]:
        return self.engine.driver(driver_id, query, offset=offset, limit=limit)

    def constructors(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        return self.engine.constructors(query, search=search, offset=offset, limit=limit)

    def constructor(
        self, constructor_id: str, query: AnalyticsQuery, *, offset: int, limit: int
    ) -> dict[str, object]:
        return self.engine.constructor(constructor_id, query, offset=offset, limit=limit)

    def circuits(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        return self.engine.circuits(query, search=search, offset=offset, limit=limit)

    def circuit(
        self, circuit_id: str, query: AnalyticsQuery, *, offset: int, limit: int
    ) -> dict[str, object]:
        return self.engine.circuit(circuit_id, query, offset=offset, limit=limit)

    def race(self, session_id: str, *, driver_ids: tuple[str, ...]) -> dict[str, object]:
        return self.engine.race(session_id, driver_ids=driver_ids)

    def compare(
        self,
        entity: AnalyticsEntity,
        entity_a: str,
        entity_b: str,
        query: AnalyticsQuery,
        mode: ComparisonMode,
    ) -> dict[str, object]:
        return self.engine.compare(entity, entity_a, entity_b, query, mode=mode)

    def rankings(
        self,
        entity: AnalyticsEntity,
        metric: RankingMetric,
        query: AnalyticsQuery,
        *,
        minimum_starts: int,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        return self.engine.rankings(
            entity,
            metric,
            query,
            minimum_starts=minimum_starts,
            offset=offset,
            limit=limit,
        )


__all__ = ["AnalyticsService"]
