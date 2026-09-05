"""Versioned canonical historical-session API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(ApiModel):
    offset: int
    limit: int
    total: int


class SessionSummaryResponse(ApiModel):
    session_id: str
    dataset_id: str
    season: int
    event_name: str
    session_type: str
    provider: str
    created_at_utc: datetime


class SessionListResponse(Page):
    items: list[SessionSummaryResponse]


class TableSummaryResponse(ApiModel):
    availability: str
    materialized: bool
    row_count: int
    min_session_time_ms: int | None
    max_session_time_ms: int | None


class SessionResponse(ApiModel):
    session_id: str
    dataset_id: str
    snapshot_id: str
    session: dict[str, object]
    provider: dict[str, object]
    capabilities: dict[str, object]
    completeness: dict[str, object]
    tables: dict[str, TableSummaryResponse]
    warnings: list[str]
    canonical_schema_version: str
    normalization_version: str
    timeline_version: str | None
    replay_version: str | None


class DriverResponse(ApiModel):
    driver_id: str
    racing_number: int | None
    abbreviation: str | None
    full_name: str | None
    team_name: str | None
    country_code: str | None


class DriverListResponse(ApiModel):
    items: list[DriverResponse]
    total: int


class LapResponse(ApiModel):
    driver_id: str
    lap_number: int
    lap_start_time_ms: int | None
    lap_end_time_ms: int | None
    lap_time_ms: int | None
    sector_1_time_ms: int | None
    sector_2_time_ms: int | None
    sector_3_time_ms: int | None
    stint_number: int | None
    compound: str
    raw_compound: str | None
    tyre_life_laps: float | None
    is_personal_best: bool | None
    is_accurate: bool | None
    is_generated: bool | None
    is_deleted: bool | None
    deleted_reason: str | None
    raw_track_status: str | None


class LapListResponse(Page):
    items: list[LapResponse]


class TrackPositionResponse(ApiModel):
    driver_id: str
    session_time_ms: int
    x_m: float
    y_m: float
    z_m: float | None
    raw_status: str | None


class TrackPositionListResponse(Page):
    items: list[TrackPositionResponse]


class TelemetryIndexResponse(ApiModel):
    driver_id: str
    start_time_ms: int
    end_time_ms: int
    data_key: str
    channel_names: list[str]
    sample_count: int
    lap_number: int | None


class TelemetryIndexListResponse(ApiModel):
    items: list[TelemetryIndexResponse]
    total: int


class TimelineEventResponse(ApiModel):
    event_id: str
    session_time_ms: int
    priority: int
    sequence: int
    event_type: str
    driver_id: str | None
    source: str
    source_key: str | None
    payload: dict[str, object]


class TimelineResponse(Page):
    items: list[TimelineEventResponse]


class WeatherStateResponse(ApiModel):
    observed_at_ms: int
    air_temperature_c: float | None
    track_temperature_c: float | None
    humidity_percent: float | None
    pressure_hpa: float | None
    rainfall: bool | None
    wind_speed_mps: float | None
    wind_direction_deg: float | None


class DriverStateResponse(ApiModel):
    driver_id: str
    racing_number: int | None
    abbreviation: str | None
    full_name: str | None
    team_name: str | None
    status: str
    position: int | None
    laps_completed: int
    current_stint: int | None
    compound: str
    tyre_age_laps: float | None
    last_lap_time_ms: int | None
    in_pit: bool
    pit_stop_count: int
    last_pit_lap: int | None


class RaceControlStateResponse(ApiModel):
    observed_at_ms: int
    message: str
    category: str | None
    scope: str | None
    driver_id: str | None


class RaceStateResponse(ApiModel):
    replay_version: str
    session_id: str
    session_time_ms: int
    reference_lap: int | None
    track_status: str
    weather: WeatherStateResponse | None
    drivers: list[DriverStateResponse]
    recent_race_control: list[RaceControlStateResponse]
    completeness: dict[str, str]
    data_quality: str


__all__ = [
    "DriverListResponse",
    "LapListResponse",
    "RaceStateResponse",
    "SessionListResponse",
    "SessionResponse",
    "TelemetryIndexListResponse",
    "TimelineResponse",
    "TrackPositionListResponse",
]
