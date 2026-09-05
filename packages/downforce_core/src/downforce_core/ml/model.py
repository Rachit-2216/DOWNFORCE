"""Dependency-light deterministic baselines, ridge candidates, metrics, and calibration."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Metrics:
    mae_ms: float
    rmse_ms: float
    median_ae_ms: float
    count: int


@dataclass(frozen=True, slots=True)
class RidgeModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    regularization: float

    def predict(self, features: tuple[float, ...]) -> float:
        if len(features) != len(self.means):
            raise ValueError("feature vector does not match model schema")
        return self.intercept + sum(
            coefficient * ((value - mean) / scale)
            for value, mean, scale, coefficient in zip(
                features, self.means, self.scales, self.coefficients, strict=True
            )
        )


def _solve(matrix: list[list[float]], target: list[float]) -> list[float]:
    size = len(target)
    augmented = [matrix[row][:] + [target[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            augmented[pivot][column] = 1e-12
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [
                    current - factor * pivot_value
                    for current, pivot_value in zip(augmented[row], augmented[column], strict=True)
                ]
    return [augmented[row][-1] for row in range(size)]


def fit_ridge(
    features: list[tuple[float, ...]],
    targets: list[float],
    *,
    regularization: float,
) -> RidgeModel:
    if not features or len(features) != len(targets):
        raise ValueError("training features and targets must be nonempty and aligned")
    width = len(features[0])
    if width == 0 or any(len(row) != width for row in features):
        raise ValueError("training feature rows must have one stable nonempty schema")
    means = tuple(statistics.fmean(row[index] for row in features) for index in range(width))
    scales = tuple(
        max(1.0, statistics.pstdev(row[index] for row in features)) for index in range(width)
    )
    normalized = [
        tuple((value - means[index]) / scales[index] for index, value in enumerate(row))
        for row in features
    ]
    intercept = statistics.fmean(targets)
    centered = [target - intercept for target in targets]
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, target in zip(normalized, centered, strict=True):
        for left in range(width):
            rhs[left] += row[left] * target
            for right in range(width):
                gram[left][right] += row[left] * row[right]
    for index in range(width):
        gram[index][index] += regularization
    coefficients = tuple(_solve(gram, rhs))
    return RidgeModel(means, scales, coefficients, intercept, regularization)


def regression_metrics(actual: list[float], predicted: list[float]) -> Metrics:
    if not actual or len(actual) != len(predicted):
        raise ValueError("metric vectors must be nonempty and aligned")
    errors = [
        abs(expected - estimate) for expected, estimate in zip(actual, predicted, strict=True)
    ]
    squared = [
        (expected - estimate) ** 2 for expected, estimate in zip(actual, predicted, strict=True)
    ]
    return Metrics(
        mae_ms=statistics.fmean(errors),
        rmse_ms=math.sqrt(statistics.fmean(squared)),
        median_ae_ms=statistics.median(errors),
        count=len(actual),
    )


def conformal_quantile(residuals: list[float], coverage: float) -> float:
    if not residuals or not 0.0 < coverage < 1.0:
        raise ValueError("conformal calibration requires residuals and a valid coverage")
    ordered = sorted(abs(value) for value in residuals)
    rank = min(len(ordered), math.ceil((len(ordered) + 1) * coverage))
    return ordered[rank - 1]


__all__ = ["Metrics", "RidgeModel", "conformal_quantile", "fit_ridge", "regression_metrics"]
