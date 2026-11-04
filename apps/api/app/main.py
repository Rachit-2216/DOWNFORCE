import logging

from downforce_core.analytics import AnalyticsEngine
from downforce_core.archive import HistoricalArchiveStore
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import (
    DownforceError,
    ReplayCursorError,
    SchemaVersionError,
    SessionDataIncompleteError,
    SessionNotFoundError,
    StorageIntegrityError,
)
from downforce_core.runtime_blob.transport import ObjectReader
from downforce_core.storage import DownforceRepository
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.analytics import router as analytics_router
from app.api.catalog import router as catalog_router
from app.api.health import router as health_router
from app.api.ml import router as ml_router
from app.api.sessions import router as sessions_router
from app.api.strategy import router as strategy_router
from app.api.telemetry import router as telemetry_router
from app.api.telemetry import telemetry_exception_handler
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.schemas.errors import ErrorDetail, ErrorResponse
from app.services.analytics_service import AnalyticsService
from app.services.catalog_service import CatalogService
from app.services.intelligence_service import IntelligenceService
from app.services.replay_service import ReplayService
from app.services.session_service import SessionService
from app.services.strategy_service import StrategyService
from app.services.telemetry_context_service import TelemetryContextService
from app.services.telemetry_service import TelemetryDataUnavailable, TelemetryService

logger = logging.getLogger(__name__)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


async def http_exception_handler(_request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, StarletteHTTPException):
        return _error_response(500, "internal_error", "An unexpected error occurred")
    detail = exception.detail if isinstance(exception.detail, str) else "Request failed"
    return _error_response(exception.status_code, "http_error", detail)


async def unhandled_exception_handler(_request: Request, exception: Exception) -> JSONResponse:
    logger.exception("Unhandled API exception", exc_info=exception)
    return _error_response(500, "internal_error", "An unexpected error occurred")


async def validation_exception_handler(_request: Request, _exception: Exception) -> JSONResponse:
    return _error_response(422, "validation_error", "Request validation failed")


async def downforce_exception_handler(_request: Request, exception: Exception) -> JSONResponse:
    if isinstance(exception, SessionNotFoundError):
        return _error_response(404, "session_not_found", str(exception))
    if isinstance(exception, ReplayCursorError):
        return _error_response(422, "invalid_replay_cursor", str(exception))
    if isinstance(exception, SessionDataIncompleteError):
        return _error_response(409, "session_data_incomplete", str(exception))
    if isinstance(exception, (SchemaVersionError, StorageIntegrityError)):
        return _error_response(
            409,
            "canonical_data_incompatible",
            "Canonical session data is unavailable or incompatible",
        )
    if isinstance(exception, DownforceError):
        return _error_response(400, "downforce_error", str(exception))
    return _error_response(500, "internal_error", "An unexpected error occurred")


def create_app(
    settings: Settings | None = None,
    repository: DownforceRepository | None = None,
    detailed_store: DetailedStore | None = None,
    *,
    object_reader: ObjectReader | None = None,
    manifest_body: bytes | None = None,
    archive_store: HistoricalArchiveStore | None = None,
    telemetry_service: TelemetryService | None = None,
    analytics_service: AnalyticsService | None = None,
    intelligence_service: IntelligenceService | None = None,
    strategy_service: StrategyService | None = None,
    replay_service: ReplayService | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    if resolved_settings.storage_backend == "bundled" and repository is None:
        from app.core.runtime_blob import create_bundled_app

        return create_bundled_app(resolved_settings, create_app)
    if resolved_settings.storage_backend == "vercel_blob" and repository is None:
        from app.core.runtime_blob import create_blob_app

        return create_blob_app(
            resolved_settings, create_app, reader=object_reader, manifest_body=manifest_body
        )
    configure_logging(resolved_settings.log_level)

    application = FastAPI(
        title="DOWNFORCE API",
        version=resolved_settings.app_version,
        description="Canonical historical-session and deterministic replay boundary for DOWNFORCE.",
    )
    application.state.settings = resolved_settings
    resolved_repository = repository or DownforceRepository(resolved_settings.project_root)
    application.state.repository = resolved_repository
    archive_store = archive_store or HistoricalArchiveStore(resolved_repository.layout.project_root)
    application.state.telemetry_service = telemetry_service or TelemetryService(
        detailed_store or DetailedStore(resolved_repository.layout.project_root),
        TelemetryContextService(resolved_repository, archive_store),
    )
    application.state.catalog_service = CatalogService(archive_store)
    application.state.analytics_service = analytics_service or AnalyticsService(
        AnalyticsEngine(archive_store)
    )
    application.state.session_service = SessionService(resolved_repository)
    application.state.replay_service = replay_service or ReplayService(resolved_repository)
    application.state.intelligence_service = intelligence_service or IntelligenceService(
        resolved_repository, application.state.replay_service
    )
    application.state.strategy_service = strategy_service or StrategyService(
        resolved_repository, resolved_repository.layout.project_root
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type"],
    )
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(DownforceError, downforce_exception_handler)
    application.add_exception_handler(TelemetryDataUnavailable, telemetry_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.include_router(health_router)
    application.include_router(analytics_router)
    application.include_router(catalog_router)
    application.include_router(ml_router)
    application.include_router(sessions_router)
    application.include_router(strategy_router)
    application.include_router(telemetry_router)
    return application


app = create_app()
