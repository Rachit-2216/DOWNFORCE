"""Read-only ML artifact status route."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import get_intelligence_service
from app.schemas.intelligence import MLStatusResponse
from app.services.intelligence_service import IntelligenceService

router = APIRouter(prefix="/api/v1/ml", tags=["historical intelligence"])


@router.get("/status", response_model=MLStatusResponse)
def ml_status(
    service: Annotated[IntelligenceService, Depends(get_intelligence_service)],
) -> MLStatusResponse:
    return MLStatusResponse.model_validate(service.status())


__all__ = ["router"]
