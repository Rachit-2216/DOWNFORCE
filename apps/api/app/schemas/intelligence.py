"""Typed historical ML status and replay inference schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class IntelligenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MLStatusResponse(IntelligenceModel):
    availability: Literal["available", "unavailable"]
    reason: str | None
    dataset_digest: str | None
    model_version: str | None
    models: list[Literal["pace", "tyre_degradation", "pit_loss"]]


class AsOfResponse(IntelligenceModel):
    time_ms: int
    lap: int | None


class IntervalResponse(IntelligenceModel):
    lower_ms: int
    upper_ms: int


class PaceIntelligenceResponse(IntelligenceModel):
    label: str
    predicted_lap_time_ms: int
    observed_latest_lap_time_ms: int
    interval_80: IntervalResponse
    interval_90: IntervalResponse


class DegradationPointResponse(IntelligenceModel):
    laps_ahead: int
    tyre_age_laps: float
    predicted_pace_delta_ms: int


class TyreIntelligenceResponse(IntelligenceModel):
    label: str
    compound: str
    current_tyre_age_laps: float
    predicted_residual_ms: int
    interval_80_half_width_ms: int
    interval_90_half_width_ms: int
    curve: list[DegradationPointResponse]


class PitLossIntelligenceResponse(IntelligenceModel):
    label: str
    circuit: str
    estimated_effective_loss_ms: int
    interval_80: IntervalResponse
    interval_90: IntervalResponse
    stationary_duration_ms: None
    stationary_duration_reason: str


class IntelligenceResponse(IntelligenceModel):
    availability: Literal["available", "unavailable"]
    reason: str | None
    model_version: str | None
    dataset_digest: str | None
    assumptions: list[str]
    as_of: AsOfResponse
    pace: PaceIntelligenceResponse | None
    tyre_degradation: TyreIntelligenceResponse | None
    pit_loss: PitLossIntelligenceResponse | None


__all__ = ["IntelligenceResponse", "MLStatusResponse"]
