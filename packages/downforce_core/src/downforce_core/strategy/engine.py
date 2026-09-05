"""Causal, seeded multi-driver historical race strategy simulator."""

from __future__ import annotations

import hashlib
import heapq
import math
import statistics
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from downforce_core.domain.enums import TrackStatus, TyreCompound
from downforce_core.domain.models import LapRecord
from downforce_core.domain.state import TERMINAL_DRIVER_STATUSES, ReplayDriverStatus
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError
from downforce_core.ml.features import CanonicalFeatureBuilder
from downforce_core.normalization.models import NormalizedSession
from downforce_core.replay import CanonicalTimeline, ReplayEngine, build_lap_cursors
from downforce_core.storage import DownforceRepository
from downforce_core.strategy.composition import ModelComposition
from downforce_core.strategy.contracts import (
    DRY_COMPOUNDS,
    SIMULATION_VERSION,
    DriverSimulationState,
    PitAction,
    ScenarioAssumptions,
    SimulationState,
    Strategy,
    validate_strategy,
)

DEFAULT_SIMULATIONS = 500
MAX_SIMULATIONS = 10_000
RECOMMENDATION_THRESHOLD = 0.60


class StrategyUnavailableError(RuntimeError):
    """Structured fail-closed state for unsupported simulation contexts."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _MutableDriver:
    source: DriverSimulationState
    laps: int
    completion_ms: float
    compound: TyreCompound
    tyre_age: float
    local_anchor_age: float
    stint: int
    stops: int
    finished: bool = False
    local_horizon_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class _PathOutcome:
    position: int
    classified_laps: int
    final_time_ms: float
    winner_time_ms: float
    local_horizon_exceeded: bool


@dataclass(frozen=True, slots=True)
class _SessionContext:
    session: NormalizedSession
    replay: ReplayEngine
    builder: CanonicalFeatureBuilder


def _uniform_triplet(seed: int, *parts: object) -> tuple[float, float, float]:
    """Derive three stable uniforms from one keyed digest."""

    payload = "|".join((str(seed), *(str(part) for part in parts))).encode()
    digest = hashlib.blake2b(payload, digest_size=24, person=b"downforce").digest()
    return tuple(
        (int.from_bytes(digest[index : index + 8], "big") + 0.5) / (2**64) for index in (0, 8, 16)
    )  # type: ignore[return-value]


@lru_cache(maxsize=500_000)
def _quantile_error(width80: float, width90: float, seed: int, *parts: object) -> float:
    """Bounded symmetric sampler preserving the provided 80% and 90% absolute quantiles."""

    band, within, sign_draw = _uniform_triplet(seed, *parts)
    sign = -1.0 if sign_draw < 0.5 else 1.0
    if band < 0.8:
        magnitude = within * width80
    elif band < 0.9:
        magnitude = width80 + within * max(0.0, width90 - width80)
    else:
        magnitude = width90 + within * width90
    return sign * magnitude


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires observations")
    index = probability * (len(ordered) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


class StrategyEngine:
    """Read-only hypothetical domain layered on canonical replay and frozen Step 4 artifacts."""

    assumptions = (
        "dry green-flag continuation; current observed weather is held constant",
        "no future safety car, VSC, red flag, retirement or weather transition",
        "background drivers make no unannounced future pit stops",
        "race-time ordering is simulated; detailed traffic and overtaking dynamics are not",
        "pace anchor plus tyre residual is composed once; tyre uncertainty is sampled once",
        "tyre mean projection is capped after five local laps and uncertainty then widens",
        "effective dry green-flag pit-cycle loss is charged exactly once per simulated stop",
        "scheduled distance uses causal published metadata unless explicitly overridden",
        "an in-progress lap is conditioned on the observed elapsed time and cannot be repitted",
        "calibrated marginal intervals do not identify temporal or cross-driver dependence",
        "drivers excluded from the simulated field are listed in input diagnostics",
        "dry-compound availability is hypothetical; physical tyre inventory is not asserted",
    )

    def __init__(self, repository: DownforceRepository, project_root: Path) -> None:
        self.repository = repository
        self._contexts: dict[tuple[str, str], _SessionContext] = {}
        try:
            self.composition = ModelComposition(ArtifactStore(project_root))
        except ArtifactUnavailableError as exc:
            raise StrategyUnavailableError("missing_or_corrupt_model") from exc

    def _context(self, session_id: str) -> _SessionContext:
        canonical_id, dataset_id = self.repository.active_dataset_identity(session_id)
        key = (canonical_id, dataset_id)
        cached = self._contexts.get(key)
        if cached is not None:
            return cached
        session = self.repository.load_session(canonical_id, include_track_positions=False)
        events = self.repository.load_events(canonical_id)
        if not events:
            raise StrategyUnavailableError("missing_canonical_timeline")
        cursors = build_lap_cursors(session)
        timeline = CanonicalTimeline(
            session_id=session.metadata.session_id,
            events=events,
            lap_cursors={cursor.lap_number: cursor for cursor in cursors},
        )
        context = _SessionContext(
            session=session,
            replay=ReplayEngine(session, timeline),
            builder=CanonicalFeatureBuilder(session),
        )
        self._contexts = {
            existing: value
            for existing, value in self._contexts.items()
            if existing[0] != canonical_id
        }
        self._contexts[key] = context
        return context

    @staticmethod
    def _published_scheduled_laps(session: NormalizedSession) -> int | None:
        value = session.provider_metadata.get("total_laps")
        if session.provider_name != "fastf1" or type(value) is not int:
            return None
        return value if 1 <= value <= 200 else None

    def status(self) -> dict[str, object]:
        return {
            "availability": "available",
            "reason": None,
            "simulation_version": SIMULATION_VERSION,
            "model_version": self.composition.model_version,
            "dataset_digest": self.composition.dataset_digest,
            "default_simulation_count": DEFAULT_SIMULATIONS,
            "maximum_simulation_count": MAX_SIMULATIONS,
            "assumptions": list(self.assumptions),
        }

    def _unavailable(
        self, *, session_id: str, driver_id: str, cursor_ms: int, reason: str
    ) -> dict[str, object]:
        return {
            "status": "unavailable",
            "availability_reason": reason,
            "session_id": session_id,
            "driver_id": driver_id,
            "cursor": {"time_ms": cursor_ms, "lap": None},
            "simulation_version": SIMULATION_VERSION,
            "model_version": self.composition.model_version,
            "dataset_digest": self.composition.dataset_digest,
            "assumptions": list(self.assumptions),
            "outcome": None,
        }

    def build_state(
        self,
        session_id: str,
        driver_id: str,
        cursor_ms: int,
        scenario: ScenarioAssumptions,
        seed: int,
    ) -> SimulationState:
        if cursor_ms < 0:
            raise StrategyUnavailableError("invalid_cursor")
        context = self._context(session_id)
        session = context.session
        factual = context.replay.state_at(cursor_ms)
        builder = context.builder
        if builder.session_is_finished_at(cursor_ms):
            raise StrategyUnavailableError("session_finished")
        if factual.track_status is not TrackStatus.CLEAR:
            raise StrategyUnavailableError("neutralized_or_unknown_track")
        if factual.weather is None or factual.weather.rainfall is not False:
            raise StrategyUnavailableError("unsupported_wet_or_unknown_weather")
        if factual.reference_lap is None:
            raise StrategyUnavailableError("no_completed_race_lap")
        published_total_laps = self._published_scheduled_laps(session)
        if scenario.scheduled_total_laps is None:
            if published_total_laps is None:
                raise StrategyUnavailableError("scheduled_distance_required")
            scheduled_total_laps = published_total_laps
            scheduled_distance_source = "canonical_published"
        else:
            scheduled_total_laps = scenario.scheduled_total_laps
            scheduled_distance_source = "explicit_override"
        if scheduled_total_laps <= factual.reference_lap:
            raise StrategyUnavailableError("session_finished_or_invalid_distance")
        circuit = session.metadata.circuit_name or "unknown"
        try:
            self.composition.pit_estimate(circuit)
        except ArtifactUnavailableError as exc:
            raise StrategyUnavailableError("unsupported_circuit") from exc

        target_feature_result = builder.feature_at(driver_id, cursor_ms)
        if target_feature_result.feature is None:
            raise StrategyUnavailableError(
                target_feature_result.eligibility.reason or "insufficient_target_state"
            )
        causal_laps: dict[str, list[LapRecord]] = {}
        for lap in session.laps:
            if lap.lap_end_time_ms is not None and lap.lap_end_time_ms <= cursor_ms:
                causal_laps.setdefault(str(lap.driver_id), []).append(lap)

        drivers: list[DriverSimulationState] = []
        excluded_drivers: list[tuple[str, str]] = []
        target_seen = False
        for factual_driver in factual.drivers.values():
            identity = str(factual_driver.driver_id)
            if factual_driver.status in TERMINAL_DRIVER_STATUSES:
                if identity == driver_id:
                    raise StrategyUnavailableError("driver_not_running")
                excluded_drivers.append((identity, f"terminal_{factual_driver.status.value}"))
                continue
            if factual_driver.status not in {
                ReplayDriverStatus.ACTIVE,
                ReplayDriverStatus.IN_PIT,
            }:
                excluded_drivers.append((identity, f"status_{factual_driver.status.value}"))
                continue
            if factual_driver.in_pit:
                if identity == driver_id:
                    raise StrategyUnavailableError("driver_in_pit")
                excluded_drivers.append((identity, "driver_in_pit"))
                continue
            if (
                factual_driver.compound not in DRY_COMPOUNDS
                or factual_driver.tyre_age_laps is None
                or factual_driver.last_lap_time_ms is None
            ):
                if identity == driver_id:
                    raise StrategyUnavailableError("unsupported_or_missing_tyre_state")
                excluded_drivers.append((identity, "unsupported_or_missing_tyre_state"))
                continue
            completed = causal_laps.get(identity, [])
            if not completed:
                if identity == driver_id:
                    raise StrategyUnavailableError("no_completed_lap")
                excluded_drivers.append((identity, "no_completed_lap"))
                continue
            latest = max(completed, key=lambda lap: (lap.lap_end_time_ms or -1, lap.lap_number))
            feature_result = builder.feature_at(identity, cursor_ms)
            supported = feature_result.feature is not None
            feature = feature_result.feature
            if identity == driver_id and feature is None:
                raise StrategyUnavailableError(
                    feature_result.eligibility.reason or "insufficient_target_state"
                )
            weather = factual.weather
            values = (
                feature.values
                if feature is not None
                else (
                    float(factual_driver.laps_completed),
                    float(factual_driver.tyre_age_laps),
                    float(factual_driver.current_stint or 1),
                    float(factual_driver.pit_stop_count),
                    float(factual_driver.position or 30),
                    float(factual_driver.last_lap_time_ms),
                    float(factual_driver.last_lap_time_ms),
                    0.0,
                    0.0,
                    float(weather.track_temperature_c or 30.0),
                    float(weather.air_temperature_c or 20.0),
                    float(weather.humidity_percent or 50.0),
                    float(factual_driver.compound is TyreCompound.SOFT),
                    float(factual_driver.compound is TyreCompound.MEDIUM),
                    float(factual_driver.compound is TyreCompound.HARD),
                )
            )
            used_compounds = tuple(
                sorted(
                    {
                        factual_driver.compound,
                        *(lap.compound for lap in completed if lap.compound in DRY_COMPOUNDS),
                    },
                    key=lambda compound: compound.value,
                )
            )
            latest_completion_ms = float(latest.lap_end_time_ms or cursor_ms)
            drivers.append(
                DriverSimulationState(
                    driver_id=identity,
                    abbreviation=factual_driver.abbreviation or identity[-6:],
                    status="active",
                    laps_completed=factual_driver.laps_completed,
                    next_completion_time_ms=latest_completion_ms,
                    current_lap_elapsed_ms=max(0.0, cursor_ms - latest_completion_ms),
                    compound=factual_driver.compound,
                    tyre_age_laps=float(factual_driver.tyre_age_laps),
                    stint_number=factual_driver.current_stint or 1,
                    stops_completed=factual_driver.pit_stop_count,
                    source_position=factual_driver.position or 30,
                    next_actionable_lap=factual_driver.laps_completed + 2,
                    pace_anchor_ms=float(values[6]),
                    feature_values=values,
                    feature_compound=factual_driver.compound,
                    used_compounds=used_compounds,
                    model_supported=supported,
                )
            )
            target_seen = target_seen or identity == driver_id
        if not target_seen:
            raise StrategyUnavailableError("driver_not_in_active_field")
        if len(drivers) < 2:
            raise StrategyUnavailableError("insufficient_active_field")
        return SimulationState(
            session_id=str(factual.session_id),
            source_cursor_ms=cursor_ms,
            reference_lap=factual.reference_lap,
            scheduled_total_laps=scheduled_total_laps,
            scheduled_distance_source=scheduled_distance_source,
            circuit=circuit,
            drivers=tuple(drivers),
            seed=seed,
            model_version=self.composition.model_version,
            dataset_digest=self.composition.dataset_digest,
            assumptions=self.assumptions,
            excluded_drivers=tuple(excluded_drivers),
        )

    def _pit_loss(
        self,
        state: SimulationState,
        scenario: ScenarioAssumptions,
        iteration: int,
        driver_id: str,
        lap: int,
    ) -> float:
        point, lower, upper = self.composition.pit_estimate(state.circuit)
        if scenario.pit_loss_mode == "point":
            return point
        if scenario.pit_loss_mode == "lower-90":
            return lower
        if scenario.pit_loss_mode == "upper-90":
            return upper
        width80 = self.composition.pit_width80
        width90 = self.composition.pit_width90
        sampled = point + _quantile_error(
            width80, width90, state.seed, iteration, driver_id, lap, "pit"
        )
        return max(0.0, sampled)

    def _schedule_next(
        self,
        driver: _MutableDriver,
        *,
        state: SimulationState,
        scenario: ScenarioAssumptions,
        strategy: Strategy | None,
        target_driver_id: str,
        iteration: int,
        minimum_duration_ms: float = 0.0,
    ) -> float:
        next_lap = driver.laps + 1
        action = next(
            (
                item
                for item in (
                    strategy.actions
                    if strategy and driver.source.driver_id == target_driver_id
                    else ()
                )
                if item.lap == next_lap
            ),
            None,
        )
        pit_loss = 0.0
        if action is not None:
            driver.compound = action.compound
            driver.tyre_age = 1.0
            driver.local_anchor_age = 0.0
            driver.stint += 1
            driver.stops += 1
            pit_loss = self._pit_loss(state, scenario, iteration, driver.source.driver_id, next_lap)
        else:
            driver.tyre_age += 1.0
        prediction = self.composition.lap_prediction(
            anchor_ms=driver.source.pace_anchor_ms,
            source_values=driver.source.feature_values,
            lap=next_lap,
            tyre_age=driver.tyre_age,
            stint=driver.stint,
            pit_count=driver.stops,
            compound=driver.compound,
            local_anchor_age=driver.local_anchor_age,
        )
        driver.local_horizon_exceeded |= prediction.local_horizon_exceeded
        attempts = 1 if minimum_duration_ms <= 0 else 512
        for attempt in range(attempts):
            draw_parts: tuple[object, ...] = (
                iteration,
                driver.source.driver_id,
                next_lap,
                "lap",
            )
            if attempt:
                draw_parts += ("partial-condition", attempt)
            noise = _quantile_error(
                prediction.error_width_80_ms,
                prediction.error_width_90_ms,
                state.seed,
                *draw_parts,
            )
            duration = max(45_000.0, prediction.mean_lap_ms + noise) + pit_loss
            if duration > minimum_duration_ms:
                return duration
        maximum_supported = (
            max(
                45_000.0,
                prediction.mean_lap_ms + 2.0 * prediction.error_width_90_ms,
            )
            + pit_loss
        )
        if maximum_supported <= minimum_duration_ms:
            raise StrategyUnavailableError("partial_lap_out_of_model_support")
        return maximum_supported

    def _path(
        self,
        state: SimulationState,
        target_driver_id: str,
        strategy: Strategy,
        scenario: ScenarioAssumptions,
        iteration: int,
    ) -> _PathOutcome:
        drivers = {
            source.driver_id: _MutableDriver(
                source=source,
                laps=source.laps_completed,
                completion_ms=source.next_completion_time_ms,
                compound=source.compound,
                tyre_age=source.tyre_age_laps,
                local_anchor_age=source.tyre_age_laps,
                stint=source.stint_number,
                stops=source.stops_completed,
            )
            for source in state.drivers
        }
        queue: list[tuple[float, int, str]] = []
        sequence = 0
        for driver in drivers.values():
            duration = self._schedule_next(
                driver,
                state=state,
                scenario=scenario,
                strategy=strategy,
                target_driver_id=target_driver_id,
                iteration=iteration,
                minimum_duration_ms=driver.source.current_lap_elapsed_ms,
            )
            driver.completion_ms += duration
            heapq.heappush(queue, (driver.completion_ms, sequence, driver.source.driver_id))
            sequence += 1
        winner_time = math.inf
        while queue:
            completion, _, identity = heapq.heappop(queue)
            if completion > winner_time:
                break
            driver = drivers[identity]
            driver.completion_ms = completion
            driver.laps += 1
            if driver.laps >= state.scheduled_total_laps:
                driver.finished = True
                winner_time = min(winner_time, completion)
                continue
            duration = self._schedule_next(
                driver,
                state=state,
                scenario=scenario,
                strategy=strategy,
                target_driver_id=target_driver_id,
                iteration=iteration,
            )
            driver.completion_ms += duration
            heapq.heappush(queue, (driver.completion_ms, sequence, identity))
            sequence += 1
        classified = sorted(
            drivers.values(),
            key=lambda item: (-item.laps, item.completion_ms, item.source.source_position),
        )
        target = drivers[target_driver_id]
        position = next(index for index, item in enumerate(classified, 1) if item is target)
        return _PathOutcome(
            position=position,
            classified_laps=target.laps,
            final_time_ms=target.completion_ms,
            winner_time_ms=winner_time,
            local_horizon_exceeded=target.local_horizon_exceeded,
        )

    def _run_paths(
        self,
        state: SimulationState,
        driver_id: str,
        strategy: Strategy,
        scenario: ScenarioAssumptions,
        simulations: int,
    ) -> list[_PathOutcome]:
        target = next(driver for driver in state.drivers if driver.driver_id == driver_id)
        validate_strategy(
            strategy,
            driver=target,
            scheduled_total_laps=state.scheduled_total_laps,
            require_two_compounds=scenario.require_two_compounds,
        )
        return [
            self._path(state, driver_id, strategy, scenario, iteration)
            for iteration in range(simulations)
        ]

    @staticmethod
    def _aggregate(outcomes: list[_PathOutcome], field_size: int) -> dict[str, object]:
        positions = [outcome.position for outcome in outcomes]
        times = [outcome.final_time_ms for outcome in outcomes]
        probabilities = Counter(positions)
        return {
            "expected_position": round(statistics.fmean(positions), 3),
            "median_position": round(statistics.median(positions), 3),
            "position_probabilities": {
                str(position): round(probabilities.get(position, 0) / len(outcomes), 4)
                for position in range(1, field_size + 1)
                if probabilities.get(position, 0)
            },
            "probability_top_3": round(
                sum(position <= 3 for position in positions) / len(outcomes), 4
            ),
            "race_time_ms": {
                "p10": round(_quantile(times, 0.1)),
                "median": round(_quantile(times, 0.5)),
                "p90": round(_quantile(times, 0.9)),
            },
            "local_tyre_horizon_exceeded_probability": round(
                sum(outcome.local_horizon_exceeded for outcome in outcomes) / len(outcomes), 4
            ),
        }

    def simulate(
        self,
        *,
        session_id: str,
        driver_id: str,
        cursor_ms: int,
        strategy: Strategy,
        scenario: ScenarioAssumptions,
        simulations: int = DEFAULT_SIMULATIONS,
        seed: int = 2216,
    ) -> dict[str, object]:
        _quantile_error.cache_clear()
        if not 10 <= simulations <= MAX_SIMULATIONS:
            raise ValueError(f"simulations must be between 10 and {MAX_SIMULATIONS}")
        try:
            state = self.build_state(session_id, driver_id, cursor_ms, scenario, seed)
            outcomes = self._run_paths(state, driver_id, strategy, scenario, simulations)
        except (StrategyUnavailableError, ArtifactUnavailableError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, StrategyUnavailableError)
                else "simulation_model_out_of_domain"
            )
            _quantile_error.cache_clear()
            return self._unavailable(
                session_id=session_id, driver_id=driver_id, cursor_ms=cursor_ms, reason=reason
            )
        response: dict[str, object] = {
            "status": "available",
            "availability_reason": None,
            "session_id": state.session_id,
            "driver_id": driver_id,
            "cursor": {"time_ms": cursor_ms, "lap": state.reference_lap},
            "strategy": strategy_to_dict(strategy),
            "scenario": {
                "scheduled_total_laps": state.scheduled_total_laps,
                "scheduled_distance_source": state.scheduled_distance_source,
                "requested_scheduled_total_laps": scenario.scheduled_total_laps,
                "pit_loss_mode": scenario.pit_loss_mode,
                "require_two_compounds": scenario.require_two_compounds,
            },
            "simulation_count": simulations,
            "seed": seed,
            "model_version": state.model_version,
            "dataset_digest": state.dataset_digest,
            "simulation_version": SIMULATION_VERSION,
            "assumptions": list(state.assumptions),
            "field_size": len(state.drivers),
            "input_diagnostics": {
                "simulated_driver_count": len(state.drivers),
                "classification_scope": "simulated_supported_field",
                "background_fallback_driver_ids": [
                    driver.driver_id
                    for driver in state.drivers
                    if driver.driver_id != driver_id and not driver.model_supported
                ],
                "excluded_drivers": [
                    {"driver_id": identity, "reason": reason}
                    for identity, reason in state.excluded_drivers
                ],
            },
            "path_diagnostics": {
                "requested": simulations,
                "valid": len(outcomes),
                "failed": 0,
                "reason_counts": {},
            },
            "outcome": self._aggregate(outcomes, len(state.drivers)),
        }
        _quantile_error.cache_clear()
        return response

    def compare(
        self,
        *,
        session_id: str,
        driver_id: str,
        cursor_ms: int,
        strategies: tuple[Strategy, ...],
        scenario: ScenarioAssumptions,
        simulations: int = DEFAULT_SIMULATIONS,
        seed: int = 2216,
    ) -> dict[str, object]:
        _quantile_error.cache_clear()
        if not 2 <= len(strategies) <= 12:
            raise ValueError("comparison requires between 2 and 12 strategies")
        if len({item.strategy_id for item in strategies}) != len(strategies):
            raise ValueError("strategy identifiers must be unique")
        if not 10 <= simulations <= MAX_SIMULATIONS:
            raise ValueError(f"simulations must be between 10 and {MAX_SIMULATIONS}")
        try:
            state = self.build_state(session_id, driver_id, cursor_ms, scenario, seed)
            target = next(driver for driver in state.drivers if driver.driver_id == driver_id)
            for strategy in strategies:
                validate_strategy(
                    strategy,
                    driver=target,
                    scheduled_total_laps=state.scheduled_total_laps,
                    require_two_compounds=scenario.require_two_compounds,
                )
            base_paths = {
                strategy.strategy_id: self._run_paths(
                    state, driver_id, strategy, scenario, simulations
                )
                for strategy in strategies
            }
        except (StrategyUnavailableError, ArtifactUnavailableError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, StrategyUnavailableError)
                else "simulation_model_out_of_domain"
            )
            _quantile_error.cache_clear()
            return self._unavailable(
                session_id=session_id, driver_id=driver_id, cursor_ms=cursor_ms, reason=reason
            )
        aggregates = {
            strategy.strategy_id: self._aggregate(
                base_paths[strategy.strategy_id], len(state.drivers)
            )
            for strategy in strategies
        }
        ordered = sorted(
            strategies,
            key=lambda item: _ranking_score(aggregates[item.strategy_id]),
        )
        best, runner_up = ordered[0], ordered[1]
        paired_best = base_paths[best.strategy_id]
        paired_second = base_paths[runner_up.strategy_id]
        best_beats = (
            sum(
                left.position < right.position
                or (left.position == right.position and left.final_time_ms < right.final_time_ms)
                for left, right in zip(paired_best, paired_second, strict=True)
            )
            / simulations
        )

        sensitivity: dict[str, str] = {}
        winners: set[str] = set()
        # A bounded paired stress sweep is sufficient because these are deterministic
        # pit-loss scenarios; larger counts add latency without changing the stress values.
        sensitivity_count = min(simulations, 100)
        try:
            for mode in ("lower-90", "point", "upper-90"):
                stressed = replace(scenario, pit_loss_mode=mode)
                scores: dict[str, tuple[float, float]] = {}
                for strategy in strategies:
                    paths = self._run_paths(state, driver_id, strategy, stressed, sensitivity_count)
                    scores[strategy.strategy_id] = (
                        statistics.fmean(outcome.position for outcome in paths),
                        statistics.fmean(outcome.final_time_ms for outcome in paths),
                    )
                winner = min(scores, key=scores.__getitem__)
                winners.add(winner)
                sensitivity[mode] = winner
        except (StrategyUnavailableError, ArtifactUnavailableError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, StrategyUnavailableError)
                else "simulation_model_out_of_domain"
            )
            _quantile_error.cache_clear()
            return self._unavailable(
                session_id=session_id, driver_id=driver_id, cursor_ms=cursor_ms, reason=reason
            )
        fragile = len(winners) > 1
        horizon_values = [
            aggregates[strategy.strategy_id].get("local_tyre_horizon_exceeded_probability")
            for strategy in (best, runner_up)
        ]
        long_horizon_limited = any(
            isinstance(value, (int, float)) and float(value) > 0.5 for value in horizon_values
        )
        fallback_driver_ids = [
            driver.driver_id
            for driver in state.drivers
            if driver.driver_id != driver_id and not driver.model_supported
        ]
        input_data_limited = bool(fallback_driver_ids or state.excluded_drivers)
        guard_reasons: list[str] = []
        if best_beats < RECOMMENDATION_THRESHOLD:
            guard_reasons.append("paired_superiority_below_product_threshold")
        if fragile:
            guard_reasons.append("pit_loss_sensitivity_winner_flip")
        if long_horizon_limited:
            guard_reasons.append("local_tyre_horizon_exceeded")
        if input_data_limited:
            guard_reasons.append("incomplete_or_fallback_background_field")
        clear = not guard_reasons
        ranking = {
            "status": ("PREFERRED UNDER CURRENT ASSUMPTIONS" if clear else "NO CLEAR PREFERENCE"),
            "recommended_strategy_id": best.strategy_id if clear else None,
            "leading_strategy_id": best.strategy_id,
            "probability_leading_beats_runner_up": round(best_beats, 4),
            "recommendation_threshold": RECOMMENDATION_THRESHOLD,
            "pit_loss_sensitive": fragile,
            "long_horizon_limited": long_horizon_limited,
            "input_data_limited": input_data_limited,
            "background_strategy_assumption": "no_unannounced_future_stops",
            "guard_reasons": guard_reasons,
            "explanation": (
                "Leading strategy is preferred only under the declared simulation assumptions."
                if clear
                else (
                    "The leading comparison exceeds the validated local tyre horizon."
                    if long_horizon_limited
                    else (
                        "Background field limitations prevent a robust recommendation."
                        if input_data_limited
                        else "The evidence does not support a robust single recommendation."
                    )
                )
            ),
        }
        response: dict[str, object] = {
            "status": "available",
            "availability_reason": None,
            "session_id": state.session_id,
            "driver_id": driver_id,
            "cursor": {"time_ms": cursor_ms, "lap": state.reference_lap},
            "scenario": {
                "scheduled_total_laps": state.scheduled_total_laps,
                "scheduled_distance_source": state.scheduled_distance_source,
                "requested_scheduled_total_laps": scenario.scheduled_total_laps,
                "pit_loss_mode": scenario.pit_loss_mode,
                "require_two_compounds": scenario.require_two_compounds,
            },
            "simulation_count": simulations,
            "sensitivity_simulation_count": sensitivity_count,
            "seed": seed,
            "model_version": state.model_version,
            "dataset_digest": state.dataset_digest,
            "simulation_version": SIMULATION_VERSION,
            "assumptions": list(state.assumptions),
            "common_random_numbers": True,
            "input_diagnostics": {
                "simulated_driver_count": len(state.drivers),
                "classification_scope": "simulated_supported_field",
                "background_fallback_driver_ids": fallback_driver_ids,
                "excluded_drivers": [
                    {"driver_id": identity, "reason": reason}
                    for identity, reason in state.excluded_drivers
                ],
            },
            "path_diagnostics": {
                "primary_by_strategy": {
                    strategy.strategy_id: {
                        "requested": simulations,
                        "valid": len(base_paths[strategy.strategy_id]),
                        "failed": 0,
                        "reason_counts": {},
                    }
                    for strategy in strategies
                },
                "sensitivity_requested_per_strategy_per_mode": sensitivity_count,
                "sensitivity_failed": 0,
            },
            "strategies": [
                {
                    "strategy": strategy_to_dict(strategy),
                    "outcome": aggregates[strategy.strategy_id],
                }
                for strategy in strategies
            ],
            "ranking": ranking,
            "pit_loss_sensitivity": sensitivity,
        }
        _quantile_error.cache_clear()
        return response

    def generate_candidates(
        self,
        *,
        driver_laps_completed: int,
        scheduled_total_laps: int,
        current_compound: TyreCompound,
    ) -> tuple[Strategy, ...]:
        first_actionable = driver_laps_completed + 2
        actionable_laps = scheduled_total_laps - first_actionable + 1
        if actionable_laps < 2:
            return (Strategy("stay-out", "Stay out"),)
        alternatives = tuple(
            compound for compound in DRY_COMPOUNDS if compound is not current_compound
        )
        midpoint = first_actionable + (actionable_laps - 1) // 2
        candidates = [Strategy("stay-out", "Stay out")]
        for compound in sorted(alternatives, key=lambda item: item.value):
            candidates.append(
                Strategy(
                    f"one-stop-l{midpoint}-{compound.value}",
                    f"One stop · own lap {midpoint} · {compound.value.title()}",
                    (PitAction(midpoint, compound),),
                )
            )
        if actionable_laps >= 18:
            first = first_actionable + actionable_laps // 3
            second = first_actionable + (2 * actionable_laps) // 3
            candidates.append(
                Strategy(
                    f"two-stop-l{first}-l{second}",
                    f"Two stop · own laps {first}/{second}",
                    (
                        PitAction(first, TyreCompound.MEDIUM),
                        PitAction(second, TyreCompound.SOFT),
                    ),
                )
            )
        return tuple(candidates)


def strategy_to_dict(strategy: Strategy) -> dict[str, object]:
    return {
        "strategy_id": strategy.strategy_id,
        "label": strategy.label,
        "actions": [
            {"type": "pit", "lap": action.lap, "compound": action.compound.value}
            for action in strategy.actions
        ],
    }


def _expected_position(outcome: dict[str, object]) -> float:
    value = outcome.get("expected_position")
    if not isinstance(value, (int, float)):
        raise TypeError("strategy outcome is malformed")
    return float(value)


def _ranking_score(outcome: dict[str, object]) -> tuple[float, float]:
    race_time = outcome.get("race_time_ms")
    if not isinstance(race_time, dict):
        raise TypeError("strategy race-time outcome is malformed")
    median = race_time.get("median")
    if not isinstance(median, (int, float)):
        raise TypeError("strategy race-time outcome is malformed")
    return _expected_position(outcome), float(median)


__all__ = [
    "DEFAULT_SIMULATIONS",
    "MAX_SIMULATIONS",
    "StrategyEngine",
    "StrategyUnavailableError",
    "strategy_to_dict",
]
