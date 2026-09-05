"""Typed read-only-over-POST strategy simulation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import get_strategy_service
from app.schemas.strategy import (
    ComparisonRequest,
    CounterfactualRequest,
    SimulationRequest,
    StrategyResponse,
    StrategyStatusResponse,
)
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api/v1", tags=["strategy engineering"])


@router.get("/strategy/status", response_model=StrategyStatusResponse)
def strategy_status(
    service: Annotated[StrategyService, Depends(get_strategy_service)],
) -> StrategyStatusResponse:
    return StrategyStatusResponse.model_validate(service.status())


@router.post("/sessions/{session_id}/strategy/simulate", response_model=StrategyResponse)
def simulate_strategy(
    session_id: str,
    request: SimulationRequest,
    service: Annotated[StrategyService, Depends(get_strategy_service)],
) -> StrategyResponse:
    return StrategyResponse.model_validate(service.simulate(session_id, request))


@router.post("/sessions/{session_id}/strategy/compare", response_model=StrategyResponse)
def compare_strategies(
    session_id: str,
    request: ComparisonRequest,
    service: Annotated[StrategyService, Depends(get_strategy_service)],
) -> StrategyResponse:
    return StrategyResponse.model_validate(service.compare(session_id, request))


@router.post("/sessions/{session_id}/strategy/counterfactual", response_model=StrategyResponse)
def counterfactual_strategy(
    session_id: str,
    request: CounterfactualRequest,
    service: Annotated[StrategyService, Depends(get_strategy_service)],
) -> StrategyResponse:
    return StrategyResponse.model_validate(service.counterfactual(session_id, request))


__all__ = ["router"]
