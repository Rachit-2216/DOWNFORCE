"""Broad historical archive, catalog, quality, and synchronization boundaries."""

from downforce_core.archive.catalog import HistoricalCatalogIndex
from downforce_core.archive.contracts import (
    ArchiveEventStatus,
    CapabilityTier,
    DataQuality,
    HistoricalCatalog,
    HistoricalEvent,
    HistoricalSeason,
    HistoricalSession,
    ProviderProvenance,
    QualityStatus,
    RaceDataCapabilities,
    SyncLifecycle,
)
from downforce_core.archive.schemas import ARCHIVE_SCHEMA_VERSION, CATALOG_VERSION, ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.archive.sync import ArchiveSyncResult, HistoricalArchiveSync

__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "CATALOG_VERSION",
    "ArchiveEventStatus",
    "ArchiveSyncResult",
    "ArchiveTableName",
    "CapabilityTier",
    "DataQuality",
    "HistoricalCatalog",
    "HistoricalCatalogIndex",
    "HistoricalArchiveStore",
    "HistoricalArchiveSync",
    "HistoricalEvent",
    "HistoricalSeason",
    "HistoricalSession",
    "ProviderProvenance",
    "QualityStatus",
    "RaceDataCapabilities",
    "SyncLifecycle",
]
