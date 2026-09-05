"""Typed historical catalog API schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityResponse(CatalogModel):
    results: bool
    grid: bool
    lap_times: bool
    lap_positions: bool
    pit_stops: bool
    stints: bool
    compounds: bool
    weather: bool
    race_control: bool
    track_positions: bool
    telemetry: bool
    speed: bool
    throttle: bool
    brake: bool
    gear: bool
    rpm: bool
    drs: bool
    ml_intelligence: bool
    strategy_simulation: bool
    counterfactual_support: bool


class QualityResponse(CatalogModel):
    status: str
    reasons: list[str]
    metrics: dict[str, int | float | str | bool | None]
    validated_at_utc: str


class ProvenanceResponse(CatalogModel):
    provider: str
    provider_version: str
    source: str
    source_url: str
    retrieved_at_utc: str
    raw_sha256: str


class ArchiveSessionResponse(CatalogModel):
    session_id: str
    session_type: str
    status: str
    sync_status: str
    capability_tier: str
    capabilities: CapabilityResponse
    quality: QualityResponse
    provenance: list[ProvenanceResponse]
    row_counts: dict[str, int]
    data_revision: str | None
    legacy_session_id: str | None


class CatalogEventResponse(CatalogModel):
    event_id: str
    season: int
    round_number: int
    name: str
    official_name: str
    event_date: str
    circuit_name: str
    locality: str | None
    country: str | None
    country_code: str | None
    status: str
    sessions: list[ArchiveSessionResponse]
    drivers: list[str]
    teams: list[str]


class CatalogEventListResponse(CatalogModel):
    items: list[CatalogEventResponse]
    offset: int
    limit: int
    total: int


class SeasonSummaryResponse(CatalogModel):
    year: int
    event_count: int
    completed_event_count: int
    latest_event_date: str | None


class SeasonListResponse(CatalogModel):
    items: list[SeasonSummaryResponse]
    total: int
    event_count: int
    completed_event_count: int
    archive_start_year: int
    latest_completed_event_id: str | None
    latest_completed_event_date: str | None


class ArchiveCapabilityResponse(CatalogModel):
    session_id: str
    event_id: str
    status: str
    sync_status: str
    capability_tier: str
    capabilities: CapabilityResponse
    quality: QualityResponse
    row_counts: dict[str, int]
    legacy_session_id: str | None


class ArchivePageResponse(CatalogModel):
    items: list[dict[str, object]]
    offset: int
    limit: int
    total: int


class StorageStatusResponse(CatalogModel):
    file_count: int
    session_count: int
    total_bytes: int
    bytes_by_suffix: dict[str, int]
    catalog_bytes: int
    sync: dict[str, object] | None


__all__ = [
    "ArchiveCapabilityResponse",
    "ArchivePageResponse",
    "CatalogEventListResponse",
    "CatalogEventResponse",
    "SeasonListResponse",
    "StorageStatusResponse",
]
