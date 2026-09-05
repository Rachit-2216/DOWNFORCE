"""Independent version markers for persisted and replayed canonical data."""

from typing import Final

CANONICAL_SCHEMA_VERSION: Final = "1.0.0"
NORMALIZATION_VERSION: Final = "1.0.0"
TIMELINE_VERSION: Final = "1.0.0"
REPLAY_VERSION: Final = "1.0.0"
STORAGE_FORMAT_VERSION: Final = "1.0.0"

__all__ = [
    "CANONICAL_SCHEMA_VERSION",
    "NORMALIZATION_VERSION",
    "REPLAY_VERSION",
    "STORAGE_FORMAT_VERSION",
    "TIMELINE_VERSION",
]
