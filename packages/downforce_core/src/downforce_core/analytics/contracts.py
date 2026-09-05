"""Typed, versioned contracts for deterministic historical analytics."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast

ANALYTICS_VERSION = "1.1.0"


class OutcomeCategory(StrEnum):
    FINISHED = "finished"
    CLASSIFIED = "classified"
    DNF = "dnf"
    DNS = "dns"
    DSQ = "dsq"
    OTHER = "other"


class AnalyticsEntity(StrEnum):
    DRIVER = "driver"
    CONSTRUCTOR = "constructor"
    CIRCUIT = "circuit"


class ComparisonMode(StrEnum):
    COMMON_RACES = "common_races"
    ALL_SELECTED_RACES = "all_selected_races"


class RankingMetric(StrEnum):
    STARTS = "starts"
    WINS = "wins"
    PODIUMS = "podiums"
    POINTS = "points"
    POSITIONS_GAINED = "positions_gained"
    AVERAGE_FINISH = "average_finish"
    DNF_RATE = "dnf_rate"
    PIT_STOPS = "pit_stops"


@dataclass(frozen=True, slots=True)
class AnalyticsQuery:
    start_season: int = 2000
    end_season: int = 9999
    driver_id: str | None = None
    constructor_id: str | None = None
    circuit_id: str | None = None

    def __post_init__(self) -> None:
        if self.start_season < 2000 or self.end_season > 9999:
            raise ValueError("analytics season range is outside the archive contract")
        if self.end_season < self.start_season:
            raise ValueError("analytics end season precedes start season")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Coverage:
    sample_count: int
    race_count: int
    eligible_race_count: int
    missing_count: int
    verified_count: int
    good_count: int
    quality_exclusions: int
    analytics_version: str
    archive_source_revision: str

    @property
    def ratio(self) -> float | None:
        if self.eligible_race_count == 0:
            return None
        return self.race_count / self.eligible_race_count

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "ratio": self.ratio}


@dataclass(frozen=True, slots=True)
class DriverRaceObservation:
    event_id: str
    session_id: str
    season: int
    round_number: int
    event_name: str
    event_date: str
    circuit_id: str
    circuit_name: str
    driver_id: str
    driver_name: str
    constructor_id: str | None
    constructor_name: str | None
    grid_position: int | None
    finish_position: int | None
    points: float
    laps_completed: int | None
    outcome: OutcomeCategory
    classified: bool
    positions_gained: int | None
    recorded_lap_count: int
    timed_lap_count: int
    raw_mean_lap_ms: float | None
    raw_median_lap_ms: float | None
    best_recorded_lap_ms: int | None
    fastest_lap_recorded: bool
    pit_stop_count: int | None
    median_pit_duration_ms: float | None
    pit_durations_ms: tuple[int, ...]
    lap_data_available: bool
    pit_data_available: bool
    quality_status: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DriverRaceObservation:
        return cls(
            event_id=str(value["event_id"]),
            session_id=str(value["session_id"]),
            season=int(cast(int, value["season"])),
            round_number=int(cast(int, value["round_number"])),
            event_name=str(value["event_name"]),
            event_date=str(value["event_date"]),
            circuit_id=str(value["circuit_id"]),
            circuit_name=str(value["circuit_name"]),
            driver_id=str(value["driver_id"]),
            driver_name=str(value["driver_name"]),
            constructor_id=(
                None if value.get("constructor_id") is None else str(value["constructor_id"])
            ),
            constructor_name=(
                None if value.get("constructor_name") is None else str(value["constructor_name"])
            ),
            grid_position=_nullable_int(value.get("grid_position")),
            finish_position=_nullable_int(value.get("finish_position")),
            points=float(cast(float, value["points"])),
            laps_completed=_nullable_int(value.get("laps_completed")),
            outcome=OutcomeCategory(str(value["outcome"])),
            classified=bool(value["classified"]),
            positions_gained=_nullable_int(value.get("positions_gained")),
            recorded_lap_count=int(cast(int, value["recorded_lap_count"])),
            timed_lap_count=int(cast(int, value["timed_lap_count"])),
            raw_mean_lap_ms=_nullable_float(value.get("raw_mean_lap_ms")),
            raw_median_lap_ms=_nullable_float(value.get("raw_median_lap_ms")),
            best_recorded_lap_ms=_nullable_int(value.get("best_recorded_lap_ms")),
            fastest_lap_recorded=bool(value["fastest_lap_recorded"]),
            pit_stop_count=_nullable_int(value.get("pit_stop_count")),
            median_pit_duration_ms=_nullable_float(value.get("median_pit_duration_ms")),
            pit_durations_ms=tuple(
                int(cast(int, item))
                for item in cast(list[object], value.get("pit_durations_ms", []))
            ),
            lap_data_available=bool(value["lap_data_available"]),
            pit_data_available=bool(value["pit_data_available"]),
            quality_status=str(value["quality_status"]),
        )


def _nullable_int(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _nullable_float(value: object) -> float | None:
    return None if value is None else float(cast(float, value))


def deterministic_key(kind: str, source_revision: str, payload: object) -> str:
    serialized = json.dumps(
        {
            "analytics_version": ANALYTICS_VERSION,
            "kind": kind,
            "payload": payload,
            "source_revision": source_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


class _JsonSerializable(Protocol):
    def to_dict(self) -> object: ...


def _json_default(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "to_dict"):
        return cast(_JsonSerializable, value).to_dict()
    raise TypeError(f"analytics cache key cannot serialize {type(value).__name__}")


__all__ = [
    "ANALYTICS_VERSION",
    "AnalyticsEntity",
    "AnalyticsQuery",
    "ComparisonMode",
    "Coverage",
    "DriverRaceObservation",
    "OutcomeCategory",
    "RankingMetric",
    "deterministic_key",
]
