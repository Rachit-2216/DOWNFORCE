"""One causal feature builder for both training examples and replay inference."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass
from typing import Final

from downforce_core.domain.enums import TrackStatus, TyreCompound
from downforce_core.domain.models import (
    DriverRecord,
    LapRecord,
    PitStopRecord,
    RacePositionRecord,
    WeatherRecord,
)
from downforce_core.ml.contracts import ML_SCHEMA_VERSION, Eligibility, lap_eligibility
from downforce_core.normalization import NormalizedSession


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    value_type: str
    unit: str
    semantic_definition: str
    source: str
    earliest_availability: str
    missing_behavior: str
    category: str
    minimum: float | None = None
    maximum: float | None = None


FEATURE_DEFINITIONS: Final[tuple[FeatureDefinition, ...]] = (
    FeatureDefinition(
        "lap_number",
        "float",
        "lap",
        "Completed boundary lap number",
        "laps",
        "lap_end_time_ms",
        "unavailable",
        "dynamic_numeric",
        1,
        200,
    ),
    FeatureDefinition(
        "tyre_age_laps",
        "float",
        "lap",
        "Observed tyre life at the boundary",
        "laps",
        "lap_end_time_ms",
        "unavailable",
        "dynamic_numeric",
        0,
        100,
    ),
    FeatureDefinition(
        "stint_number",
        "float",
        "count",
        "Current observed stint number",
        "laps",
        "lap_end_time_ms",
        "unavailable",
        "dynamic_numeric",
        1,
        20,
    ),
    FeatureDefinition(
        "pit_count",
        "float",
        "count",
        "Pit exits completed by the cutoff",
        "pit_stops",
        "pit_out_time_ms",
        "zero when none",
        "dynamic_numeric",
        0,
        20,
    ),
    FeatureDefinition(
        "race_position",
        "float",
        "position",
        "Latest known race position",
        "race_positions",
        "session_time_ms",
        "unavailable",
        "dynamic_numeric",
        1,
        30,
    ),
    FeatureDefinition(
        "latest_lap_ms",
        "float",
        "ms",
        "Latest eligible completed lap time",
        "laps",
        "lap_end_time_ms",
        "unavailable",
        "dynamic_numeric",
        50_000,
        300_000,
    ),
    FeatureDefinition(
        "rolling_median_3_ms",
        "float",
        "ms",
        "Median of up to three eligible laps",
        "laps",
        "lap_end_time_ms",
        "unavailable",
        "dynamic_numeric",
        50_000,
        300_000,
    ),
    FeatureDefinition(
        "rolling_std_3_ms",
        "float",
        "ms",
        "Population deviation of up to three eligible laps",
        "laps",
        "lap_end_time_ms",
        "zero only when one sample",
        "dynamic_numeric",
        0,
        125_000,
    ),
    FeatureDefinition(
        "rolling_trend_3_ms",
        "float",
        "ms/lap",
        "Endpoint pace trend across up to three eligible laps",
        "laps",
        "lap_end_time_ms",
        "zero only when one sample",
        "dynamic_numeric",
        -125_000,
        125_000,
    ),
    FeatureDefinition(
        "track_temperature_c",
        "float",
        "celsius",
        "Latest observed track temperature",
        "weather",
        "session_time_ms",
        "unavailable",
        "dynamic_numeric",
        -10,
        80,
    ),
    FeatureDefinition(
        "air_temperature_c",
        "float",
        "celsius",
        "Latest observed air temperature",
        "weather",
        "session_time_ms",
        "unavailable",
        "dynamic_numeric",
        -20,
        60,
    ),
    FeatureDefinition(
        "humidity_percent",
        "float",
        "percent",
        "Latest observed relative humidity",
        "weather",
        "session_time_ms",
        "unavailable",
        "dynamic_numeric",
        0,
        100,
    ),
    FeatureDefinition(
        "compound_soft",
        "float",
        "flag",
        "Current compound is soft",
        "laps",
        "lap_end_time_ms",
        "unavailable for unsupported compound",
        "dynamic_categorical",
        0,
        1,
    ),
    FeatureDefinition(
        "compound_medium",
        "float",
        "flag",
        "Current compound is medium",
        "laps",
        "lap_end_time_ms",
        "unavailable for unsupported compound",
        "dynamic_categorical",
        0,
        1,
    ),
    FeatureDefinition(
        "compound_hard",
        "float",
        "flag",
        "Current compound is hard",
        "laps",
        "lap_end_time_ms",
        "unavailable for unsupported compound",
        "dynamic_categorical",
        0,
        1,
    ),
)

NONLINEAR_FEATURE_DEFINITIONS: Final[tuple[FeatureDefinition, ...]] = (
    FeatureDefinition(
        "tyre_age_squared",
        "float",
        "lap^2",
        "Squared tyre age basis",
        "derived",
        "feature cutoff",
        "unavailable",
        "derived_numeric",
        0,
        10_000,
    ),
    FeatureDefinition(
        "tyre_age_x_lap",
        "float",
        "lap^2",
        "Tyre age by race-lap interaction",
        "derived",
        "feature cutoff",
        "unavailable",
        "derived_numeric",
        0,
        20_000,
    ),
    FeatureDefinition(
        "tyre_age_x_track_temperature",
        "float",
        "lap*celsius",
        "Tyre age by track-temperature interaction",
        "derived",
        "feature cutoff",
        "unavailable",
        "derived_numeric",
        -1_000,
        8_000,
    ),
)

FEATURE_NAMES: Final[tuple[str, ...]] = tuple(item.name for item in FEATURE_DEFINITIONS)
NONLINEAR_FEATURE_NAMES: Final[tuple[str, ...]] = FEATURE_NAMES + tuple(
    item.name for item in NONLINEAR_FEATURE_DEFINITIONS
)


def feature_schema_payload() -> dict[str, object]:
    """Return the exact ordered, machine-readable training/serving schema."""

    return {
        "version": ML_SCHEMA_VERSION,
        "features": [asdict(item) for item in FEATURE_DEFINITIONS],
        "nonlinear_features": [
            asdict(item) for item in FEATURE_DEFINITIONS + NONLINEAR_FEATURE_DEFINITIONS
        ],
    }


@dataclass(frozen=True, slots=True)
class FeatureVector:
    session_id: str
    driver_id: str
    cutoff_time_ms: int
    boundary_lap: int
    values: tuple[float, ...]
    compound: str
    observed_lap_time_ms: int

    def nonlinear_values(self) -> tuple[float, ...]:
        age = self.values[1]
        return self.values + (age * age, age * self.values[0], age * self.values[9])


@dataclass(frozen=True, slots=True)
class FeatureResult:
    feature: FeatureVector | None
    eligibility: Eligibility


class CanonicalFeatureBuilder:
    """Pre-index a canonical session, while enforcing every lookup at an as-of cutoff."""

    def __init__(self, session: NormalizedSession) -> None:
        self.session = session
        self._drivers = {str(driver.driver_id): driver for driver in session.drivers}
        self._laps: dict[str, tuple[LapRecord, ...]] = {}
        for driver in session.drivers:
            driver_id = str(driver.driver_id)
            self._laps[driver_id] = tuple(
                sorted(
                    (lap for lap in session.laps if str(lap.driver_id) == driver_id),
                    key=lambda lap: lap.lap_number,
                )
            )
        self._weather = tuple(sorted(session.weather, key=lambda row: row.session_time_ms))
        self._weather_times = tuple(row.session_time_ms for row in self._weather)
        self._positions: dict[str, tuple[RacePositionRecord, ...]] = {}
        for driver_id in self._drivers:
            self._positions[driver_id] = tuple(
                sorted(
                    (row for row in session.race_positions if str(row.driver_id) == driver_id),
                    key=lambda row: row.session_time_ms,
                )
            )
        self._pits: dict[str, tuple[PitStopRecord, ...]] = {}
        for driver_id in self._drivers:
            self._pits[driver_id] = tuple(
                sorted(
                    (pit for pit in session.pit_stops if str(pit.driver_id) == driver_id),
                    key=lambda pit: (pit.lap_number or 0, pit.stop_number),
                )
            )
        self._race_control = tuple(
            sorted(session.race_control, key=lambda row: row.session_time_ms)
        )
        restart_times: list[int] = []
        red_flag_seen = False
        for record in self._race_control:
            if record.track_status is TrackStatus.RED_FLAG:
                red_flag_seen = True
            if red_flag_seen and record.message.strip().casefold() == "started":
                restart_times.append(record.session_time_ms)
                red_flag_seen = False
        self._restart_laps: dict[str, frozenset[int]] = {}
        for driver_id, laps in self._laps.items():
            restart_laps: set[int] = set()
            for restart_time in restart_times:
                first_timed = next(
                    (
                        lap
                        for lap in laps
                        if lap.lap_end_time_ms is not None
                        and lap.lap_end_time_ms > restart_time
                        and lap.lap_time_ms is not None
                    ),
                    None,
                )
                if first_timed is not None:
                    restart_laps.add(first_timed.lap_number)
            self._restart_laps[driver_id] = frozenset(restart_laps)

    @property
    def drivers(self) -> tuple[DriverRecord, ...]:
        return tuple(self._drivers.values())

    def laps_for(self, driver_id: str) -> tuple[LapRecord, ...]:
        return self._laps.get(driver_id, ())

    def pit_laps_for(self, driver_id: str) -> frozenset[int]:
        return frozenset(
            pit.lap_number for pit in self._pits.get(driver_id, ()) if pit.lap_number is not None
        )

    def is_restart_lap(self, driver_id: str, lap: LapRecord) -> bool:
        """Return whether this is the first timed lap after a red-flag restart."""

        return lap.lap_number in self._restart_laps.get(driver_id, frozenset())

    def session_is_finished_at(self, cutoff_time_ms: int) -> bool:
        """Return a causal session-finished state using observations available by cutoff."""

        terminal_statuses = {"finished", "finalised", "finalized", "ends", "ended"}
        for record in self._race_control:
            if record.session_time_ms > cutoff_time_ms:
                break
            message = " ".join(record.message.strip().casefold().split())
            raw_status = "" if record.raw_status is None else record.raw_status.strip().casefold()
            source_kind = "" if record.source_kind is None else record.source_kind.casefold()
            if "chequered" in message or raw_status == "chequered":
                return True
            if source_kind == "session_status" and (
                message in terminal_statuses or raw_status in terminal_statuses
            ):
                return True
        return False

    def weather_at(self, cutoff_time_ms: int) -> WeatherRecord | None:
        index = bisect_right(self._weather_times, cutoff_time_ms) - 1
        return None if index < 0 else self._weather[index]

    def lap_weather_is_dry(self, lap: LapRecord) -> bool:
        """Require explicitly dry weather for the entire observed lap interval."""

        if lap.lap_start_time_ms is None or lap.lap_end_time_ms is None:
            return False
        at_start = self.weather_at(lap.lap_start_time_ms)
        if at_start is None or at_start.rainfall is not False:
            return False
        within_lap = (
            row
            for row in self._weather
            if lap.lap_start_time_ms < row.session_time_ms <= lap.lap_end_time_ms
        )
        return all(row.rainfall is False for row in within_lap)

    def eligibility(self, driver_id: str, lap: LapRecord) -> Eligibility:
        if lap.lap_end_time_ms is None:
            return Eligibility(False, "missing_timing")
        if self.is_restart_lap(driver_id, lap):
            return Eligibility(False, "restart_lap")
        return lap_eligibility(
            lap,
            pit_laps=self.pit_laps_for(driver_id),
            raining=False if self.lap_weather_is_dry(lap) else None,
        )

    def latest_completed_lap(self, driver_id: str, cutoff_time_ms: int) -> LapRecord | None:
        completed = [
            lap
            for lap in self.laps_for(driver_id)
            if lap.lap_end_time_ms is not None and lap.lap_end_time_ms <= cutoff_time_ms
        ]
        return completed[-1] if completed else None

    def is_lapped_at_boundary(self, lap: LapRecord) -> bool:
        """Return whether another driver is already on a higher lap at this exact lap end."""

        cutoff = lap.lap_end_time_ms
        if cutoff is None:
            return False
        session_boundary_lap = max(
            (
                item.lap_number
                for laps in self._laps.values()
                for item in laps
                if item.lap_end_time_ms is not None and item.lap_end_time_ms <= cutoff
            ),
            default=lap.lap_number,
        )
        return session_boundary_lap > lap.lap_number

    def feature_at(self, driver_id: str, cutoff_time_ms: int) -> FeatureResult:
        if driver_id not in self._drivers:
            return FeatureResult(None, Eligibility(False, "unknown_driver"))
        lap = self.latest_completed_lap(driver_id, cutoff_time_ms)
        if lap is None:
            return FeatureResult(None, Eligibility(False, "no_completed_lap"))
        eligibility = self.eligibility(driver_id, lap)
        if not eligibility.eligible:
            return FeatureResult(None, eligibility)
        return self._feature_for_lap(driver_id, lap)

    def feature_for_lap(self, driver_id: str, lap_number: int) -> FeatureResult:
        lap = next(
            (item for item in self.laps_for(driver_id) if item.lap_number == lap_number),
            None,
        )
        if lap is None:
            return FeatureResult(None, Eligibility(False, "unknown_lap"))
        eligibility = self.eligibility(driver_id, lap)
        if not eligibility.eligible:
            return FeatureResult(None, eligibility)
        return self._feature_for_lap(driver_id, lap)

    def _feature_for_lap(self, driver_id: str, lap: LapRecord) -> FeatureResult:
        cutoff = lap.lap_end_time_ms
        if cutoff is None or lap.lap_time_ms is None:
            return FeatureResult(None, Eligibility(False, "missing_timing"))
        if lap.tyre_life_laps is None:
            return FeatureResult(None, Eligibility(False, "missing_tyre_age"))
        if lap.stint_number is None:
            return FeatureResult(None, Eligibility(False, "missing_stint"))
        if self.is_lapped_at_boundary(lap):
            return FeatureResult(None, Eligibility(False, "lapped_driver_unsupported"))
        eligible_history = [
            item
            for item in self.laps_for(driver_id)
            if item.lap_number <= lap.lap_number
            and item.lap_end_time_ms is not None
            and item.lap_end_time_ms <= cutoff
            and self.eligibility(driver_id, item).eligible
            and item.lap_time_ms is not None
        ]
        if len(eligible_history) < 2:
            return FeatureResult(None, Eligibility(False, "insufficient_clean_history"))
        recent = [float(item.lap_time_ms) for item in eligible_history[-3:] if item.lap_time_ms]
        weather = self.weather_at(cutoff)
        if weather is None:
            return FeatureResult(None, Eligibility(False, "missing_weather"))
        if (
            weather.track_temperature_c is None
            or weather.air_temperature_c is None
            or weather.humidity_percent is None
        ):
            return FeatureResult(None, Eligibility(False, "missing_weather_fields"))
        positions = [row for row in self._positions[driver_id] if row.session_time_ms <= cutoff]
        if not positions:
            return FeatureResult(None, Eligibility(False, "missing_race_position"))
        position = float(positions[-1].position)
        pits = sum(
            1
            for pit in self._pits[driver_id]
            if pit.pit_out_time_ms is not None and pit.pit_out_time_ms <= cutoff
        )
        trend = 0.0 if len(recent) < 2 else (recent[-1] - recent[0]) / (len(recent) - 1)
        compound = lap.compound
        values = (
            float(lap.lap_number),
            float(lap.tyre_life_laps),
            float(lap.stint_number),
            float(pits),
            position,
            float(lap.lap_time_ms),
            float(statistics.median(recent)),
            float(statistics.pstdev(recent)) if len(recent) > 1 else 0.0,
            trend,
            float(weather.track_temperature_c),
            float(weather.air_temperature_c),
            float(weather.humidity_percent),
            float(compound is TyreCompound.SOFT),
            float(compound is TyreCompound.MEDIUM),
            float(compound is TyreCompound.HARD),
        )
        if any(not math.isfinite(value) for value in values):
            return FeatureResult(None, Eligibility(False, "nonfinite_feature"))
        for definition, value in zip(FEATURE_DEFINITIONS, values, strict=True):
            if definition.minimum is not None and value < definition.minimum:
                return FeatureResult(
                    None,
                    Eligibility(False, f"out_of_distribution:{definition.name}"),
                )
            if definition.maximum is not None and value > definition.maximum:
                return FeatureResult(
                    None,
                    Eligibility(False, f"out_of_distribution:{definition.name}"),
                )
        return FeatureResult(
            FeatureVector(
                session_id=str(self.session.metadata.session_id),
                driver_id=driver_id,
                cutoff_time_ms=cutoff,
                boundary_lap=lap.lap_number,
                values=values,
                compound=compound.value,
                observed_lap_time_ms=lap.lap_time_ms,
            ),
            Eligibility(True),
        )


__all__ = [
    "FEATURE_DEFINITIONS",
    "FEATURE_NAMES",
    "NONLINEAR_FEATURE_DEFINITIONS",
    "NONLINEAR_FEATURE_NAMES",
    "CanonicalFeatureBuilder",
    "FeatureDefinition",
    "FeatureResult",
    "FeatureVector",
    "feature_schema_payload",
]
