"""Application boundary for read-only hypothetical strategy operations."""

from pathlib import Path

from downforce_core.domain.enums import TyreCompound
from downforce_core.storage import DownforceRepository
from downforce_core.strategy import PitAction, ScenarioAssumptions, Strategy, StrategyEngine
from downforce_core.strategy.contracts import SIMULATION_VERSION
from downforce_core.strategy.engine import StrategyUnavailableError

from app.schemas.strategy import (
    ComparisonRequest,
    CounterfactualRequest,
    ScenarioRequest,
    SimulationRequest,
    StrategyRequest,
)


def _strategy(value: StrategyRequest) -> Strategy:
    return Strategy(
        strategy_id=value.strategy_id,
        label=value.label,
        actions=tuple(
            PitAction(action.lap, TyreCompound(action.compound)) for action in value.actions
        ),
    )


def _scenario(value: ScenarioRequest) -> ScenarioAssumptions:
    return ScenarioAssumptions(
        scheduled_total_laps=value.scheduled_total_laps,
        pit_loss_mode=value.pit_loss_mode,
        require_two_compounds=value.require_two_compounds,
    )


class StrategyService:
    def __init__(self, repository: DownforceRepository, project_root: Path) -> None:
        self.repository = repository
        self.project_root = project_root
        self._engine: StrategyEngine | None = None

    def _strategy_engine(self) -> StrategyEngine:
        if self._engine is None:
            self._engine = StrategyEngine(self.repository, self.project_root)
        return self._engine

    def status(self) -> dict[str, object]:
        try:
            return self._strategy_engine().status()
        except StrategyUnavailableError as exc:
            return {
                "availability": "unavailable",
                "reason": exc.reason,
                "simulation_version": None,
                "model_version": None,
                "dataset_digest": None,
                "default_simulation_count": None,
                "maximum_simulation_count": None,
                "assumptions": [],
            }

    @staticmethod
    def _unavailable(
        session_id: str, driver_id: str, cursor_ms: int, reason: str
    ) -> dict[str, object]:
        return {
            "status": "unavailable",
            "availability_reason": reason,
            "session_id": session_id,
            "driver_id": driver_id,
            "cursor": {"time_ms": cursor_ms, "lap": None},
            "simulation_version": SIMULATION_VERSION,
            "model_version": None,
            "dataset_digest": None,
            "assumptions": [],
            "outcome": None,
        }

    def simulate(self, session_id: str, request: SimulationRequest) -> dict[str, object]:
        try:
            return self._strategy_engine().simulate(
                session_id=session_id,
                driver_id=request.driver_id,
                cursor_ms=request.cursor_time_ms,
                strategy=_strategy(request.strategy),
                scenario=_scenario(request.scenario),
                simulations=request.simulation_count,
                seed=request.seed,
            )
        except StrategyUnavailableError as exc:
            return self._unavailable(
                session_id, request.driver_id, request.cursor_time_ms, exc.reason
            )
        except ValueError:
            return self._unavailable(
                session_id, request.driver_id, request.cursor_time_ms, "invalid_strategy"
            )

    def compare(self, session_id: str, request: ComparisonRequest) -> dict[str, object]:
        try:
            return self._strategy_engine().compare(
                session_id=session_id,
                driver_id=request.driver_id,
                cursor_ms=request.cursor_time_ms,
                strategies=tuple(_strategy(strategy) for strategy in request.strategies),
                scenario=_scenario(request.scenario),
                simulations=request.simulation_count,
                seed=request.seed,
            )
        except StrategyUnavailableError as exc:
            return self._unavailable(
                session_id, request.driver_id, request.cursor_time_ms, exc.reason
            )
        except ValueError:
            return self._unavailable(
                session_id, request.driver_id, request.cursor_time_ms, "invalid_strategy"
            )

    def counterfactual(self, session_id: str, request: CounterfactualRequest) -> dict[str, object]:
        # Hypothetical computation completes before any post-cursor observed fact is read.
        result = self.simulate(session_id, request)
        if result.get("status") != "available" or not request.include_observed_result:
            return result
        session = self.repository.load_session(session_id, include_track_positions=False)
        classification = next(
            (row for row in session.classifications if str(row.driver_id) == request.driver_id),
            None,
        )
        future_stops = [
            {"lap": stop.lap_number, "observed_pit_lane_duration_ms": stop.pit_lane_duration_ms}
            for stop in session.pit_stops
            if str(stop.driver_id) == request.driver_id
            and stop.pit_in_time_ms is not None
            and stop.pit_in_time_ms > request.cursor_time_ms
        ]
        return {
            **result,
            "observed_historical_result": {
                "label": "Observed historical fact — not a simulation input",
                "classified_position": (
                    None if classification is None else classification.classified_position
                ),
                "status": None if classification is None else classification.status.value,
                "future_pit_events": future_stops,
            },
            "counterfactual_notice": (
                "The alternative outcome is a model distribution, not observable truth. "
                "The historical result was attached only after simulation."
            ),
        }


__all__ = ["StrategyService"]
