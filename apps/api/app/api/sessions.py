"""Versioned canonical historical session and replay routes."""

from __future__ import annotations

from typing import Annotated, Literal

from downforce_core.domain.events import RaceEventType
from downforce_core.domain.identifiers import DriverId, SessionId
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import AfterValidator

from app.core.dependencies import (
    get_intelligence_service,
    get_replay_service,
    get_session_service,
)
from app.schemas.intelligence import IntelligenceResponse
from app.schemas.sessions import (
    DriverListResponse,
    LapListResponse,
    RaceStateResponse,
    SessionListResponse,
    SessionResponse,
    TelemetryIndexListResponse,
    TimelineResponse,
    TrackPositionListResponse,
)
from app.services.intelligence_service import IntelligenceService
from app.services.replay_service import ReplayService
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/v1/sessions", tags=["historical sessions"])

MAX_INT64 = 9_223_372_036_854_775_807


def _canonical_session_id(value: str) -> str:
    SessionId(value)
    return value


def _canonical_driver_id(value: str) -> str:
    DriverId(value)
    return value


SessionIdPath = Annotated[
    str,
    Path(min_length=1, max_length=240),
    AfterValidator(_canonical_session_id),
]
DriverFilter = Annotated[
    str | None,
    Query(min_length=1, max_length=240),
    AfterValidator(lambda value: None if value is None else _canonical_driver_id(value)),
]
DriverIdPath = Annotated[
    str,
    Path(min_length=1, max_length=240),
    AfterValidator(_canonical_driver_id),
]


def _validate_range(start: int | None, end: int | None, label: str) -> None:
    if start is not None and end is not None and end < start:
        raise HTTPException(status_code=422, detail=f"{label} upper bound precedes lower bound")


@router.get("", response_model=SessionListResponse)
def list_sessions(
    service: Annotated[SessionService, Depends(get_session_service)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SessionListResponse:
    return SessionListResponse.model_validate(service.list_sessions(offset=offset, limit=limit))


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: SessionIdPath,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionResponse:
    return SessionResponse.model_validate(service.session(session_id))


@router.get("/{session_id}/state", response_model=RaceStateResponse)
def get_state(
    session_id: SessionIdPath,
    service: Annotated[ReplayService, Depends(get_replay_service)],
    time_ms: Annotated[int | None, Query(ge=0)] = None,
    lap: Annotated[int | None, Query(ge=1, le=200)] = None,
    phase: Literal["start", "end"] = "end",
) -> RaceStateResponse:
    if (time_ms is None) == (lap is None):
        raise HTTPException(status_code=422, detail="supply exactly one of time_ms or lap")
    return RaceStateResponse.model_validate(
        service.state(session_id, time_ms=time_ms, lap=lap, phase=phase)
    )


@router.get("/{session_id}/timeline", response_model=TimelineResponse)
def get_timeline(
    session_id: SessionIdPath,
    service: Annotated[ReplayService, Depends(get_replay_service)],
    from_ms: Annotated[int | None, Query(ge=0, le=MAX_INT64)] = None,
    to_ms: Annotated[int | None, Query(ge=0, le=MAX_INT64)] = None,
    types: Annotated[list[RaceEventType] | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 250,
) -> TimelineResponse:
    _validate_range(from_ms, to_ms, "timeline")
    return TimelineResponse.model_validate(
        service.timeline(
            session_id,
            from_ms=from_ms,
            to_ms=to_ms,
            types=None if types is None else frozenset(types),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/{session_id}/drivers", response_model=DriverListResponse)
def get_drivers(
    session_id: SessionIdPath,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> DriverListResponse:
    return DriverListResponse.model_validate(service.drivers(session_id))


@router.get("/{session_id}/laps", response_model=LapListResponse)
def get_laps(
    session_id: SessionIdPath,
    service: Annotated[SessionService, Depends(get_session_service)],
    driver_id: DriverFilter = None,
    from_lap: Annotated[int | None, Query(ge=1, le=200)] = None,
    to_lap: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 250,
) -> LapListResponse:
    _validate_range(from_lap, to_lap, "lap")
    return LapListResponse.model_validate(
        service.laps(
            session_id,
            driver_id=driver_id,
            from_lap=from_lap,
            to_lap=to_lap,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/{session_id}/track-positions", response_model=TrackPositionListResponse)
def get_track_positions(
    session_id: SessionIdPath,
    service: Annotated[SessionService, Depends(get_session_service)],
    driver_id: DriverFilter = None,
    from_ms: Annotated[int | None, Query(ge=0, le=MAX_INT64)] = None,
    to_ms: Annotated[int | None, Query(ge=0, le=MAX_INT64)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5_000)] = 1_000,
) -> TrackPositionListResponse:
    _validate_range(from_ms, to_ms, "track-position")
    return TrackPositionListResponse.model_validate(
        service.track_positions(
            session_id,
            driver_id=driver_id,
            from_ms=from_ms,
            to_ms=to_ms,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/{session_id}/telemetry-index", response_model=TelemetryIndexListResponse)
def get_telemetry_index(
    session_id: SessionIdPath,
    service: Annotated[SessionService, Depends(get_session_service)],
    driver_id: DriverFilter = None,
) -> TelemetryIndexListResponse:
    return TelemetryIndexListResponse.model_validate(
        service.telemetry_index(session_id, driver_id=driver_id)
    )


@router.get(
    "/{session_id}/drivers/{driver_id}/intelligence",
    response_model=IntelligenceResponse,
)
def get_intelligence(
    session_id: SessionIdPath,
    driver_id: DriverIdPath,
    service: Annotated[IntelligenceService, Depends(get_intelligence_service)],
    time_ms: Annotated[int, Query(ge=0)],
) -> IntelligenceResponse:
    result = service.intelligence(session_id, driver_id, time_ms)
    if result.get("reason") == "unknown_driver":
        raise HTTPException(status_code=404, detail="canonical driver is not available")
    return IntelligenceResponse.model_validate(result)


__all__ = ["router"]
