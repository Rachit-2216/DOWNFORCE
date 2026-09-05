"""Typed request and response boundary for hypothetical strategy analysis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PitActionRequest(StrategyModel):
    type: Literal["pit"] = "pit"
    lap: int = Field(ge=1, le=200)
    compound: Literal["soft", "medium", "hard"]


class StrategyRequest(StrategyModel):
    strategy_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    actions: list[PitActionRequest] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def ordered_unique_actions(self) -> "StrategyRequest":
        laps = [action.lap for action in self.actions]
        if laps != sorted(laps) or len(laps) != len(set(laps)):
            raise ValueError("pit actions must be unique and ordered by driver-own lap")
        return self


class ScenarioRequest(StrategyModel):
    scheduled_total_laps: int | None = Field(default=None, ge=1, le=200)
    pit_loss_mode: Literal["sampled", "point", "lower-90", "upper-90"] = "sampled"
    require_two_compounds: bool = False


class SimulationRequest(StrategyModel):
    cursor_time_ms: int = Field(ge=0)
    driver_id: str = Field(min_length=1, max_length=300)
    strategy: StrategyRequest
    scenario: ScenarioRequest
    simulation_count: int = Field(default=500, ge=10, le=10_000)
    seed: int = Field(default=2216, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def actions_fit_explicit_distance(self) -> "SimulationRequest":
        distance = self.scenario.scheduled_total_laps
        if distance is not None and any(action.lap > distance for action in self.strategy.actions):
            raise ValueError("pit action exceeds the explicit scheduled-distance override")
        return self


class ComparisonRequest(StrategyModel):
    cursor_time_ms: int = Field(ge=0)
    driver_id: str = Field(min_length=1, max_length=300)
    strategies: list[StrategyRequest] = Field(min_length=2, max_length=12)
    scenario: ScenarioRequest
    simulation_count: int = Field(default=500, ge=10, le=10_000)
    seed: int = Field(default=2216, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def unique_strategy_ids(self) -> "ComparisonRequest":
        identifiers = [strategy.strategy_id for strategy in self.strategies]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("strategy identifiers must be unique")
        distance = self.scenario.scheduled_total_laps
        if distance is not None and any(
            action.lap > distance for strategy in self.strategies for action in strategy.actions
        ):
            raise ValueError("pit action exceeds the explicit scheduled-distance override")
        return self


class CounterfactualRequest(SimulationRequest):
    include_observed_result: bool = True


class StrategyStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    availability: Literal["available", "unavailable"]
    reason: str | None
    simulation_version: str | None = None
    model_version: str | None
    dataset_digest: str | None
    default_simulation_count: int | None = None
    maximum_simulation_count: int | None = None
    assumptions: list[str]


class StrategyResponse(BaseModel):
    """JSON-safe response; nested outcome fields are versioned by simulation_version."""

    model_config = ConfigDict(extra="allow")
    status: Literal["available", "unavailable"]
    availability_reason: str | None
    session_id: str
    driver_id: str
    cursor: dict[str, int | None]
    simulation_version: str
    model_version: str | None
    dataset_digest: str | None
    assumptions: list[str]
    outcome: dict[str, object] | None = None


__all__ = [
    "ComparisonRequest",
    "CounterfactualRequest",
    "ScenarioRequest",
    "SimulationRequest",
    "StrategyRequest",
    "StrategyResponse",
    "StrategyStatusResponse",
]
