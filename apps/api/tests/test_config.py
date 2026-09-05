import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_settings_normalize_cors_origins() -> None:
    settings = Settings(environment="test", cors_origins=["http://localhost:3000/"])

    assert settings.environment == "test"
    assert settings.cors_origins == ["http://localhost:3000"]


def test_settings_reject_empty_cors_origins() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_origins=[])
