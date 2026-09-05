from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.config import Settings
from app.core.dependencies import get_analytics_service
from app.main import create_app
from downforce_core.analytics import AnalyticsEntity, AnalyticsQuery, ComparisonMode, RankingMetric
from fastapi.testclient import TestClient


def _coverage() -> dict[str, object]:
    return {
        "sample_count": 2,
        "race_count": 1,
        "eligible_race_count": 1,
        "missing_count": 0,
        "verified_count": 1,
        "good_count": 0,
        "quality_exclusions": 0,
        "analytics_version": "1.1.0",
        "archive_source_revision": "fixture",
        "ratio": 1.0,
    }


class StubAnalyticsService:
    def season(self, year: int) -> dict[str, object]:
        return {
            "analytics_version": "1.1.0",
            "archive_source_revision": "fixture",
            "season": year,
            "summary": {"completed_races": 1},
            "competitiveness": {"different_winners": 1},
            "drivers": [],
            "constructors": [],
            "races": [],
            "driver_points_progression": [],
            "constructor_points_progression": [],
            "coverage": {"results": _coverage()},
        }

    def drivers(
        self,
        query: AnalyticsQuery,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        del query, search
        return {
            "items": [],
            "offset": offset,
            "limit": limit,
            "total": 0,
            "coverage": _coverage(),
            "analytics_version": "1.1.0",
            "archive_source_revision": "fixture",
        }

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
        return {
            "entity_type": entity.value,
            "metric": metric.value,
            "minimum_starts": minimum_starts,
            "items": [],
            "offset": offset,
            "limit": limit,
            "total": 0,
            "coverage": _coverage(),
            "analytics_version": "1.1.0",
            "archive_source_revision": "fixture",
        }

    def compare(
        self,
        entity: AnalyticsEntity,
        entity_a: str,
        entity_b: str,
        query: AnalyticsQuery,
        mode: ComparisonMode,
    ) -> dict[str, object]:
        return {
            "entity_type": entity.value,
            "mode": mode.value,
            "filters": query.to_dict(),
            "entity_a": {"entity_id": entity_a, "summary": {}},
            "entity_b": {"entity_id": entity_b, "summary": {}},
            "common_race_count": 1,
            "head_to_head": {"denominator": 1},
            "coverage": _coverage(),
            "analytics_version": "1.1.0",
            "archive_source_revision": "fixture",
        }


@pytest.fixture
def analytics_client(tmp_path) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(environment="test", log_level="CRITICAL", project_root=tmp_path)
    )
    stub = StubAnalyticsService()
    application.dependency_overrides[get_analytics_service] = lambda: stub
    with TestClient(application) as client:
        yield client


def test_typed_season_ranking_and_compare_contracts(analytics_client: TestClient) -> None:
    season = analytics_client.get("/api/v1/analytics/seasons/2000")
    assert season.status_code == 200
    assert season.json()["coverage"]["results"]["sample_count"] == 2

    drivers = analytics_client.get("/api/v1/analytics/drivers")
    assert drivers.status_code == 200
    assert drivers.json()["coverage"]["sample_count"] == 2

    ranking = analytics_client.get(
        "/api/v1/analytics/rankings",
        params={"metric": "average_finish", "minimum_starts": 20, "limit": 10},
    )
    assert ranking.status_code == 200
    assert ranking.json()["minimum_starts"] == 20

    comparison = analytics_client.post(
        "/api/v1/analytics/compare",
        json={
            "entity_type": "driver",
            "entity_a": "driver-a",
            "entity_b": "driver-b",
            "mode": "common_races",
            "filters": {"start_season": 2000, "end_season": 2026},
        },
    )
    assert comparison.status_code == 200
    assert comparison.json()["head_to_head"]["denominator"] == 1


def test_analytics_validation_is_bounded(analytics_client: TestClient) -> None:
    assert analytics_client.get("/api/v1/analytics/seasons/1999").status_code == 422
    assert (
        analytics_client.get("/api/v1/analytics/rankings", params={"limit": 101}).status_code == 422
    )
    assert (
        analytics_client.post(
            "/api/v1/analytics/compare",
            json={"entity_type": "circuit", "entity_a": "a", "entity_b": "b"},
        ).status_code
        == 422
    )
    assert (
        analytics_client.get(
            "/api/v1/analytics/rankings",
            params={"start_season": 2026, "end_season": 2025},
        ).status_code
        == 422
    )
    assert (
        analytics_client.get(
            "/api/v1/analytics/drivers",
            params={"circuit_id": "../../escape"},
        ).status_code
        == 422
    )
    assert (
        analytics_client.post(
            "/api/v1/analytics/compare",
            json={
                "entity_type": "driver",
                "entity_a": "../escape",
                "entity_b": "driver-b",
            },
        ).status_code
        == 422
    )
    assert (
        analytics_client.post(
            "/api/v1/analytics/compare",
            json={
                "entity_type": "driver",
                "entity_a": "driver-a",
                "entity_b": "driver-a",
            },
        ).status_code
        == 422
    )
