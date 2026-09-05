"""Explicit canonical Arrow schemas and stable Parquet/IPC helpers."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.providers.base import DatasetName


class CanonicalTableName(StrEnum):
    DRIVERS = "drivers"
    DRIVER_CLASSIFICATIONS = "driver_classifications"
    LAPS = "laps"
    STINTS = "stints"
    PIT_STOPS = "pit_stops"
    WEATHER = "weather"
    RACE_CONTROL = "race_control"
    RACE_POSITIONS = "race_positions"
    TRACK_POSITIONS = "track_positions"
    TELEMETRY_INDEX = "telemetry_index"
    EVENTS = "events"


PROVENANCE_TYPE = pa.struct(
    [
        pa.field("provider", pa.string(), nullable=False),
        pa.field("provider_version", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("retrieved_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source_record_id", pa.string()),
        pa.field("source_published_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _field(name: str, data_type: pa.DataType, *, nullable: bool = True) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


def _required_id(name: str) -> pa.Field:
    return _field(name, pa.string(), nullable=False)


_PROVENANCE = _field("provenance", PROVENANCE_TYPE, nullable=False)


CANONICAL_SCHEMAS: dict[CanonicalTableName, pa.Schema] = {
    CanonicalTableName.DRIVERS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("racing_number", pa.int32()),
            _field("abbreviation", pa.string()),
            _field("full_name", pa.string()),
            _field("team_name", pa.string()),
            _field("country_code", pa.string()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.DRIVER_CLASSIFICATIONS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("classified_position", pa.int32()),
            _field("status", pa.string(), nullable=False),
            _field("points", pa.float64()),
            _field("raw_status", pa.string()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.LAPS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("lap_number", pa.int32(), nullable=False),
            *[
                _field(name, pa.int64())
                for name in (
                    "lap_start_time_ms",
                    "lap_end_time_ms",
                    "lap_time_ms",
                    "sector_1_time_ms",
                    "sector_2_time_ms",
                    "sector_3_time_ms",
                )
            ],
            _field("stint_number", pa.int32()),
            _field("compound", pa.string(), nullable=False),
            _field("raw_compound", pa.string()),
            _field("tyre_life_laps", pa.float64()),
            _field("is_personal_best", pa.bool_()),
            _field("is_accurate", pa.bool_()),
            _field("is_generated", pa.bool_()),
            _field("is_deleted", pa.bool_()),
            _field("deleted_reason", pa.string()),
            _field("raw_track_status", pa.string()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.STINTS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("stint_number", pa.int32(), nullable=False),
            _field("start_lap", pa.int32(), nullable=False),
            _field("end_lap", pa.int32()),
            _field("start_time_ms", pa.int64()),
            _field("end_time_ms", pa.int64()),
            _field("compound", pa.string(), nullable=False),
            _field("raw_compound", pa.string()),
            _field("tyre_life_start_laps", pa.float64()),
            _field("tyre_life_end_laps", pa.float64()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.PIT_STOPS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("stop_number", pa.int32(), nullable=False),
            _field("lap_number", pa.int32()),
            *[
                _field(name, pa.int64())
                for name in (
                    "pit_in_time_ms",
                    "pit_out_time_ms",
                    "pit_lane_duration_ms",
                    "stationary_duration_ms",
                )
            ],
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.WEATHER: pa.schema(
        [
            _required_id("session_id"),
            _field("session_time_ms", pa.int64(), nullable=False),
            _field("air_temperature_c", pa.float64()),
            _field("track_temperature_c", pa.float64()),
            _field("humidity_percent", pa.float64()),
            _field("pressure_hpa", pa.float64()),
            _field("rainfall", pa.bool_()),
            _field("wind_speed_mps", pa.float64()),
            _field("wind_direction_deg", pa.float64()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.RACE_CONTROL: pa.schema(
        [
            _required_id("session_id"),
            _field("session_time_ms", pa.int64(), nullable=False),
            _field("message", pa.string(), nullable=False),
            _field("track_status", pa.string(), nullable=False),
            _field("raw_status", pa.string()),
            _field("category", pa.string()),
            _field("scope", pa.string()),
            _field("source_kind", pa.string()),
            _field("lap_number", pa.int32()),
            _field("driver_id", pa.string()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.RACE_POSITIONS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("session_time_ms", pa.int64(), nullable=False),
            _field("position", pa.int32(), nullable=False),
            _field("lap_number", pa.int32()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.TRACK_POSITIONS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("session_time_ms", pa.int64(), nullable=False),
            _field("x_m", pa.float64(), nullable=False),
            _field("y_m", pa.float64(), nullable=False),
            _field("z_m", pa.float64()),
            _field("raw_status", pa.string()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.TELEMETRY_INDEX: pa.schema(
        [
            _required_id("session_id"),
            _required_id("driver_id"),
            _field("start_time_ms", pa.int64(), nullable=False),
            _field("end_time_ms", pa.int64(), nullable=False),
            _field("data_key", pa.string(), nullable=False),
            _field("channel_names", pa.list_(pa.string()), nullable=False),
            _field("sample_count", pa.int64(), nullable=False),
            _field("lap_number", pa.int32()),
            _PROVENANCE,
        ]
    ),
    CanonicalTableName.EVENTS: pa.schema(
        [
            _required_id("session_id"),
            _required_id("event_id"),
            _field("session_time_ms", pa.int64(), nullable=False),
            _field("priority", pa.int32(), nullable=False),
            _field("sequence", pa.int64(), nullable=False),
            _field("event_type", pa.string(), nullable=False),
            _field("driver_id", pa.string()),
            _field("source", pa.string(), nullable=False),
            _field("source_key", pa.string()),
            _field("payload_json", pa.string(), nullable=False),
            _field("normalization_version", pa.string(), nullable=False),
        ]
    ),
}


SOURCE_DATASET: dict[CanonicalTableName, str] = {
    CanonicalTableName.DRIVERS: DatasetName.DRIVERS.value,
    CanonicalTableName.DRIVER_CLASSIFICATIONS: DatasetName.DRIVERS.value,
    CanonicalTableName.LAPS: DatasetName.LAPS.value,
    CanonicalTableName.STINTS: DatasetName.LAPS.value,
    CanonicalTableName.PIT_STOPS: DatasetName.LAPS.value,
    CanonicalTableName.WEATHER: DatasetName.WEATHER.value,
    CanonicalTableName.RACE_CONTROL: DatasetName.RACE_CONTROL.value,
    CanonicalTableName.RACE_POSITIONS: DatasetName.RACE_POSITIONS.value,
    CanonicalTableName.TRACK_POSITIONS: DatasetName.TRACK_POSITIONS.value,
    CanonicalTableName.TELEMETRY_INDEX: DatasetName.CAR_TELEMETRY.value,
    CanonicalTableName.EVENTS: "replay-derived",
}


TIME_COLUMNS: dict[CanonicalTableName, tuple[str, ...]] = {
    CanonicalTableName.LAPS: ("lap_start_time_ms", "lap_end_time_ms"),
    CanonicalTableName.STINTS: ("start_time_ms", "end_time_ms"),
    CanonicalTableName.PIT_STOPS: ("pit_in_time_ms", "pit_out_time_ms"),
    CanonicalTableName.WEATHER: ("session_time_ms",),
    CanonicalTableName.RACE_CONTROL: ("session_time_ms",),
    CanonicalTableName.RACE_POSITIONS: ("session_time_ms",),
    CanonicalTableName.TRACK_POSITIONS: ("session_time_ms",),
    CanonicalTableName.TELEMETRY_INDEX: ("start_time_ms", "end_time_ms"),
    CanonicalTableName.EVENTS: ("session_time_ms",),
}


def schema_fingerprint(schema: pa.Schema) -> str:
    return sha256(schema.remove_metadata().serialize().to_pybytes()).hexdigest()


__all__ = [
    "CANONICAL_SCHEMAS",
    "PROVENANCE_TYPE",
    "SOURCE_DATASET",
    "TIME_COLUMNS",
    "CanonicalTableName",
    "schema_fingerprint",
]
