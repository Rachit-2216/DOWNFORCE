"""Versioned, content-verified storage for derived analytics observations."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.analytics.contracts import (
    ANALYTICS_VERSION,
    DriverRaceObservation,
)
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256

DRIVER_RACE_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("session_id", pa.string(), nullable=False),
        pa.field("season", pa.int32(), nullable=False),
        pa.field("round_number", pa.int32(), nullable=False),
        pa.field("event_name", pa.string(), nullable=False),
        pa.field("event_date", pa.string(), nullable=False),
        pa.field("circuit_id", pa.string(), nullable=False),
        pa.field("circuit_name", pa.string(), nullable=False),
        pa.field("driver_id", pa.string(), nullable=False),
        pa.field("driver_name", pa.string(), nullable=False),
        pa.field("constructor_id", pa.string()),
        pa.field("constructor_name", pa.string()),
        pa.field("grid_position", pa.int32()),
        pa.field("finish_position", pa.int32()),
        pa.field("points", pa.float64(), nullable=False),
        pa.field("laps_completed", pa.int32()),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("classified", pa.bool_(), nullable=False),
        pa.field("positions_gained", pa.int32()),
        pa.field("recorded_lap_count", pa.int32(), nullable=False),
        pa.field("timed_lap_count", pa.int32(), nullable=False),
        pa.field("raw_mean_lap_ms", pa.float64()),
        pa.field("raw_median_lap_ms", pa.float64()),
        pa.field("best_recorded_lap_ms", pa.int64()),
        pa.field("fastest_lap_recorded", pa.bool_(), nullable=False),
        pa.field("pit_stop_count", pa.int32()),
        pa.field("median_pit_duration_ms", pa.float64()),
        pa.field("pit_durations_ms", pa.list_(pa.int64()), nullable=False),
        pa.field("lap_data_available", pa.bool_(), nullable=False),
        pa.field("pit_data_available", pa.bool_(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True, slots=True)
class StoredAnalyticsSnapshot:
    observations: tuple[DriverRaceObservation, ...]
    digest: str
    built_at_utc: str
    provider_circuit_identities: int


class AnalyticsDerivedStore:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve() / ".downforce" / "analytics"
        self.manifest_path = self.root / "manifest.json"

    def load(self, source_revision: str) -> StoredAnalyticsSnapshot | None:
        try:
            manifest_value = json.loads(self.manifest_path.read_text("utf-8"))
            if not isinstance(manifest_value, dict):
                return None
            manifest = cast(dict[str, object], manifest_value)
            if manifest.get("analytics_version") != ANALYTICS_VERSION:
                return None
            if manifest.get("archive_source_revision") != source_revision:
                return None
            relative_file = str(manifest["file"])
            if relative_file != "driver-race.parquet":
                return None
            path = self.root / relative_file
            ensure_contained(path, self.root, must_exist=True)
            if path.stat().st_size != int(cast(int, manifest["bytes"])):
                return None
            if file_sha256(path) != str(manifest["sha256"]):
                return None
            table = pq.read_table(path)
            if not table.schema.equals(DRIVER_RACE_SCHEMA, check_metadata=False):
                return None
            if table.num_rows != int(cast(int, manifest["observation_rows"])):
                return None
            observations = tuple(
                DriverRaceObservation.from_dict(row)
                for row in cast(list[dict[str, object]], table.to_pylist())
            )
            return StoredAnalyticsSnapshot(
                observations=observations,
                digest=str(manifest["snapshot_digest"]),
                built_at_utc=str(manifest["built_at_utc"]),
                provider_circuit_identities=int(cast(int, manifest["provider_circuit_identities"])),
            )
        except (KeyError, OSError, TypeError, ValueError, pa.ArrowException):
            return None

    def publish(
        self,
        observations: tuple[DriverRaceObservation, ...],
        *,
        source_revision: str,
        snapshot_digest: str,
        built_at_utc: str,
        provider_circuit_identities: int,
    ) -> dict[str, object]:
        staging = self.root.with_name(f".{self.root.name}.{uuid4().hex}.tmp")
        staging.mkdir(parents=True, exist_ok=False)
        try:
            path = staging / "driver-race.parquet"
            table = pa.Table.from_pylist(
                [item.to_dict() for item in observations], schema=DRIVER_RACE_SCHEMA
            )
            pq.write_table(
                table,
                path,
                compression="zstd",
                compression_level=9,
                use_dictionary=True,
                write_statistics=True,
                version="2.6",
            )
            manifest = {
                "analytics_version": ANALYTICS_VERSION,
                "archive_source_revision": source_revision,
                "snapshot_digest": snapshot_digest,
                "built_at_utc": built_at_utc,
                "provider_circuit_identities": provider_circuit_identities,
                "observation_rows": len(observations),
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
            (staging / "manifest.json").write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            if self.root.exists():
                quarantine = self.root.with_name(f".{self.root.name}.{uuid4().hex}.old")
                os.replace(self.root, quarantine)
                try:
                    os.replace(staging, self.root)
                except Exception:
                    if not self.root.exists():
                        os.replace(quarantine, self.root)
                    raise
                shutil.rmtree(quarantine, ignore_errors=True)
            else:
                os.replace(staging, self.root)
            return manifest
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise


__all__ = [
    "DRIVER_RACE_SCHEMA",
    "AnalyticsDerivedStore",
    "StoredAnalyticsSnapshot",
]
