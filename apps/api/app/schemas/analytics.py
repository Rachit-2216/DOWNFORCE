"""Typed HTTP contracts for deterministic historical analytics."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENTITY_ID_PATTERN = r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$"


class AnalyticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverageResponse(AnalyticsModel):
    sample_count: int
    race_count: int
    eligible_race_count: int
    missing_count: int
    verified_count: int
    good_count: int
    quality_exclusions: int
    analytics_version: str
    archive_source_revision: str
    ratio: float | None


class AnalyticsStatusResponse(AnalyticsModel):
    analytics_version: str
    archive_source_revision: str
    snapshot_digest: str
    driver_race_observations: int
    completed_races: int
    provider_circuit_identities: int
    built_at_utc: str


class AnalyticsPageResponse(AnalyticsModel):
    items: list[dict[str, object]]
    offset: int
    limit: int
    total: int
    coverage: CoverageResponse
    analytics_version: str
    archive_source_revision: str


class SeasonAnalyticsResponse(AnalyticsModel):
    analytics_version: str
    archive_source_revision: str
    season: int
    summary: dict[str, object]
    competitiveness: dict[str, object]
    drivers: list[dict[str, object]]
    constructors: list[dict[str, object]]
    races: list[dict[str, object]]
    driver_points_progression: list[dict[str, object]]
    constructor_points_progression: list[dict[str, object]]
    coverage: dict[str, CoverageResponse]


class EntityProfileResponse(AnalyticsModel):
    entity: dict[str, object]
    summary: dict[str, object]
    seasons: list[dict[str, object]] | None = None
    races: dict[str, object]
    drivers: list[dict[str, object]] | None = None
    constructors: list[dict[str, object]] | None = None
    circuits: list[dict[str, object]] | None = None
    finish_distribution: dict[str, int] | None = None
    pit_trend: list[dict[str, object]] | None = None
    coverage: dict[str, CoverageResponse]
    analytics_version: str
    archive_source_revision: str


class RaceAnalyticsResponse(AnalyticsModel):
    event: dict[str, object]
    summary: dict[str, object]
    drivers: list[dict[str, object]]
    biggest_movers: list[dict[str, object]]
    position_progression: list[dict[str, object]]
    coverage: dict[str, CoverageResponse]
    analytics_version: str
    archive_source_revision: str


class AnalyticsFilterRequest(AnalyticsModel):
    start_season: int = Field(default=2000, ge=2000, le=9999)
    end_season: int = Field(default=9999, ge=2000, le=9999)
    circuit_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=ENTITY_ID_PATTERN,
    )


class ComparisonRequest(AnalyticsModel):
    entity_type: str = Field(pattern="^(driver|constructor)$")
    entity_a: str = Field(min_length=1, max_length=120, pattern=ENTITY_ID_PATTERN)
    entity_b: str = Field(min_length=1, max_length=120, pattern=ENTITY_ID_PATTERN)
    mode: str = Field(default="common_races", pattern="^(common_races|all_selected_races)$")
    filters: AnalyticsFilterRequest = Field(default_factory=AnalyticsFilterRequest)

    @model_validator(mode="after")
    def entities_must_be_distinct(self) -> Self:
        if self.entity_a == self.entity_b:
            raise ValueError("comparison entities must be distinct")
        return self


class ComparisonResponse(AnalyticsModel):
    entity_type: str
    mode: str
    filters: dict[str, object]
    entity_a: dict[str, object]
    entity_b: dict[str, object]
    common_race_count: int
    head_to_head: dict[str, object]
    coverage: CoverageResponse
    analytics_version: str
    archive_source_revision: str


class RankingResponse(AnalyticsModel):
    entity_type: str
    metric: str
    minimum_starts: int
    items: list[dict[str, object]]
    offset: int
    limit: int
    total: int
    coverage: CoverageResponse
    analytics_version: str
    archive_source_revision: str


class CoverageReportResponse(AnalyticsModel):
    analytics_version: str
    archive_source_revision: str
    metrics: dict[str, CoverageResponse]
    eras: list[dict[str, object]]


__all__ = [
    "AnalyticsPageResponse",
    "AnalyticsStatusResponse",
    "ComparisonRequest",
    "ComparisonResponse",
    "CoverageReportResponse",
    "EntityProfileResponse",
    "RaceAnalyticsResponse",
    "RankingResponse",
    "SeasonAnalyticsResponse",
]
