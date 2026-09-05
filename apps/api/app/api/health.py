from fastapi import APIRouter, Request

from app.core.config import Settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(service=settings.service_name, version=settings.app_version)
