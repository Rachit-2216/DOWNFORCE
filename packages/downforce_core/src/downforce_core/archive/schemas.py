"""Versioned Arrow schemas for the broad historical archive."""

from __future__ import annotations

from enum import StrEnum

import pyarrow as pa  # type: ignore[import-untyped]

ARCHIVE_SCHEMA_VERSION = "1.1.0"
CATALOG_VERSION = "1.0.0"


class ArchiveTableName(StrEnum):
    RESULTS = "results"
    LAPS = "laps"
    PIT_STOPS = "pit_stops"


ARCHIVE_SCHEMAS: dict[ArchiveTableName, pa.Schema] = {
    ArchiveTableName.RESULTS: pa.schema(
        [
            pa.field("session_id", pa.string(), nullable=False),
            pa.field("driver_id", pa.string(), nullable=False),
            pa.field("driver_code", pa.string()),
            pa.field("driver_name", pa.string(), nullable=False),
            pa.field("team_id", pa.string()),
            pa.field("team_name", pa.string()),
            pa.field("car_number", pa.int32()),
            pa.field("grid_position", pa.int32()),
            pa.field("finish_position", pa.int32()),
            pa.field("points", pa.float64()),
            pa.field("laps_completed", pa.int32()),
            pa.field("status", pa.string()),
            pa.field("classified", pa.bool_()),
            pa.field("total_time_ms", pa.int64()),
        ]
    ),
    ArchiveTableName.LAPS: pa.schema(
        [
            pa.field("session_id", pa.string(), nullable=False),
            pa.field("driver_id", pa.string(), nullable=False),
            pa.field("lap_number", pa.int32(), nullable=False),
            pa.field("position", pa.int32()),
            pa.field("lap_time_ms", pa.int64()),
            pa.field("average_speed_kph", pa.float64()),
            pa.field("is_fastest_lap", pa.bool_()),
        ]
    ),
    ArchiveTableName.PIT_STOPS: pa.schema(
        [
            pa.field("session_id", pa.string(), nullable=False),
            pa.field("driver_id", pa.string(), nullable=False),
            pa.field("stop_number", pa.int32(), nullable=False),
            pa.field("lap_number", pa.int32()),
            pa.field("duration_ms", pa.int64()),
            pa.field("local_time", pa.string()),
        ]
    ),
}


__all__ = ["ARCHIVE_SCHEMAS", "ARCHIVE_SCHEMA_VERSION", "CATALOG_VERSION", "ArchiveTableName"]
