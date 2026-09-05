"""Provider contracts and lazily imported concrete adapters."""

from typing import TYPE_CHECKING

from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    LoadOptions,
    ProviderCapabilities,
    ProviderSession,
    ProviderTable,
    RaceDataProvider,
    SessionRef,
    encode_provider_metadata,
    thaw_provider_metadata,
)

if TYPE_CHECKING:
    from downforce_core.providers.fastf1_provider import FastF1Provider


def __getattr__(name: str) -> object:
    """Keep FastF1 out of core-only imports while exposing the adapter publicly."""

    if name == "FastF1Provider":
        from downforce_core.providers.fastf1_provider import FastF1Provider

        return FastF1Provider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DatasetAvailability",
    "DatasetName",
    "FastF1Provider",
    "LoadOptions",
    "ProviderCapabilities",
    "ProviderSession",
    "ProviderTable",
    "RaceDataProvider",
    "SessionRef",
    "encode_provider_metadata",
    "thaw_provider_metadata",
]
