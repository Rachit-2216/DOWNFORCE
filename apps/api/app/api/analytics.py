"""Typed, paginated endpoints for deterministic historical analytics."""

from __future__ import annotations

from typing import Annotated

from downforce_core.analytics import (
    AnalyticsEntity,
    AnalyticsQuery,
    ComparisonMode,
    RankingMetric,
)
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.dependencies import get_analytics_service
from app.schemas.analytics import (
    AnalyticsPageResponse,
    AnalyticsStatusResponse,
    ComparisonRequest,
    ComparisonResponse,
    CoverageReportResponse,
    EntityProfileResponse,
    RaceAnalyticsResponse,
    RankingResponse,
    SeasonAnalyticsResponse,
)
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/api/v1/analytics", tags=["historical analytics"])

EntityPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$",
    ),
]
ArchiveSessionPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=120,
        pattern=r"^archive-\d{4}-round-\d{2}-race$",
    ),
]


def _query(
    start_season: int,
    end_season: int,
    *,
    circuit_id: str | None = None,
) -> AnalyticsQuery:
    if end_season < start_season:
        raise HTTPException(status_code=422, detail="season range is inverted")
    return AnalyticsQuery(start_season, end_season, circuit_id=circuit_id)


@router.get("/status", response_model=AnalyticsStatusResponse)
def analytics_status(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> AnalyticsStatusResponse:
    return AnalyticsStatusResponse.model_validate(service.status())


@router.get("/coverage", response_model=CoverageReportResponse)
def analytics_coverage(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> CoverageReportResponse:
    return CoverageReportResponse.model_validate(service.coverage())


@router.get("/seasons/{year}", response_model=SeasonAnalyticsResponse)
def season_analytics(
    year: Annotated[int, Path(ge=2000, le=9999)],
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> SeasonAnalyticsResponse:
    return SeasonAnalyticsResponse.model_validate(service.season(year))


@router.get("/drivers", response_model=AnalyticsPageResponse)
def list_drivers(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    circuit_id: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    ] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnalyticsPageResponse:
    return AnalyticsPageResponse.model_validate(
        service.drivers(
            _query(start_season, end_season, circuit_id=circuit_id),
            search=search,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/drivers/{driver_id}", response_model=EntityProfileResponse)
def driver_analytics(
    driver_id: EntityPath,
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    circuit_id: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EntityProfileResponse:
    return EntityProfileResponse.model_validate(
        service.driver(
            driver_id,
            _query(start_season, end_season, circuit_id=circuit_id),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/constructors", response_model=AnalyticsPageResponse)
def list_constructors(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    circuit_id: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    ] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnalyticsPageResponse:
    return AnalyticsPageResponse.model_validate(
        service.constructors(
            _query(start_season, end_season, circuit_id=circuit_id),
            search=search,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/constructors/{constructor_id}", response_model=EntityProfileResponse)
def constructor_analytics(
    constructor_id: EntityPath,
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    circuit_id: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EntityProfileResponse:
    return EntityProfileResponse.model_validate(
        service.constructor(
            constructor_id,
            _query(start_season, end_season, circuit_id=circuit_id),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/circuits", response_model=AnalyticsPageResponse)
def list_circuits(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    search: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnalyticsPageResponse:
    return AnalyticsPageResponse.model_validate(
        service.circuits(
            _query(start_season, end_season),
            search=search,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/circuits/{circuit_id}", response_model=EntityProfileResponse)
def circuit_analytics(
    circuit_id: EntityPath,
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EntityProfileResponse:
    return EntityProfileResponse.model_validate(
        service.circuit(
            circuit_id,
            _query(start_season, end_season),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/races/{session_id}", response_model=RaceAnalyticsResponse)
def race_analytics(
    session_id: ArchiveSessionPath,
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    driver_ids: Annotated[list[str] | None, Query(max_length=10)] = None,
) -> RaceAnalyticsResponse:
    return RaceAnalyticsResponse.model_validate(
        service.race(session_id, driver_ids=tuple(driver_ids or ()))
    )


@router.post("/compare", response_model=ComparisonResponse)
def compare_entities(
    request: ComparisonRequest,
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
) -> ComparisonResponse:
    filters = request.filters
    return ComparisonResponse.model_validate(
        service.compare(
            AnalyticsEntity(request.entity_type),
            request.entity_a,
            request.entity_b,
            _query(
                filters.start_season,
                filters.end_season,
                circuit_id=filters.circuit_id,
            ),
            ComparisonMode(request.mode),
        )
    )


@router.get("/rankings", response_model=RankingResponse)
def analytics_rankings(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    entity_type: AnalyticsEntity = AnalyticsEntity.DRIVER,
    metric: RankingMetric = RankingMetric.WINS,
    start_season: Annotated[int, Query(ge=2000, le=9999)] = 2000,
    end_season: Annotated[int, Query(ge=2000, le=9999)] = 9999,
    circuit_id: Annotated[
        str | None, Query(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    ] = None,
    minimum_starts: Annotated[int, Query(ge=1, le=500)] = 5,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RankingResponse:
    return RankingResponse.model_validate(
        service.rankings(
            entity_type,
            metric,
            _query(start_season, end_season, circuit_id=circuit_id),
            minimum_starts=minimum_starts,
            offset=offset,
            limit=limit,
        )
    )


__all__ = ["router"]
