"""Read-only replay-time inference from hash-verified offline artifacts."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import cast

from downforce_core.ml.artifacts import (
    ArtifactStore,
    ArtifactUnavailableError,
    ridge_from_dict,
)
from downforce_core.ml.features import (
    FEATURE_NAMES,
    NONLINEAR_FEATURE_NAMES,
    CanonicalFeatureBuilder,
    FeatureVector,
    feature_schema_payload,
)
from downforce_core.storage import DownforceRepository


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ArtifactUnavailableError(f"ML artifact {label} is malformed")
    return cast(dict[str, object], value)


def _widths(value: object) -> dict[str, float]:
    mapping = _mapping(value, "intervals")
    try:
        widths = {key: float(cast(str | int | float, mapping[key])) for key in ("80", "90")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("ML artifact intervals are malformed") from exc
    if (
        any(not math.isfinite(item) or not 0 <= item <= 120_000 for item in widths.values())
        or widths["90"] < widths["80"]
    ):
        raise ArtifactUnavailableError("ML artifact intervals are invalid")
    return widths


def _validate_ridge(section: dict[str, object], *, nonlinear: bool) -> None:
    model = ridge_from_dict(section.get("model"))
    expected = len(NONLINEAR_FEATURE_NAMES if nonlinear else FEATURE_NAMES)
    if not (len(model.means) == len(model.scales) == len(model.coefficients) == expected):
        raise ArtifactUnavailableError("ML artifact model does not match feature schema")
    values = (
        *model.means,
        *model.scales,
        *model.coefficients,
        model.intercept,
        model.regularization,
    )
    if (
        any(not math.isfinite(item) for item in values)
        or any(item <= 0 for item in model.scales)
        or model.regularization < 0
    ):
        raise ArtifactUnavailableError("ML artifact model contains invalid coefficients")


def _validate_estimator(
    section: dict[str, object],
    *,
    label: str,
    allowed_baseline: str,
) -> None:
    selected = section.get("selected")
    if not isinstance(selected, str):
        raise ArtifactUnavailableError(f"ML artifact {label} estimator is malformed")
    nonlinear = section.get("nonlinear") is True
    if section.get("model") is None:
        if selected != allowed_baseline or nonlinear:
            raise ArtifactUnavailableError(f"ML artifact {label} estimator is malformed")
    else:
        if selected not in {"ridge-linear", "ridge-nonlinear"} or nonlinear != (
            selected == "ridge-nonlinear"
        ):
            raise ArtifactUnavailableError(f"ML artifact {label} estimator is malformed")
        _validate_ridge(section, nonlinear=nonlinear)
    _widths(section.get("interval_half_width_ms"))


def _validate_bundle(bundle: dict[str, object]) -> None:
    digest = bundle.get("dataset_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ArtifactUnavailableError("ML artifact dataset identity is malformed")
    if bundle.get("feature_schema") != feature_schema_payload():
        raise ArtifactUnavailableError("ML artifact feature schema is incompatible")
    pace = _mapping(bundle.get("pace"), "pace")
    tyre = _mapping(bundle.get("tyre_degradation"), "tyre degradation")
    pit = _mapping(bundle.get("pit_loss"), "pit loss")
    _validate_estimator(
        pace,
        label="pace",
        allowed_baseline="rolling-median-baseline",
    )
    _validate_estimator(
        tyre,
        label="tyre degradation",
        allowed_baseline="zero-residual-baseline",
    )
    _widths(pit.get("interval_half_width_ms"))
    if pit.get("selected") not in {
        "global-median",
        "circuit-median-with-global-fallback",
    }:
        raise ArtifactUnavailableError("ML artifact pit estimator is malformed")
    try:
        global_median = float(cast(str | int | float, pit["global_median_ms"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("ML artifact pit estimator is malformed") from exc
    circuit_medians = _mapping(pit.get("circuit_medians_ms"), "pit circuit medians")
    supported = pit.get("supported_circuits")
    if (
        not math.isfinite(global_median)
        or not 0 < global_median <= 120_000
        or not isinstance(supported, list)
        or not supported
        or any(not isinstance(item, str) or not item for item in supported)
    ):
        raise ArtifactUnavailableError("ML artifact pit estimator is invalid")
    try:
        medians = {
            str(key): float(cast(str | int | float, value))
            for key, value in circuit_medians.items()
        }
    except (TypeError, ValueError) as exc:
        raise ArtifactUnavailableError("ML artifact pit circuit medians are malformed") from exc
    if any(not math.isfinite(value) or not 0 < value <= 120_000 for value in medians.values()):
        raise ArtifactUnavailableError("ML artifact pit circuit medians are invalid")
    split_sessions = _mapping(bundle.get("split_sessions"), "split sessions")
    groups: list[set[str]] = []
    for split in ("train", "validation", "calibration", "test"):
        values = split_sessions.get(split)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) for item in values)
        ):
            raise ArtifactUnavailableError("ML artifact split manifest is malformed")
        groups.append(set(cast(list[str], values)))
    if any(groups[left] & groups[right] for left in range(4) for right in range(left + 1, 4)):
        raise ArtifactUnavailableError("ML artifact split manifest overlaps")


def _predict(section: dict[str, object], feature: FeatureVector) -> float:
    model_value = section.get("model")
    if model_value is None:
        selected = section.get("selected")
        if selected == "rolling-median-baseline":
            return feature.values[6]
        if selected == "zero-residual-baseline":
            return 0.0
        raise ArtifactUnavailableError("ML artifact estimator is malformed")
    model = ridge_from_dict(model_value)
    values = feature.nonlinear_values() if section.get("nonlinear") is True else feature.values
    try:
        prediction = model.predict(values)
    except ValueError as exc:
        raise ArtifactUnavailableError("ML artifact model does not match feature schema") from exc
    if not math.isfinite(prediction):
        raise ArtifactUnavailableError("ML artifact produced a non-finite prediction")
    return prediction


def _feature_at_age(feature: FeatureVector, age: float) -> FeatureVector:
    values = list(feature.values)
    values[1] = age
    return FeatureVector(
        session_id=feature.session_id,
        driver_id=feature.driver_id,
        cutoff_time_ms=feature.cutoff_time_ms,
        boundary_lap=feature.boundary_lap,
        values=tuple(values),
        compound=feature.compound,
        observed_lap_time_ms=feature.observed_lap_time_ms,
    )


class MLInferenceEngine:
    def __init__(self, repository: DownforceRepository, project_root: Path) -> None:
        self.repository = repository
        self.store = ArtifactStore(project_root)
        self._bundle: dict[str, object] | None = None
        self._builders: dict[tuple[str, str], CanonicalFeatureBuilder] = {}

    def status(self) -> dict[str, object]:
        try:
            bundle = self._load_bundle()
        except ArtifactUnavailableError as exc:
            return {
                "availability": "unavailable",
                "reason": str(exc),
                "dataset_digest": None,
                "model_version": None,
                "models": [],
            }
        return {
            "availability": "available",
            "reason": None,
            "dataset_digest": bundle.get("dataset_digest"),
            "model_version": bundle.get("model_bundle_version"),
            "models": ["pace", "tyre_degradation", "pit_loss"],
        }

    def _load_bundle(self) -> dict[str, object]:
        if self._bundle is None:
            bundle = self.store.load()
            _validate_bundle(bundle)
            self._bundle = bundle
        return self._bundle

    @staticmethod
    def _unavailable(
        bundle: dict[str, object],
        time_ms: int,
        reason: str,
        *,
        lap: int | None = None,
    ) -> dict[str, object]:
        return {
            "availability": "unavailable",
            "reason": reason,
            "model_version": bundle.get("model_bundle_version"),
            "dataset_digest": bundle.get("dataset_digest"),
            "assumptions": [],
            "as_of": {"time_ms": time_ms, "lap": lap},
            "pace": None,
            "tyre_degradation": None,
            "pit_loss": None,
        }

    def _builder(self, session_id: str) -> CanonicalFeatureBuilder:
        canonical_id, dataset_id = self.repository.active_dataset_identity(session_id)
        key = (canonical_id, dataset_id)
        cached = self._builders.get(key)
        if cached is not None:
            return cached
        session = self.repository.load_session(canonical_id, include_track_positions=False)
        builder = CanonicalFeatureBuilder(session)
        self._builders = {
            existing: value
            for existing, value in self._builders.items()
            if existing[0] != canonical_id
        }
        self._builders[key] = builder
        return builder

    def predict(self, session_id: str, driver_id: str, time_ms: int) -> dict[str, object]:
        bundle = self._load_bundle()
        builder = self._builder(session_id)
        if builder.session_is_finished_at(time_ms):
            return self._unavailable(bundle, time_ms, "session_finished")
        result = builder.feature_at(driver_id, time_ms)
        if result.feature is None:
            reason = result.eligibility.reason or "not_eligible"
            return self._unavailable(bundle, time_ms, reason)
        feature = result.feature
        pace = _mapping(bundle.get("pace"), "pace")
        tyre = _mapping(bundle.get("tyre_degradation"), "tyre degradation")
        pit = _mapping(bundle.get("pit_loss"), "pit loss")
        pace_prediction = _predict(pace, feature)
        pace_width = _widths(pace.get("interval_half_width_ms"))
        tyre_prediction = _predict(tyre, feature)
        tyre_width = _widths(tyre.get("interval_half_width_ms"))
        current_age = feature.values[1]
        circuit = builder.session.metadata.circuit_name or "unknown"
        supported = cast(list[object], pit.get("supported_circuits"))
        if circuit not in supported:
            return self._unavailable(
                bundle,
                feature.cutoff_time_ms,
                "unsupported_circuit",
                lap=feature.boundary_lap,
            )
        if current_age + 5 > 100:
            return self._unavailable(
                bundle,
                feature.cutoff_time_ms,
                "tyre_forecast_out_of_distribution",
                lap=feature.boundary_lap,
            )
        if not 50_000 <= pace_prediction <= 300_000:
            return self._unavailable(
                bundle,
                feature.cutoff_time_ms,
                "implausible_pace_output",
                lap=feature.boundary_lap,
            )
        if pace_prediction - pace_width["90"] <= 0:
            return self._unavailable(
                bundle,
                feature.cutoff_time_ms,
                "implausible_prediction_interval",
                lap=feature.boundary_lap,
            )
        if abs(tyre_prediction) > 30_000:
            return self._unavailable(
                bundle,
                feature.cutoff_time_ms,
                "implausible_tyre_output",
                lap=feature.boundary_lap,
            )
        curve: list[dict[str, object]] = []
        previous_penalty = 0.0
        for horizon in range(1, 6):
            future = _predict(tyre, _feature_at_age(feature, current_age + horizon))
            if abs(future) > 30_000:
                return self._unavailable(
                    bundle,
                    feature.cutoff_time_ms,
                    "implausible_tyre_output",
                    lap=feature.boundary_lap,
                )
            penalty = max(previous_penalty, future - tyre_prediction, 0.0)
            previous_penalty = penalty
            curve.append(
                {
                    "laps_ahead": horizon,
                    "tyre_age_laps": current_age + horizon,
                    "predicted_pace_delta_ms": round(penalty),
                }
            )
        circuit_medians = _mapping(pit.get("circuit_medians_ms"), "pit circuit medians")
        global_pit = float(cast(str | int | float, pit.get("global_median_ms", 0.0)))
        pit_estimate = float(cast(str | int | float, circuit_medians.get(circuit, global_pit)))
        pit_width = _widths(pit.get("interval_half_width_ms"))
        return {
            "availability": "available",
            "reason": None,
            "model_version": bundle.get("model_bundle_version"),
            "dataset_digest": bundle.get("dataset_digest"),
            "assumptions": [
                "next eligible lap is dry and green flag",
                "tyre age changes while current observed conditions remain fixed",
                "future weather, traffic, race control and pit decisions are unknown",
                "pit loss is a dry green-flag circuit estimate",
            ],
            "as_of": {"time_ms": feature.cutoff_time_ms, "lap": feature.boundary_lap},
            "pace": {
                "label": "Predicted next representative green-flag lap",
                "predicted_lap_time_ms": round(pace_prediction),
                "observed_latest_lap_time_ms": feature.observed_lap_time_ms,
                "interval_80": {
                    "lower_ms": round(pace_prediction - pace_width["80"]),
                    "upper_ms": round(pace_prediction + pace_width["80"]),
                },
                "interval_90": {
                    "lower_ms": round(pace_prediction - pace_width["90"]),
                    "upper_ms": round(pace_prediction + pace_width["90"]),
                },
            },
            "tyre_degradation": {
                "label": "Predicted pace residual under held-constant race conditions",
                "compound": feature.compound,
                "current_tyre_age_laps": current_age,
                "predicted_residual_ms": round(tyre_prediction),
                "interval_80_half_width_ms": round(tyre_width["80"]),
                "interval_90_half_width_ms": round(tyre_width["90"]),
                "curve": curve,
            },
            "pit_loss": {
                "label": "Estimated dry green-flag effective pit-cycle loss",
                "circuit": circuit,
                "estimated_effective_loss_ms": round(pit_estimate),
                "interval_80": {
                    "lower_ms": round(max(0.0, pit_estimate - pit_width["80"])),
                    "upper_ms": round(pit_estimate + pit_width["80"]),
                },
                "interval_90": {
                    "lower_ms": round(max(0.0, pit_estimate - pit_width["90"])),
                    "upper_ms": round(pit_estimate + pit_width["90"]),
                },
                "stationary_duration_ms": None,
                "stationary_duration_reason": "not_observed_in_canonical_data",
            },
        }


__all__ = ["MLInferenceEngine"]
