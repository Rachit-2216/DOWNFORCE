"""Bounded telemetry contract; provider observations and derived products are explicit."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelName = Literal[
    "speed_kmh", "rpm", "throttle_percent", "gear", "brake", "drs_raw", "x", "y", "z"
]
ChannelValue = float | int | bool | None


class TelemetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, from_attributes=True)


class TraceSettings(TelemetryModel):
    channels: str = Field(
        default="speed_kmh,rpm,throttle_percent,gear,brake,drs_raw,x,y,z", max_length=200
    )
    max_points: int = Field(default=20_000, ge=2, le=20_000)
    allow_partial: bool = False
    allow_pit_laps: bool = False
    allow_deleted: bool = False
    allow_provider_generated: bool = False
    allow_unreliable: bool = False
    max_edge_gap_seconds: float = Field(default=2.0, gt=0, le=10)


class CompareSettings(TraceSettings):
    spacing_m: float = Field(default=1.0, gt=0, le=1000)
    grid: Literal["fixed_spacing", "reference_union"] = "fixed_spacing"
    max_gap_seconds: float = Field(default=2.0, gt=0, le=10)
    max_gap_m: float = Field(default=100.0, gt=0, le=1000)


class TelemetryContext(TelemetryModel):
    replay_session_id: str | None = None
    race_analytics_session_id: str | None = None
    replay_driver_numbers: list[str] = Field(default_factory=list)
    replay_laps_by_driver: dict[str, list[int]] = Field(default_factory=dict)
    replay_unavailable_reason: str | None = (
        "Session identity is unavailable for replay verification."
    )
    race_analytics_unavailable_reason: str | None = (
        "Event identity is unavailable for race analytics verification."
    )


class TelemetrySession(TelemetryContext):
    session_id: str
    revision: str
    season: int | None
    session_type: str | None
    event: str | None
    circuit: str | None
    drivers: list[str]
    available_tables: list[str]
    channel_availability: dict[str, str]


class TelemetrySessionPage(TelemetryModel):
    items: list[TelemetrySession]
    total: int
    offset: int
    limit: int
    source: Literal["production-canonical"] = "production-canonical"
    root_configured: bool = True


class TelemetryLap(TelemetryModel):
    driver: str
    lap_number: int
    lap_time_seconds: float | None
    sector_1_seconds: float | None
    sector_2_seconds: float | None
    sector_3_seconds: float | None
    compound: str | None
    stint: float | None
    tyre_life: float | None
    track_status: str | None
    reliable: bool
    deleted: bool
    provider_generated: bool
    pit_in: bool
    pit_out: bool
    timing_available: bool
    car_available: bool
    position_available: bool
    weather_available: bool
    qualifying_phase: str | None


class TelemetryLapPage(TelemetryModel):
    session_id: str
    revision: str
    driver: str
    items: list[TelemetryLap]
    total: int
    offset: int
    limit: int


class NativeSamples(TelemetryModel):
    provenance: Literal["PROVIDER_NATIVE"] = "PROVIDER_NATIVE"
    session_time_ns: list[int]
    channels: dict[str, list[ChannelValue]]
    channel_valid_masks: dict[str, list[bool]]
    source_sensors: list[str | None]


class TraceResponse(TelemetryModel):
    session_id: str
    revision: str
    driver: str
    lap_number: int
    lap_time_seconds: float | None
    clock_source: str
    provenance: Literal["DERIVED"] = "DERIVED"
    distance_m: list[float]
    elapsed_seconds: list[float]
    channels: dict[str, list[ChannelValue]]
    channel_valid_masks: dict[str, list[bool]]
    source_samples: NativeSamples
    channel_availability: dict[str, str]
    context: TelemetryLap
    flags: list[str]
    start_covered: bool
    finish_covered: bool
    distance_derived: bool
    brake_label: Literal["BRAKE_ACTIVE"] = "BRAKE_ACTIVE"
    drs_interpretation: Literal["UNKNOWN_PROVIDER_CODE"] = "UNKNOWN_PROVIDER_CODE"
    position_provenance: Literal["DERIVED_TIME_ALIGNED_PROVIDER_COORDINATES"] = (
        "DERIVED_TIME_ALIGNED_PROVIDER_COORDINATES"
    )


class AlignedResponse(TelemetryModel):
    driver: str
    lap_number: int
    elapsed_seconds: list[float | None]
    channels: dict[str, list[ChannelValue]]
    valid_mask: list[bool]
    channel_valid_masks: dict[str, list[bool]]


class ComparisonResponse(TelemetryModel):
    session_id: str
    revision: str
    provenance: Literal["DERIVED"] = "DERIVED"
    algorithm_version: str
    corner_version: str
    settings: CompareSettings
    distance_m: list[float]
    a: AlignedResponse
    b: AlignedResponse
    delta_seconds: list[float | None]
    valid_mask: list[bool]
    end_delta_seconds: float | None
    observed_lap_delta_seconds: float | None
    finish_line_agreement_applicable: bool
    finish_delta_error_seconds: float | None
    delta_convention: str
    flags: list[str]
    availability_a: dict[str, str]
    availability_b: dict[str, str]


class CornerMarkerResponse(TelemetryModel):
    number: int
    letter: str
    distance_m: float
    x: float | None
    y: float | None
    angle: float | None
    source: str


class AnalysisWindowResponse(TelemetryModel):
    label: str
    start_m: float
    end_m: float
    kind: Literal["PROVIDER_CORNER", "DISTANCE_WINDOW"]
    marker: CornerMarkerResponse | None


class WindowMetricsResponse(TelemetryModel):
    entry_speed_kmh: float | None
    minimum_speed_kmh: float | None
    exit_speed_kmh: float | None
    gear_at_minimum: int | None
    time_seconds: float | None
    brake_onset_m: float | None
    brake_release_m: float | None
    throttle_pickup_m: float | None
    full_throttle_m: float | None
    flags: list[str]


class WindowComparisonResponse(TelemetryModel):
    window: AnalysisWindowResponse
    a: WindowMetricsResponse
    b: WindowMetricsResponse
    delta_seconds: float | None
    algorithm_version: str
    provenance: Literal["DERIVED"]
    persistence_seconds: float
    throttle_pickup_threshold_percent: float
    full_throttle_threshold_percent: float
    brake_label: str


class CornersResponse(TelemetryModel):
    session_id: str
    revision: str
    algorithm_version: str
    items: list[WindowComparisonResponse]
    markers_available: bool
    claim: Literal["PROVIDER_MARKER_WINDOWS_NOT_PHYSICAL_APEX"] = (
        "PROVIDER_MARKER_WINDOWS_NOT_PHYSICAL_APEX"
    )


class MicrosectorResponse(TelemetryModel):
    index: int
    start_m: float
    end_m: float
    time_a_seconds: float | None
    time_b_seconds: float | None
    delta_seconds: float | None
    label: str


class MicrosectorsResponse(TelemetryModel):
    session_id: str
    revision: str
    sectors: list[MicrosectorResponse]
    total_delta_seconds: float | None
    sum_error_seconds: float | None
    largest_gain_a_index: int | None
    largest_gain_b_index: int | None
    fastest_a_index: int | None
    fastest_b_index: int | None
    slowest_a_index: int | None
    slowest_b_index: int | None
    flags: list[str]
    accuracy: str
