"""Frozen Step 4 model adapter for strategy-time pace and uncertainty composition."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

from downforce_core.domain.enums import TyreCompound
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError, ridge_from_dict
from downforce_core.ml.features import FeatureVector


def _section(bundle: dict[str, object], name: str) -> dict[str, object]:
    value = bundle.get(name)
    if not isinstance(value, dict):
        raise ArtifactUnavailableError(f"ML artifact {name} section is malformed")
    return cast(dict[str, object], value)


def _widths(section: dict[str, object]) -> tuple[float, float]:
    value = section.get("interval_half_width_ms")
    if not isinstance(value, dict):
        raise ArtifactUnavailableError("ML artifact interval is malformed")
    try:
        width80, width90 = float(value["80"]), float(value["90"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("ML artifact interval is malformed") from exc
    if not 0 <= width80 <= width90 <= 120_000:
        raise ArtifactUnavailableError("ML artifact interval is invalid")
    return width80, width90


@dataclass(frozen=True, slots=True)
class CompositionPrediction:
    mean_lap_ms: float
    error_width_80_ms: float
    error_width_90_ms: float
    local_horizon_exceeded: bool


class ModelComposition:
    """Compose Step 4 once: rolling-pace anchor + tyre residual correction.

    The pace interval is deliberately not sampled as a second error term. The tyre target is
    already next-lap time minus rolling pace, so its conformal residual is the uncertainty of
    the composed prediction. Long-horizon mean degradation is capped at a five-lap local
    projection and only uncertainty grows thereafter.
    """

    def __init__(self, store: ArtifactStore) -> None:
        self.bundle = store.load()
        self.tyre = _section(self.bundle, "tyre_degradation")
        self.pit = _section(self.bundle, "pit_loss")
        self.tyre_width80, self.tyre_width90 = _widths(self.tyre)
        self.pit_width80, self.pit_width90 = _widths(self.pit)
        self.model_version = str(self.bundle.get("model_bundle_version"))
        self.dataset_digest = str(self.bundle.get("dataset_digest"))
        self._lap_cache: dict[tuple[object, ...], CompositionPrediction] = {}
        self._pit_cache: dict[str, tuple[float, float, float]] = {}

    def _residual(self, values: tuple[float, ...]) -> float:
        model_value = self.tyre.get("model")
        if model_value is None:
            return 0.0
        model = ridge_from_dict(model_value)
        age = values[1]
        nonlinear = values + (age * age, age * values[0], age * values[9])
        try:
            predicted = model.predict(nonlinear if self.tyre.get("nonlinear") is True else values)
        except ValueError as exc:
            raise ArtifactUnavailableError(
                "tyre model does not match the frozen feature schema"
            ) from exc
        if not math.isfinite(predicted) or abs(predicted) > 30_000:
            raise ArtifactUnavailableError("tyre model produced an implausible output")
        return predicted

    @staticmethod
    def _values_for(
        source: tuple[float, ...],
        *,
        lap: int,
        age: float,
        stint: int,
        pit_count: int,
        compound: TyreCompound,
    ) -> tuple[float, ...]:
        values = list(source)
        values[0] = float(min(200, lap))
        values[1] = float(min(100.0, age))
        values[2] = float(min(20, stint))
        values[3] = float(min(20, pit_count))
        values[12:15] = [
            float(compound is TyreCompound.SOFT),
            float(compound is TyreCompound.MEDIUM),
            float(compound is TyreCompound.HARD),
        ]
        return tuple(values)

    def lap_prediction(
        self,
        *,
        anchor_ms: float,
        source_values: tuple[float, ...],
        lap: int,
        tyre_age: float,
        stint: int,
        pit_count: int,
        compound: TyreCompound,
        local_anchor_age: float,
    ) -> CompositionPrediction:
        key = (
            anchor_ms,
            source_values,
            lap,
            tyre_age,
            stint,
            pit_count,
            compound,
            local_anchor_age,
        )
        cached = self._lap_cache.get(key)
        if cached is not None:
            return cached
        capped_age = min(tyre_age, local_anchor_age + 5.0)
        values = self._values_for(
            source_values,
            lap=lap,
            age=capped_age,
            stint=stint,
            pit_count=pit_count,
            compound=compound,
        )
        mean = anchor_ms + self._residual(values)
        if not 45_000 <= mean <= 330_000:
            raise ArtifactUnavailableError("composed lap time is outside the supported domain")
        exceeded = tyre_age > capped_age
        extra = max(0.0, tyre_age - capped_age)
        widening = math.sqrt(1.0 + extra / 5.0)
        result = CompositionPrediction(
            mean_lap_ms=mean,
            error_width_80_ms=self.tyre_width80 * widening,
            error_width_90_ms=self.tyre_width90 * widening,
            local_horizon_exceeded=exceeded,
        )
        if len(self._lap_cache) >= 100_000:
            self._lap_cache.clear()
        self._lap_cache[key] = result
        return result

    def pit_estimate(self, circuit: str) -> tuple[float, float, float]:
        cached = self._pit_cache.get(circuit)
        if cached is not None:
            return cached
        supported = self.pit.get("supported_circuits")
        medians = self.pit.get("circuit_medians_ms")
        if (
            not isinstance(supported, list)
            or circuit not in supported
            or not isinstance(medians, dict)
        ):
            raise ArtifactUnavailableError("unsupported_circuit")
        value = medians.get(circuit)
        if not isinstance(value, (int, float)) or not 0 < float(value) <= 120_000:
            raise ArtifactUnavailableError("pit model artifact is malformed")
        point = float(value)
        result = point, max(0.0, point - self.pit_width90), point + self.pit_width90
        self._pit_cache[circuit] = result
        return result


def feature_with_compound(feature: FeatureVector, compound: TyreCompound) -> tuple[float, ...]:
    return ModelComposition._values_for(
        feature.values,
        lap=feature.boundary_lap,
        age=feature.values[1],
        stint=round(feature.values[2]),
        pit_count=round(feature.values[3]),
        compound=compound,
    )


__all__ = ["CompositionPrediction", "ModelComposition", "feature_with_compound"]
