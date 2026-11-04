"""Read-only production-canonical telemetry routes with typed bounded products."""

from collections.abc import Callable
from typing import Annotated, Literal

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.telemetry.types import TraceRejected
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.schemas.errors import ErrorDetail, ErrorResponse
from app.schemas.telemetry import (
    CompareSettings,
    ComparisonResponse,
    CornersResponse,
    MicrosectorsResponse,
    TelemetryLapPage,
    TelemetrySessionPage,
    TraceResponse,
    TraceSettings,
)
from app.services.telemetry_service import TelemetryDataUnavailable, TelemetryService

router = APIRouter(
    prefix="/api/v1/telemetry",
    tags=["telemetry"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 413, 422)},
)
SessionPath = Annotated[
    str, Path(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
]
DriverQuery = Annotated[str, Query(min_length=1, max_length=3, pattern=r"^[1-9][0-9]{0,2}$")]
LapQuery = Annotated[int, Query(ge=1, le=1000)]
RevisionQuery = Annotated[
    str | None, Query(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]


def get_telemetry_service(request: Request) -> TelemetryService:
    service = request.app.state.telemetry_service
    if not isinstance(service, TelemetryService):
        raise RuntimeError("telemetry service is not configured")
    return service


def trace_settings(
    channels: Annotated[str, Query(min_length=1, max_length=200)] = TraceSettings().channels,
    max_points: Annotated[int, Query(ge=2, le=20_000)] = 20_000,
    allow_partial: bool = False,
    allow_pit_laps: bool = False,
    allow_deleted: bool = False,
    allow_provider_generated: bool = False,
    allow_unreliable: bool = False,
    max_edge_gap_seconds: Annotated[float, Query(gt=0, le=10, allow_inf_nan=False)] = 2.0,
) -> TraceSettings:
    return TraceSettings(
        channels=channels,
        max_points=max_points,
        allow_partial=allow_partial,
        allow_pit_laps=allow_pit_laps,
        allow_deleted=allow_deleted,
        allow_provider_generated=allow_provider_generated,
        allow_unreliable=allow_unreliable,
        max_edge_gap_seconds=max_edge_gap_seconds,
    )


def compare_settings(
    trace: Annotated[TraceSettings, Depends(trace_settings)],
    spacing_m: Annotated[float, Query(gt=0, le=1000, allow_inf_nan=False)] = 1.0,
    grid: Literal["fixed_spacing", "reference_union"] = "fixed_spacing",
    max_gap_seconds: Annotated[float, Query(gt=0, le=10, allow_inf_nan=False)] = 2.0,
    max_gap_m: Annotated[float, Query(gt=0, le=1000, allow_inf_nan=False)] = 100.0,
) -> CompareSettings:
    return CompareSettings(
        **trace.model_dump(),
        spacing_m=spacing_m,
        grid=grid,
        max_gap_seconds=max_gap_seconds,
        max_gap_m=max_gap_m,
    )


async def telemetry_exception_handler(_request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, TelemetryDataUnavailable):
        raise TypeError("invalid telemetry exception handler input")
    payload = ErrorResponse(error=ErrorDetail(code=exception.code, message=exception.message))
    return JSONResponse(status_code=409, content=payload.model_dump())


def _read[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except HTTPException:
        raise
    except StorageIntegrityError as exc:
        raise HTTPException(
            409, "Canonical telemetry revision is unavailable or incompatible"
        ) from exc
    except TraceRejected as exc:
        if str(exc) == "grid_point_limit":
            raise HTTPException(413, "Comparison exceeds point limit; increase spacing") from exc
        raise HTTPException(409, "Selected lap cannot support this derived operation") from exc
    except ValidationError as exc:
        raise HTTPException(409, "Canonical telemetry metadata is incompatible") from exc
    except (ValueError, KeyError) as exc:
        if str(exc) == "requested lap is missing":
            raise HTTPException(404, "Selected lap is missing") from exc
        if str(exc) == "requested lap is ambiguous":
            raise HTTPException(409, "Selected lap is ambiguous") from exc
        raise HTTPException(
            409, "Requested telemetry observations are unavailable or unsupported"
        ) from exc


Service = Annotated[TelemetryService, Depends(get_telemetry_service)]


@router.get("/sessions", response_model=TelemetrySessionPage)
def sessions(
    service: Service,
    season: Annotated[int | None, Query(ge=2018, le=2100)] = None,
    session_type: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
    event: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    circuit: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    driver: Annotated[str | None, Query(max_length=3, pattern=r"^[1-9][0-9]{0,2}$")] = None,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> TelemetrySessionPage:
    return _read(
        lambda: service.sessions(
            season=season,
            session_type=session_type,
            event=event,
            circuit=circuit,
            driver=driver,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/sessions/{session_id}/laps", response_model=TelemetryLapPage)
def laps(
    session_id: SessionPath,
    service: Service,
    driver: DriverQuery,
    revision: RevisionQuery = None,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> TelemetryLapPage:
    return _read(
        lambda: service.laps(session_id, driver, revision=revision, offset=offset, limit=limit)
    )


@router.get("/sessions/{session_id}/trace", response_model=TraceResponse)
def trace(
    session_id: SessionPath,
    service: Service,
    driver: DriverQuery,
    lap: LapQuery,
    settings: Annotated[TraceSettings, Depends(trace_settings)],
    revision: RevisionQuery = None,
) -> TraceResponse:
    return _read(
        lambda: service.trace(session_id, driver, lap, revision=revision, settings=settings)
    )


@router.get("/sessions/{session_id}/compare", response_model=ComparisonResponse)
def compare(
    session_id: SessionPath,
    service: Service,
    driver_a: DriverQuery,
    lap_a: LapQuery,
    driver_b: DriverQuery,
    lap_b: LapQuery,
    settings: Annotated[CompareSettings, Depends(compare_settings)],
    revision: RevisionQuery = None,
) -> ComparisonResponse:
    return _read(
        lambda: service.compare(
            session_id, driver_a, lap_a, driver_b, lap_b, revision=revision, settings=settings
        )
    )


@router.get("/sessions/{session_id}/corners", response_model=CornersResponse)
def corners(
    session_id: SessionPath,
    service: Service,
    driver_a: DriverQuery,
    lap_a: LapQuery,
    driver_b: DriverQuery,
    lap_b: LapQuery,
    settings: Annotated[CompareSettings, Depends(compare_settings)],
    revision: RevisionQuery = None,
    half_width_m: Annotated[float, Query(gt=0, le=2000, allow_inf_nan=False)] = 100.0,
    fallback_count: Annotated[int, Query(ge=1, le=1000)] = 10,
) -> CornersResponse:
    return _read(
        lambda: service.corners(
            session_id,
            driver_a,
            lap_a,
            driver_b,
            lap_b,
            revision=revision,
            settings=settings,
            half_width_m=half_width_m,
            fallback_count=fallback_count,
        )
    )


@router.get("/sessions/{session_id}/microsectors", response_model=MicrosectorsResponse)
def microsector_summary(
    session_id: SessionPath,
    service: Service,
    driver_a: DriverQuery,
    lap_a: LapQuery,
    driver_b: DriverQuery,
    lap_b: LapQuery,
    settings: Annotated[CompareSettings, Depends(compare_settings)],
    revision: RevisionQuery = None,
    count: Annotated[int, Query(ge=5, le=1000)] = 20,
) -> MicrosectorsResponse:
    return _read(
        lambda: service.microsectors(
            session_id,
            driver_a,
            lap_a,
            driver_b,
            lap_b,
            revision=revision,
            settings=settings,
            count=count,
        )
    )
