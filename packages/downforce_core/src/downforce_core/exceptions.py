"""Actionable errors shared across DOWNFORCE domain boundaries."""


class DownforceError(Exception):
    """Base class for expected, user-actionable DOWNFORCE failures."""


class ProviderUnavailableError(DownforceError):
    """Raised when a configured provider cannot be reached or initialized."""


class SessionNotFoundError(DownforceError):
    """Raised when a provider or repository cannot resolve a requested session."""


class ProviderCapabilityError(DownforceError):
    """Raised when a request needs a capability the provider does not expose."""


class NormalizationError(DownforceError):
    """Raised when provider data cannot be converted into canonical records."""


class SchemaVersionError(DownforceError):
    """Raised when canonical data uses an incompatible schema version."""


class ReplayCursorError(DownforceError):
    """Raised when a replay cursor is invalid or cannot be resolved unambiguously."""


class SessionDataIncompleteError(DownforceError):
    """Raised when required canonical session data is missing or incomplete."""


class StorageIntegrityError(DownforceError):
    """Raised when stored canonical data fails an integrity check."""


__all__ = [
    "DownforceError",
    "NormalizationError",
    "ProviderCapabilityError",
    "ProviderUnavailableError",
    "ReplayCursorError",
    "SchemaVersionError",
    "SessionDataIncompleteError",
    "SessionNotFoundError",
    "StorageIntegrityError",
]
