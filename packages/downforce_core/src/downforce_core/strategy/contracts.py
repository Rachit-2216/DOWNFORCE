"""Immutable contracts for hypothetical race strategy analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from downforce_core.domain.enums import TyreCompound

SIMULATION_VERSION = "1.1.0"
DRY_COMPOUNDS = frozenset({TyreCompound.SOFT, TyreCompound.MEDIUM, TyreCompound.HARD})


class StrategyAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PitAction:
    """Fit ``compound`` for the named lap in the selected driver's own lap count."""

    lap: int
    compound: TyreCompound

    def __post_init__(self) -> None:
        if self.lap < 1:
            raise ValueError("pit action lap must be positive")
        if self.compound not in DRY_COMPOUNDS:
            raise ValueError("Step 5 supports only soft, medium and hard compounds")


@dataclass(frozen=True, slots=True)
class Strategy:
    strategy_id: str
    label: str
    actions: tuple[PitAction, ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.label.strip():
            raise ValueError("strategy identity and label must be nonempty")
        laps = tuple(action.lap for action in self.actions)
        if laps != tuple(sorted(laps)) or len(laps) != len(set(laps)):
            raise ValueError("pit actions must be unique and ordered by lap")


@dataclass(frozen=True, slots=True)
class ScenarioAssumptions:
    scheduled_total_laps: int | None = None
    pit_loss_mode: str = "sampled"
    require_two_compounds: bool = False

    def __post_init__(self) -> None:
        if self.scheduled_total_laps is not None and (
            isinstance(self.scheduled_total_laps, bool) or not 1 <= self.scheduled_total_laps <= 200
        ):
            raise ValueError("scheduled_total_laps must be between 1 and 200")
        if self.pit_loss_mode not in {"sampled", "point", "lower-90", "upper-90"}:
            raise ValueError("pit_loss_mode is invalid")


@dataclass(frozen=True, slots=True)
class DriverSimulationState:
    driver_id: str
    abbreviation: str
    status: str
    laps_completed: int
    next_completion_time_ms: float
    current_lap_elapsed_ms: float
    compound: TyreCompound
    tyre_age_laps: float
    stint_number: int
    stops_completed: int
    source_position: int
    next_actionable_lap: int
    pace_anchor_ms: float
    feature_values: tuple[float, ...]
    feature_compound: TyreCompound
    used_compounds: tuple[TyreCompound, ...]
    model_supported: bool

    def __post_init__(self) -> None:
        numeric = (
            self.next_completion_time_ms,
            self.current_lap_elapsed_ms,
            self.tyre_age_laps,
            self.pace_anchor_ms,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("simulation driver values must be finite")
        if self.laps_completed < 0 or self.stops_completed < 0 or self.stint_number < 1:
            raise ValueError("simulation driver counters are invalid")
        if self.current_lap_elapsed_ms < 0:
            raise ValueError("current partial-lap elapsed time must be nonnegative")
        if self.next_actionable_lap <= self.laps_completed + 1:
            raise ValueError("next actionable lap must not have started at the simulation cursor")
        if self.compound not in DRY_COMPOUNDS:
            raise ValueError("simulation driver compound is unsupported")
        if (
            not self.used_compounds
            or any(compound not in DRY_COMPOUNDS for compound in self.used_compounds)
            or len(self.used_compounds) != len(set(self.used_compounds))
        ):
            raise ValueError("used compounds must be unique supported dry compounds")


@dataclass(frozen=True, slots=True)
class SimulationState:
    session_id: str
    source_cursor_ms: int
    reference_lap: int
    scheduled_total_laps: int
    scheduled_distance_source: str
    circuit: str
    drivers: tuple[DriverSimulationState, ...]
    seed: int
    model_version: str
    dataset_digest: str
    assumptions: tuple[str, ...]
    excluded_drivers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.source_cursor_ms < 0 or self.reference_lap < 1:
            raise ValueError("simulation cursor is invalid")
        if self.scheduled_total_laps <= self.reference_lap:
            raise ValueError("scheduled race distance must extend beyond the cursor")
        if self.scheduled_distance_source not in {"canonical_published", "explicit_override"}:
            raise ValueError("scheduled distance source is invalid")
        identifiers = tuple(driver.driver_id for driver in self.drivers)
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("simulation field must contain unique drivers")
        excluded_identifiers = tuple(identity for identity, _reason in self.excluded_drivers)
        if len(excluded_identifiers) != len(set(excluded_identifiers)):
            raise ValueError("excluded simulation drivers must be unique")


def validate_strategy(
    strategy: Strategy,
    *,
    driver: DriverSimulationState,
    scheduled_total_laps: int,
    require_two_compounds: bool,
) -> None:
    for action in strategy.actions:
        if action.lap < driver.next_actionable_lap:
            raise ValueError(
                "pit actions must start on a not-yet-started lap in the selected driver's "
                "own lap count"
            )
        if action.lap > scheduled_total_laps:
            raise ValueError("pit action exceeds the scheduled race distance")
    compounds = {*driver.used_compounds, *(action.compound for action in strategy.actions)}
    if require_two_compounds and len(compounds) < 2:
        raise ValueError("scenario requires at least two dry compounds")


__all__ = [
    "DRY_COMPOUNDS",
    "SIMULATION_VERSION",
    "DriverSimulationState",
    "PitAction",
    "ScenarioAssumptions",
    "SimulationState",
    "Strategy",
    "StrategyAvailability",
    "validate_strategy",
]
