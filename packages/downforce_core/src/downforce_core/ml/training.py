"""Offline model selection, untouched test evaluation, and conformal calibration."""

from __future__ import annotations

import statistics
from dataclasses import asdict

from downforce_core.ml.artifacts import ridge_to_dict
from downforce_core.ml.contracts import DatasetSplit
from downforce_core.ml.dataset import MLDataset, PaceExample, PitLossExample
from downforce_core.ml.features import feature_schema_payload
from downforce_core.ml.model import (
    Metrics,
    RidgeModel,
    conformal_quantile,
    fit_ridge,
    regression_metrics,
)


def _metric_dict(metrics: Metrics) -> dict[str, object]:
    return {key: round(float(value), 3) for key, value in asdict(metrics).items()}


def _pace_predictions(
    rows: tuple[PaceExample, ...],
    model: RidgeModel | None,
    nonlinear: bool,
) -> list[float]:
    if model is None:
        return [row.features[6] for row in rows]
    return [model.predict(row.nonlinear_features if nonlinear else row.features) for row in rows]


def _tyre_predictions(
    rows: tuple[PaceExample, ...],
    model: RidgeModel | None,
    nonlinear: bool,
) -> list[float]:
    if model is None:
        return [0.0 for _ in rows]
    return [model.predict(row.nonlinear_features if nonlinear else row.features) for row in rows]


def _coverage(actual: list[float], predicted: list[float], width: float) -> float:
    return sum(
        abs(expected - estimate) <= width
        for expected, estimate in zip(actual, predicted, strict=True)
    ) / len(actual)


def _stratified_pace(rows: tuple[PaceExample, ...], predicted: list[float]) -> dict[str, object]:
    diagnostics: dict[str, object] = {}
    for compound in sorted({row.compound for row in rows}):
        indexes = [index for index, row in enumerate(rows) if row.compound == compound]
        if indexes:
            diagnostics[f"compound:{compound}"] = _metric_dict(
                regression_metrics(
                    [rows[index].target_lap_time_ms for index in indexes],
                    [predicted[index] for index in indexes],
                )
            )
    return diagnostics


def _pit_estimator(
    train: tuple[PitLossExample, ...],
    *,
    by_circuit: bool,
) -> tuple[float, dict[str, float]]:
    global_median = statistics.median(row.target_effective_loss_ms for row in train)
    circuits: dict[str, float] = {}
    if by_circuit:
        for circuit in {row.circuit_name for row in train}:
            values = [row.target_effective_loss_ms for row in train if row.circuit_name == circuit]
            if len(values) >= 3:
                circuits[circuit] = statistics.median(values)
    return global_median, circuits


def _pit_predict(
    rows: tuple[PitLossExample, ...], global_median: float, circuits: dict[str, float]
) -> list[float]:
    return [circuits.get(row.circuit_name, global_median) for row in rows]


