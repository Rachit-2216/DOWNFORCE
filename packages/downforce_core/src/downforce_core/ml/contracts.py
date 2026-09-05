"""Causal prediction contracts shared by offline training and online inference."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from downforce_core.domain.enums import TyreCompound
from downforce_core.domain.models import LapRecord

ML_SCHEMA_VERSION = "1.0.0"
MODEL_BUNDLE_VERSION = "1.0.0"
DRY_COMPOUNDS = frozenset({TyreCompound.SOFT, TyreCompound.MEDIUM, TyreCompound.HARD})


class DatasetSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    CALIBRATION = "calibration"
    TEST = "test"


class PredictionAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reason: str | None = None


def raw_track_status_is_clear(raw_status: str | None) -> bool:
    """Accept only an explicit all-clear provider status, never an unknown status."""

    if raw_status is None:
        return False
    compact = "".join(raw_status.split())
    return bool(compact) and set(compact) == {"1"}


def lap_eligibility(
    lap: LapRecord,
    *,
    pit_laps: frozenset[int],
    raining: bool | None,
) -> Eligibility:
    """Conservative eligibility for dry, representative race-pace observations."""

    if lap.lap_number <= 1:
        return Eligibility(False, "opening_lap")
    if lap.lap_time_ms is None or lap.lap_end_time_ms is None:
        return Eligibility(False, "missing_timing")
    if not 50_000 <= lap.lap_time_ms <= 300_000:
        return Eligibility(False, "implausible_lap_time")
    if lap.is_deleted is True or lap.is_generated is True or lap.is_accurate is False:
        return Eligibility(False, "invalid_lap")
    if lap.compound not in DRY_COMPOUNDS:
        return Eligibility(False, "unsupported_compound")
    if lap.lap_number in pit_laps or lap.lap_number - 1 in pit_laps:
        return Eligibility(False, "pit_cycle")
    if raining is not False:
        return Eligibility(False, "wet_or_unknown_weather")
    if not raw_track_status_is_clear(lap.raw_track_status):
        return Eligibility(False, "neutralized_or_unknown_track")
    return Eligibility(True)


__all__ = [
    "DRY_COMPOUNDS",
    "ML_SCHEMA_VERSION",
    "MODEL_BUNDLE_VERSION",
    "DatasetSplit",
    "Eligibility",
    "PredictionAvailability",
    "lap_eligibility",
    "raw_track_status_is_clear",
]
