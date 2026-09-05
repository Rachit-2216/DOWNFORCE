"""Application-scoped repository and service dependencies."""

from fastapi import Request

from app.services.analytics_service import AnalyticsService
from app.services.catalog_service import CatalogService
from app.services.intelligence_service import IntelligenceService
from app.services.replay_service import ReplayService
from app.services.session_service import SessionService
from app.services.strategy_service import StrategyService


def get_session_service(request: Request) -> SessionService:
    service = request.app.state.session_service
    if not isinstance(service, SessionService):
        raise RuntimeError("session service is not configured")
    return service


def get_analytics_service(request: Request) -> AnalyticsService:
    service = request.app.state.analytics_service
    if not isinstance(service, AnalyticsService):
        raise RuntimeError("analytics service is not configured")
    return service


def get_catalog_service(request: Request) -> CatalogService:
    service = request.app.state.catalog_service
    if not isinstance(service, CatalogService):
        raise RuntimeError("catalog service is not configured")
    return service


def get_replay_service(request: Request) -> ReplayService:
    service = request.app.state.replay_service
    if not isinstance(service, ReplayService):
        raise RuntimeError("replay service is not configured")
    return service


def get_intelligence_service(request: Request) -> IntelligenceService:
    service = request.app.state.intelligence_service
    if not isinstance(service, IntelligenceService):
        raise RuntimeError("intelligence service is not configured")
    return service


def get_strategy_service(request: Request) -> StrategyService:
    service = request.app.state.strategy_service
    if not isinstance(service, StrategyService):
        raise RuntimeError("strategy service is not configured")
    return service


__all__ = [
    "get_analytics_service",
    "get_catalog_service",
    "get_intelligence_service",
    "get_replay_service",
    "get_session_service",
    "get_strategy_service",
]
