"""Reproducible canonical ML dataset assembly with session-level splits."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from downforce_core.domain.models import LapRecord
from downforce_core.ml.contracts import (
    DRY_COMPOUNDS,
    ML_SCHEMA_VERSION,
    DatasetSplit,
    raw_track_status_is_clear,
)
from downforce_core.ml.features import CanonicalFeatureBuilder
from downforce_core.storage import DownforceRepository


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    session_id: str
    split: DatasetSplit


@dataclass(frozen=True, slots=True)
class PaceExample:
    session_id: str
    dataset_id: str
    driver_id: str
    cutoff_time_ms: int
    boundary_lap: int
    target_lap: int
    target_time_ms: int
    compound: str
    split: str
    features: tuple[float, ...]
    nonlinear_features: tuple[float, ...]
    target_lap_time_ms: float
    target_pace_residual_ms: float


@dataclass(frozen=True, slots=True)
class PitLossExample:
    session_id: str
    dataset_id: str
    driver_id: str
    cutoff_time_ms: int
    pit_lap: int
    target_time_ms: int
    split: str
    circuit_name: str
    race_control_regime: str
    weather_regime: str
    pit_lane_duration_ms: float
    target_effective_loss_ms: float


@dataclass(frozen=True, slots=True)
class MLDataset:
    pace: tuple[PaceExample, ...]
    pit_loss: tuple[PitLossExample, ...]
    source_datasets: tuple[tuple[str, str], ...]
    row_rejections: tuple[tuple[str, int], ...]
    digest: str

    def split_pace(self, split: DatasetSplit) -> tuple[PaceExample, ...]:
        return tuple(row for row in self.pace if row.split == split.value)

    def split_pit_loss(self, split: DatasetSplit) -> tuple[PitLossExample, ...]:
        return tuple(row for row in self.pit_loss if row.split == split.value)


def load_corpus(path: Path) -> tuple[CorpusEntry, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("ml_schema_version") != ML_SCHEMA_VERSION:
        raise ValueError("ML corpus schema version is missing or incompatible")
    sessions = raw.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("ML corpus sessions must be a list")
    entries: list[CorpusEntry] = []
    for item in sessions:
        if not isinstance(item, dict):
            raise ValueError("ML corpus session entry must be an object")
        session_id = item.get("session_id")
        split = item.get("split")
        if not isinstance(session_id, str) or not isinstance(split, str):
            raise ValueError("ML corpus session_id and split must be strings")
        entries.append(CorpusEntry(session_id, DatasetSplit(split)))
    if len({entry.session_id for entry in entries}) != len(entries):
        raise ValueError("ML corpus must not repeat sessions")
    if {entry.split for entry in entries} != set(DatasetSplit):
        raise ValueError("ML corpus must contain train, calibration, and test sessions")
    return tuple(entries)


def _next_lap(
    builder: CanonicalFeatureBuilder, driver_id: str, lap_number: int
) -> LapRecord | None:
    return next(
        (lap for lap in builder.laps_for(driver_id) if lap.lap_number == lap_number + 1),
        None,
    )


def _pace_rows(
    builder: CanonicalFeatureBuilder,
    dataset_id: str,
    split: DatasetSplit,
    rejections: Counter[str],
) -> list[PaceExample]:
    rows: list[PaceExample] = []
    for driver in builder.drivers:
        driver_id = str(driver.driver_id)
        for lap in builder.laps_for(driver_id):
            result = builder.feature_for_lap(driver_id, lap.lap_number)
            if result.feature is None:
                rejections[f"pace:{result.eligibility.reason or 'not_eligible'}"] += 1
                continue
            following = _next_lap(builder, driver_id, lap.lap_number)
            if (
                following is None
                or following.lap_time_ms is None
                or following.lap_end_time_ms is None
            ):
                rejections["pace:missing_target"] += 1
                continue
            following_eligibility = builder.eligibility(driver_id, following)
            if not following_eligibility.eligible:
                rejections[f"pace:target:{following_eligibility.reason or 'not_eligible'}"] += 1
                continue
            feature = result.feature
            rows.append(
                PaceExample(
                    session_id=feature.session_id,
                    dataset_id=dataset_id,
                    driver_id=driver_id,
                    cutoff_time_ms=feature.cutoff_time_ms,
                    boundary_lap=feature.boundary_lap,
                    target_lap=following.lap_number,
                    target_time_ms=following.lap_end_time_ms,
                    compound=feature.compound,
                    split=split.value,
                    features=feature.values,
                    nonlinear_features=feature.nonlinear_values(),
                    target_lap_time_ms=float(following.lap_time_ms),
                    target_pace_residual_ms=float(following.lap_time_ms) - feature.values[6],
                )
            )
    return rows


def _pit_rows(
    builder: CanonicalFeatureBuilder,
    dataset_id: str,
    split: DatasetSplit,
    rejections: Counter[str],
) -> list[PitLossExample]:
    rows: list[PitLossExample] = []
    session = builder.session
    for pit in session.pit_stops:
        driver_id = str(pit.driver_id)
        if (
            pit.lap_number is None
            or pit.pit_in_time_ms is None
            or pit.pit_out_time_ms is None
            or pit.pit_lane_duration_ms is None
        ):
            rejections["pit:incomplete_record"] += 1
            continue
        by_number = {lap.lap_number: lap for lap in builder.laps_for(driver_id)}
        in_lap = by_number.get(pit.lap_number)
        out_lap = by_number.get(pit.lap_number + 1)
        if (
            in_lap is None
            or out_lap is None
            or in_lap.lap_time_ms is None
            or out_lap.lap_time_ms is None
        ):
            rejections["pit:missing_cycle_timing"] += 1
            continue
        if (
            in_lap.lap_start_time_ms is None
            or in_lap.lap_end_time_ms is None
            or out_lap.lap_start_time_ms is None
            or out_lap.lap_end_time_ms is None
            or not 50_000 <= in_lap.lap_time_ms <= 300_000
            or not 50_000 <= out_lap.lap_time_ms <= 300_000
            or in_lap.is_deleted is True
            or in_lap.is_generated is True
            or out_lap.is_deleted is True
            or out_lap.is_generated is True
        ):
            rejections["pit:invalid_cycle_timing"] += 1
            continue
        if builder.is_restart_lap(driver_id, in_lap) or builder.is_restart_lap(driver_id, out_lap):
            rejections["pit:red_flag_restart_cycle"] += 1
            continue
        if not (
            raw_track_status_is_clear(in_lap.raw_track_status)
            and raw_track_status_is_clear(out_lap.raw_track_status)
        ):
            rejections["pit:neutralized_or_unknown_track"] += 1
            continue
        if not (builder.lap_weather_is_dry(in_lap) and builder.lap_weather_is_dry(out_lap)):
            rejections["pit:wet_or_unknown_weather"] += 1
            continue
        if in_lap.compound not in DRY_COMPOUNDS or out_lap.compound not in DRY_COMPOUNDS:
            rejections["pit:unsupported_compound"] += 1
            continue
        clean_before = [
            lap
            for lap in builder.laps_for(driver_id)
            if lap.lap_number < pit.lap_number
            and lap.lap_time_ms is not None
            and builder.eligibility(driver_id, lap).eligible
        ][-3:]
        if len(clean_before) < 2:
            rejections["pit:insufficient_clean_history"] += 1
            continue
        clean_times = [lap.lap_time_ms for lap in clean_before]
        if any(value is None for value in clean_times):
            rejections["pit:invalid_reference_timing"] += 1
            continue
        expected_cycle = 2.0 * statistics.median(cast(list[int], clean_times))
        actual_cycle = float(in_lap.lap_time_ms + out_lap.lap_time_ms)
        effective_loss = actual_cycle - expected_cycle
        if not 1_000.0 <= effective_loss <= 120_000.0:
            rejections["pit:implausible_effective_loss"] += 1
            continue
        cutoff = clean_before[-1].lap_end_time_ms
        if cutoff is None or pit.pit_in_time_ms <= cutoff:
            rejections["pit:noncausal_cutoff"] += 1
            continue
        rows.append(
            PitLossExample(
                session_id=str(session.metadata.session_id),
                dataset_id=dataset_id,
                driver_id=driver_id,
                cutoff_time_ms=cutoff,
                pit_lap=pit.lap_number,
                target_time_ms=pit.pit_out_time_ms,
                split=split.value,
                circuit_name=session.metadata.circuit_name or "unknown",
                race_control_regime="green",
                weather_regime="dry",
                pit_lane_duration_ms=float(pit.pit_lane_duration_ms),
                target_effective_loss_ms=effective_loss,
            )
        )
    return rows


def build_dataset(repository: DownforceRepository, corpus: tuple[CorpusEntry, ...]) -> MLDataset:
    pace: list[PaceExample] = []
    pit_loss: list[PitLossExample] = []
    sources: list[tuple[str, str]] = []
    rejections: Counter[str] = Counter()
    for entry in corpus:
        manifest = repository.load_manifest(entry.session_id)
        session = repository.load_session(entry.session_id, include_track_positions=False)
        builder = CanonicalFeatureBuilder(session)
        sources.append((manifest.session_id, manifest.dataset_id))
        pace.extend(_pace_rows(builder, manifest.dataset_id, entry.split, rejections))
        pit_loss.extend(_pit_rows(builder, manifest.dataset_id, entry.split, rejections))
    row_rejections = tuple(sorted(rejections.items()))
    payload = {
        "ml_schema_version": ML_SCHEMA_VERSION,
        "sources": sorted(sources),
        "pace": [asdict(row) for row in pace],
        "pit_loss": [asdict(row) for row in pit_loss],
        "row_rejections": dict(row_rejections),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MLDataset(
        tuple(pace),
        tuple(pit_loss),
        tuple(sorted(sources)),
        row_rejections,
        digest,
    )


def write_dataset(dataset: MLDataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ml_schema_version": ML_SCHEMA_VERSION,
        "digest": dataset.digest,
        "source_datasets": dataset.source_datasets,
        "pace": [asdict(row) for row in dataset.pace],
        "pit_loss": [asdict(row) for row in dataset.pit_loss],
        "row_rejections": dict(dataset.row_rejections),
    }
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


__all__ = [
    "CorpusEntry",
    "MLDataset",
    "PaceExample",
    "PitLossExample",
    "build_dataset",
    "load_corpus",
    "write_dataset",
]
