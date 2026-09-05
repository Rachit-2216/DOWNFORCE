import logging

from downforce_core.analytics import AnalyticsEngine
from downforce_core.archive import HistoricalArchiveStore
from downforce_core.exceptions import (
    DownforceError,
    ReplayCursorError,
    SchemaVersionError,
    SessionDataIncompleteError,
    SessionNotFoundError,
    StorageIntegrityError,
)
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
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.schemas.errors import ErrorDetail, ErrorResponse
from app.services.analytics_service import AnalyticsService
from app.services.catalog_service import CatalogService
from app.services.intelligence_service import IntelligenceService
from app.services.replay_service import ReplayService
from app.services.session_service import SessionService
from app.services.strategy_service import StrategyService

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
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    application = FastAPI(
        title="DOWNFORCE API",
        version=resolved_settings.app_version,
        description="Canonical historical-session and deterministic replay boundary for DOWNFORCE.",
    )
    application.state.settings = resolved_settings
    resolved_repository = repository or DownforceRepository(resolved_settings.project_root)
    application.state.repository = resolved_repository
    archive_store = HistoricalArchiveStore(resolved_repository.layout.project_root)
    application.state.catalog_service = CatalogService(archive_store)
    application.state.analytics_service = AnalyticsService(AnalyticsEngine(archive_store))
    application.state.session_service = SessionService(resolved_repository)
    application.state.replay_service = ReplayService(resolved_repository)
    application.state.intelligence_service = IntelligenceService(
        resolved_repository, application.state.replay_service
    )
    application.state.strategy_service = StrategyService(
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
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.include_router(health_router)
    application.include_router(analytics_router)
    application.include_router(catalog_router)
    application.include_router(ml_router)
    application.include_router(sessions_router)
    application.include_router(strategy_router)
    return application


app = create_app()
