"""Historical discovery, capability, provenance, and archive data routes."""

from __future__ import annotations

from typing import Annotated

from downforce_core.archive import ArchiveEventStatus, ArchiveTableName
from downforce_core.domain.identifiers import validate_safe_identifier
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import AfterValidator

from app.core.dependencies import get_catalog_service
from app.schemas.catalog import (
    ArchiveCapabilityResponse,
    ArchivePageResponse,
    CatalogEventListResponse,
    CatalogEventResponse,
    SeasonListResponse,
    StorageStatusResponse,
)
from app.services.catalog_service import CatalogService

router = APIRouter(prefix="/api/v1/catalog", tags=["historical catalog"])


def _canonical_safe_path(value: str) -> str:
    return validate_safe_identifier(value, field_name="path identifier")


SafePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=240,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
    AfterValidator(_canonical_safe_path),
]


@router.get("/seasons", response_model=SeasonListResponse)
def list_seasons(
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> SeasonListResponse:
    return SeasonListResponse.model_validate(service.seasons())


@router.get("/events", response_model=CatalogEventListResponse)
def list_events(
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    season: Annotated[int | None, Query(ge=2000, le=9999)] = None,
    circuit: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    driver: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    team: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    capability: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    status: ArchiveEventStatus | None = None,
    query: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CatalogEventListResponse:
    return CatalogEventListResponse.model_validate(
        service.events(
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
    )


@router.get("/events/{event_id}", response_model=CatalogEventResponse)
def get_event(
    event_id: SafePath,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> CatalogEventResponse:
    return CatalogEventResponse.model_validate(service.event(event_id))


@router.get("/sessions/{session_id}/capabilities", response_model=ArchiveCapabilityResponse)
def get_capabilities(
    session_id: SafePath,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> ArchiveCapabilityResponse:
    return ArchiveCapabilityResponse.model_validate(service.capabilities(session_id))


def _archive_page(
    service: CatalogService,
    session_id: str,
    table_name: ArchiveTableName,
    *,
    driver_id: str | None,
    from_lap: int | None,
    to_lap: int | None,
    offset: int,
    limit: int,
) -> ArchivePageResponse:
    if from_lap is not None and to_lap is not None and to_lap < from_lap:
        raise HTTPException(status_code=422, detail="lap upper bound precedes lower bound")
    return ArchivePageResponse.model_validate(
        service.table(
            session_id,
            table_name,
            driver_id=driver_id,
            from_lap=from_lap,
            to_lap=to_lap,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/sessions/{session_id}/results", response_model=ArchivePageResponse)
def get_results(
    session_id: SafePath,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    driver_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ArchivePageResponse:
    return _archive_page(
        service,
        session_id,
        ArchiveTableName.RESULTS,
        driver_id=driver_id,
        from_lap=None,
        to_lap=None,
        offset=offset,
        limit=limit,
    )


@router.get("/sessions/{session_id}/laps", response_model=ArchivePageResponse)
def get_laps(
    session_id: SafePath,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    driver_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    from_lap: Annotated[int | None, Query(ge=1, le=200)] = None,
    to_lap: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 250,
) -> ArchivePageResponse:
    return _archive_page(
        service,
        session_id,
        ArchiveTableName.LAPS,
        driver_id=driver_id,
        from_lap=from_lap,
        to_lap=to_lap,
        offset=offset,
        limit=limit,
    )


@router.get("/sessions/{session_id}/pit-stops", response_model=ArchivePageResponse)
def get_pit_stops(
    session_id: SafePath,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    driver_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 250,
) -> ArchivePageResponse:
    return _archive_page(
        service,
        session_id,
        ArchiveTableName.PIT_STOPS,
        driver_id=driver_id,
        from_lap=None,
        to_lap=None,
        offset=offset,
        limit=limit,
    )


@router.get("/storage", response_model=StorageStatusResponse)
def get_storage(
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> StorageStatusResponse:
    return StorageStatusResponse.model_validate(service.storage())


__all__ = ["router"]
