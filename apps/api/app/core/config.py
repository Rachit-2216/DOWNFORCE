import os
import re
from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Literal

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import _safe
from downforce_core.runtime_blob.manifest import validate_relative_path
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validate_cache_root(cache_root: Path, project_root: Path, label: str) -> None:
    # Retain the original path for the cache's independent materialization checks.
    if ".." in cache_root.parts:
        raise ValueError(f"{label} cache path is unsafe")
    root = Path(os.path.abspath(cache_root))
    temporary = Path(os.path.abspath(gettempdir()))
    try:
        _safe(root)
    except StorageIntegrityError:
        raise ValueError(f"{label} cache path is unsafe") from None
    canonical_root = root.resolve()
    canonical_temporary = temporary.resolve()
    if (
        not cache_root.is_absolute()
        or canonical_root == canonical_temporary
        or not canonical_root.is_relative_to(canonical_temporary)
        or canonical_root.is_relative_to(project_root.resolve())
        or canonical_root.is_relative_to(Path(__file__).resolve().parents[4])
    ):
        raise ValueError(f"{label} cache requires a dedicated temporary directory")


class Settings(BaseSettings):
    """Validated process configuration for the DOWNFORCE API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DOWNFORCE_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    service_name: str = "downforce-api"
    app_version: str = "0.1.0"
    project_root: Path = Path(".")
    storage_backend: Literal["local", "vercel_blob", "bundled"] = Field(
        default_factory=lambda: "bundled" if os.environ.get("VERCEL") == "1" else "local"
    )
    bundle_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[4] / "product-data"
    )
    bundle_release_sha256: str | None = None
    bundle_cache_root: Path = Field(
        default_factory=lambda: Path(gettempdir()) / "downforce-runtime-bundle"
    )
    bundle_cache_max_bytes: int = Field(default=128 * 1024**2, gt=0, le=512 * 1024**2)
    bundle_cache_max_files: int = Field(default=100_002, gt=0, le=100_002)
    bundle_cache_lock_timeout: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    blob_origin: str | None = None
    blob_release_prefix: str | None = None
    blob_release_sha256: str | None = None
    blob_token: SecretStr | None = Field(default=None, repr=False)
    blob_cache_root: Path = Field(
        default_factory=lambda: Path(gettempdir()) / "downforce-runtime-blob"
    )
    blob_cache_max_bytes: int = Field(default=512 * 1024**2, gt=0, le=512 * 1024**2)
    blob_cache_max_files: int = Field(default=100_002, gt=0, le=100_002)
    blob_cache_lock_timeout: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_storage(self) -> "Settings":
        if os.environ.get("VERCEL") == "1" and self.storage_backend == "local":
            raise ValueError(
                "Vercel requires bundled or explicit vercel_blob storage configuration"
            )
        if self.storage_backend == "local":
            return self
        if self.storage_backend == "bundled":
            if self.bundle_release_sha256 is not None and not re.fullmatch(
                r"[0-9a-f]{64}", self.bundle_release_sha256
            ):
                raise ValueError("bundle release pin is invalid")
            _validate_cache_root(self.bundle_cache_root, self.project_root, "bundle")
            bundle_root = self.bundle_root.resolve()
            cache_root = self.bundle_cache_root.resolve()
            if bundle_root.is_relative_to(cache_root) or cache_root.is_relative_to(bundle_root):
                raise ValueError("bundle and mutable cache roots must be disjoint")
            return self
        if any(
            value is None
            for value in (
                self.blob_origin,
                self.blob_release_prefix,
                self.blob_release_sha256,
                self.blob_token,
            )
        ):
            raise ValueError("private Blob storage configuration is incomplete")
        if not re.fullmatch(
            r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.private\.blob\.vercel-storage\.com",
            self.blob_origin or "",
        ):
            raise ValueError("private Blob origin is invalid")
        try:
            validate_relative_path(self.blob_release_prefix or "")
        except StorageIntegrityError:
            raise ValueError("private Blob release prefix is invalid") from None
        if any(
            part.casefold() in {"latest", "current", "active"}
            for part in (self.blob_release_prefix or "").split("/")
        ):
            raise ValueError("private Blob release prefix must be immutable")
        if not re.fullmatch(r"[0-9a-f]{64}", self.blob_release_sha256 or ""):
            raise ValueError("private Blob release pin is invalid")
        token = self.blob_token.get_secret_value() if self.blob_token else ""
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("private Blob server credential is invalid")
        _validate_cache_root(self.blob_cache_root, self.project_root, "private Blob")
        return self

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