def train_bundle(dataset: MLDataset, *, seed: int = 2216) -> dict[str, object]:
    """Train deterministically. ``seed`` is recorded; solvers themselves are closed-form."""

    train = dataset.split_pace(DatasetSplit.TRAIN)
    validation = dataset.split_pace(DatasetSplit.VALIDATION)
    calibration = dataset.split_pace(DatasetSplit.CALIBRATION)
    test = dataset.split_pace(DatasetSplit.TEST)
    if min(len(train), len(validation), len(calibration), len(test)) == 0:
        raise ValueError("every chronological pace split must contain examples")

    pace_linear = fit_ridge(
        [row.features for row in train],
        [row.target_lap_time_ms for row in train],
        regularization=25.0,
    )
    pace_nonlinear = fit_ridge(
        [row.nonlinear_features for row in train],
        [row.target_lap_time_ms for row in train],
        regularization=40.0,
    )
    pace_candidates: dict[str, tuple[RidgeModel | None, bool]] = {
        "rolling-median-baseline": (None, False),
        "ridge-linear": (pace_linear, False),
        "ridge-nonlinear": (pace_nonlinear, True),
    }
    pace_validation_metrics = {
        name: regression_metrics(
            [row.target_lap_time_ms for row in validation],
            _pace_predictions(validation, model, nonlinear),
        )
        for name, (model, nonlinear) in pace_candidates.items()
    }
    pace_name = min(pace_validation_metrics, key=lambda name: pace_validation_metrics[name].mae_ms)
    pace_model, pace_is_nonlinear = pace_candidates[pace_name]
    pace_calibration_predictions = _pace_predictions(calibration, pace_model, pace_is_nonlinear)
    pace_residuals = [
        row.target_lap_time_ms - prediction
        for row, prediction in zip(calibration, pace_calibration_predictions, strict=True)
    ]
    pace_widths = {
        "80": conformal_quantile(pace_residuals, 0.8),
        "90": conformal_quantile(pace_residuals, 0.9),
    }
    pace_test_predictions = _pace_predictions(test, pace_model, pace_is_nonlinear)

    tyre_linear = fit_ridge(
        [row.features for row in train],
        [row.target_pace_residual_ms for row in train],
        regularization=40.0,
    )
    tyre_nonlinear = fit_ridge(
        [row.nonlinear_features for row in train],
        [row.target_pace_residual_ms for row in train],
        regularization=60.0,
    )
    tyre_candidates: dict[str, tuple[RidgeModel | None, bool]] = {
        "zero-residual-baseline": (None, False),
        "ridge-linear": (tyre_linear, False),
        "ridge-nonlinear": (tyre_nonlinear, True),
    }
    tyre_validation_metrics = {
        name: regression_metrics(
            [row.target_pace_residual_ms for row in validation],
            _tyre_predictions(validation, model, nonlinear),
        )
        for name, (model, nonlinear) in tyre_candidates.items()
    }
    tyre_name = min(tyre_validation_metrics, key=lambda name: tyre_validation_metrics[name].mae_ms)
    tyre_model, tyre_is_nonlinear = tyre_candidates[tyre_name]
    tyre_calibration_predictions = _tyre_predictions(calibration, tyre_model, tyre_is_nonlinear)
    tyre_residuals = [
        row.target_pace_residual_ms - prediction
        for row, prediction in zip(calibration, tyre_calibration_predictions, strict=True)
    ]
    tyre_widths = {
        "80": conformal_quantile(tyre_residuals, 0.8),
        "90": conformal_quantile(tyre_residuals, 0.9),
    }
    tyre_test_predictions = _tyre_predictions(test, tyre_model, tyre_is_nonlinear)

    pit_train = dataset.split_pit_loss(DatasetSplit.TRAIN)
    pit_validation = dataset.split_pit_loss(DatasetSplit.VALIDATION)
    pit_calibration = dataset.split_pit_loss(DatasetSplit.CALIBRATION)
    pit_test = dataset.split_pit_loss(DatasetSplit.TEST)
    if min(len(pit_train), len(pit_validation), len(pit_calibration), len(pit_test)) == 0:
        raise ValueError("every chronological pit-loss split must contain examples")
    global_median, no_circuits = _pit_estimator(pit_train, by_circuit=False)
    _, circuit_medians = _pit_estimator(pit_train, by_circuit=True)
    supported_circuits = sorted({row.circuit_name for row in pit_train})
    pit_candidates = {
        "global-median": no_circuits,
        "circuit-median-with-global-fallback": circuit_medians,
    }
    pit_validation_metrics = {
        name: regression_metrics(
            [row.target_effective_loss_ms for row in pit_validation],
            _pit_predict(pit_validation, global_median, circuits),
        )
        for name, circuits in pit_candidates.items()
    }
    pit_best_name = min(
        pit_validation_metrics, key=lambda name: pit_validation_metrics[name].mae_ms
    )
    pit_best_mae = pit_validation_metrics[pit_best_name].mae_ms
    pit_name = (
        "circuit-median-with-global-fallback"
        if pit_validation_metrics["circuit-median-with-global-fallback"].mae_ms
        <= pit_best_mae * 1.10
        else pit_best_name
    )
    selected_circuits = pit_candidates[pit_name]
    pit_calibration_predictions = _pit_predict(pit_calibration, global_median, selected_circuits)
    pit_residuals = [
        row.target_effective_loss_ms - prediction
        for row, prediction in zip(pit_calibration, pit_calibration_predictions, strict=True)
    ]
    pit_widths = {
        "80": conformal_quantile(pit_residuals, 0.8),
        "90": conformal_quantile(pit_residuals, 0.9),
    }
    pit_test_predictions = _pit_predict(pit_test, global_median, selected_circuits)

    split_sessions = {
        split.value: sorted(
            {row.session_id for row in dataset.split_pace(split)}
            | {row.session_id for row in dataset.split_pit_loss(split)}
        )
        for split in DatasetSplit
    }

    return {
        "dataset_digest": dataset.digest,
        "source_datasets": [list(item) for item in dataset.source_datasets],
        "feature_schema": feature_schema_payload(),
        "row_rejections": dict(dataset.row_rejections),
        "split_sessions": split_sessions,
        "seed": seed,
        "metadata": {
            "feature_set_version": "1.0.0",
            "target_versions": {
                "pace": "next-clean-lap-v1",
                "tyre_degradation": "next-lap-residual-v1",
                "pit_loss": "dry-green-effective-cycle-v2",
            },
            "implementation": "python-stdlib-closed-form-v1",
            "known_limitations": [
                "dry green-flag historical inference only",
                "lapped-driver contexts are unsupported",
                "pit loss is available only for historically supported circuits",
                "tyre curve is a held-constant local age projection",
            ],
        },
        "split_counts": {
            split.value: {
                "pace": len(dataset.split_pace(split)),
                "pit_loss": len(dataset.split_pit_loss(split)),
            }
            for split in DatasetSplit
        },
        "pace": {
            "selected": pace_name,
            "nonlinear": pace_is_nonlinear,
            "model": None if pace_model is None else ridge_to_dict(pace_model),
            "interval_half_width_ms": pace_widths,
            "validation_candidates": {
                name: _metric_dict(metrics) for name, metrics in pace_validation_metrics.items()
            },
            "test_metrics": _metric_dict(
                regression_metrics([row.target_lap_time_ms for row in test], pace_test_predictions)
            ),
            "test_baseline_metrics": _metric_dict(
                regression_metrics(
                    [row.target_lap_time_ms for row in test],
                    _pace_predictions(test, None, False),
                )
            ),
            "test_calibration": {
                key: {
                    "nominal_coverage": float(key) / 100,
                    "empirical_coverage": round(
                        _coverage(
                            [row.target_lap_time_ms for row in test],
                            pace_test_predictions,
                            width,
                        ),
                        4,
                    ),
                    "mean_interval_width_ms": round(2 * width, 3),
                }
                for key, width in pace_widths.items()
            },
            "test_diagnostics": _stratified_pace(test, pace_test_predictions),
        },
        "tyre_degradation": {
            "target": "next_lap_time_minus_recent_clean_pace_ms",
            "selected": tyre_name,
            "nonlinear": tyre_is_nonlinear,
            "model": None if tyre_model is None else ridge_to_dict(tyre_model),
            "interval_half_width_ms": tyre_widths,
            "validation_candidates": {
                name: _metric_dict(metrics) for name, metrics in tyre_validation_metrics.items()
            },
            "test_metrics": _metric_dict(
                regression_metrics(
                    [row.target_pace_residual_ms for row in test], tyre_test_predictions
                )
            ),
            "test_baseline_metrics": _metric_dict(
                regression_metrics(
                    [row.target_pace_residual_ms for row in test],
                    _tyre_predictions(test, None, False),
                )
            ),
            "test_calibration": {
                key: {
                    "nominal_coverage": float(key) / 100,
                    "empirical_coverage": round(
                        _coverage(
                            [row.target_pace_residual_ms for row in test],
                            tyre_test_predictions,
                            width,
                        ),
                        4,
                    ),
                    "mean_interval_width_ms": round(2 * width, 3),
                }
                for key, width in tyre_widths.items()
            },
        },
        "pit_loss": {
            "target": "dry_green_flag_in_lap_plus_out_lap_minus_two_recent_clean_laps_ms",
            "selected": pit_name,
            "global_median_ms": global_median,
            "circuit_medians_ms": selected_circuits,
            "supported_circuits": supported_circuits,
            "interval_half_width_ms": pit_widths,
            "stationary_duration": "unavailable_not_observed",
            "validation_candidates": {
                name: _metric_dict(metrics) for name, metrics in pit_validation_metrics.items()
            },
            "test_metrics": _metric_dict(
                regression_metrics(
                    [row.target_effective_loss_ms for row in pit_test], pit_test_predictions
                )
            ),
            "test_baseline_metrics": _metric_dict(
                regression_metrics(
                    [row.target_effective_loss_ms for row in pit_test],
                    _pit_predict(pit_test, global_median, {}),
                )
            ),
            "test_calibration": {
                key: {
                    "nominal_coverage": float(key) / 100,
                    "empirical_coverage": round(
                        _coverage(
                            [row.target_effective_loss_ms for row in pit_test],
                            pit_test_predictions,
                            width,
                        ),
                        4,
                    ),
                    "mean_interval_width_ms": round(2 * width, 3),
                }
                for key, width in pit_widths.items()
            },
        },
    }


__all__ = ["train_bundle"]
