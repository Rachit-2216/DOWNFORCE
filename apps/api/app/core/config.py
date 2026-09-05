from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated process configuration for the DOWNFORCE API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DOWNFORCE_",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    service_name: str = "downforce-api"
    app_version: str = "0.1.0"
    project_root: Path = Path(".")

    @field_validator("cors_origins")
    @classmethod
    def normalize_cors_origins(cls, origins: list[str]) -> list[str]:
        normalized = [origin.rstrip("/") for origin in origins if origin.strip()]
        if not normalized:
            raise ValueError("at least one CORS origin is required")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
