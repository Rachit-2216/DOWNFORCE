"""Canonical record codecs and deterministic Parquet serialization."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.domain.enums import DriverStatus, TrackStatus, TyreCompound
from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.domain.models import (
    DriverClassificationRecord,
    DriverRecord,
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
    SourceProvenance,
    StintRecord,
    TelemetryIndexRecord,
    WeatherRecord,
)
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.normalization.models import NormalizedSession
from downforce_core.storage.schemas import (
    CANONICAL_SCHEMAS,
    PROVENANCE_TYPE,
    TIME_COLUMNS,
    CanonicalTableName,
)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def _provenance_to_dict(value: SourceProvenance) -> dict[str, object]:
    return {
        "provider": value.provider,
        "provider_version": value.provider_version,
        "source": value.source,
        "retrieved_at": value.retrieved_at,
        "source_record_id": value.source_record_id,
        "source_published_at": value.source_published_at,
    }


def _record_to_dict(record: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for record_field in fields(record):  # type: ignore[arg-type]
        value = getattr(record, record_field.name)
        result[record_field.name] = (
            _provenance_to_dict(value) if isinstance(value, SourceProvenance) else _value(value)
        )
    return result


def _constant_array(value: object, size: int, data_type: pa.DataType) -> pa.Array:
    return pa.repeat(pa.scalar(value, type=data_type), size)


def _track_positions_table(session: NormalizedSession) -> pa.Table:
    dense = session.track_positions.table
    size = dense.num_rows
    provenance = session.track_positions
    provenance_array = pa.StructArray.from_arrays(
        [
            _constant_array(provenance.provider_name, size, pa.string()),
            _constant_array(provenance.provider_version, size, pa.string()),
            _constant_array(provenance.source, size, pa.string()),
            _constant_array(provenance.retrieved_at, size, pa.timestamp("us", tz="UTC")),
            pa.nulls(size, type=pa.string()),
            pa.nulls(size, type=pa.timestamp("us", tz="UTC")),
        ],
        fields=list(PROVENANCE_TYPE),
    )
    return pa.Table.from_arrays(
        [
            _constant_array(str(session.metadata.session_id), size, pa.string()),
            pc.cast(dense.column("driver_id"), pa.string()).combine_chunks(),
            dense.column("session_time_ms").combine_chunks(),
            dense.column("x_m").combine_chunks(),
            dense.column("y_m").combine_chunks(),
            dense.column("z_m").combine_chunks(),
            pc.cast(dense.column("raw_status"), pa.string()).combine_chunks(),
            provenance_array,
        ],
        schema=CANONICAL_SCHEMAS[CanonicalTableName.TRACK_POSITIONS],
    )


def canonical_tables(session: NormalizedSession) -> dict[CanonicalTableName, pa.Table]:
    """Convert canonical records to exact, versioned Arrow schemas."""

    records: dict[CanonicalTableName, tuple[object, ...]] = {
        CanonicalTableName.DRIVERS: cast(tuple[object, ...], session.drivers),
        CanonicalTableName.DRIVER_CLASSIFICATIONS: cast(
            tuple[object, ...], session.classifications
        ),
        CanonicalTableName.LAPS: cast(tuple[object, ...], session.laps),
        CanonicalTableName.STINTS: cast(tuple[object, ...], session.stints),
        CanonicalTableName.PIT_STOPS: cast(tuple[object, ...], session.pit_stops),
        CanonicalTableName.WEATHER: cast(tuple[object, ...], session.weather),
        CanonicalTableName.RACE_CONTROL: cast(tuple[object, ...], session.race_control),
        CanonicalTableName.RACE_POSITIONS: cast(tuple[object, ...], session.race_positions),
        CanonicalTableName.TELEMETRY_INDEX: cast(tuple[object, ...], session.telemetry_index),
    }
    result = {
        name: pa.Table.from_pylist(
            [_record_to_dict(record) for record in values],
            schema=CANONICAL_SCHEMAS[name],
        )
        for name, values in records.items()
    }
    result[CanonicalTableName.TRACK_POSITIONS] = _track_positions_table(session)
    return result


def write_parquet(path: Path, table_name: CanonicalTableName, table: pa.Table) -> None:
    expected = CANONICAL_SCHEMAS[table_name]
    if not table.schema.equals(expected, check_metadata=False):
        raise StorageIntegrityError(f"canonical table schema is invalid: {table_name.value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table.replace_schema_metadata(None),
        path,
        compression="zstd",
        compression_level=9,
        use_dictionary=True,
        write_statistics=True,
        version="2.6",
        data_page_version="2.0",
        use_compliant_nested_type=True,
        store_schema=True,
    )


def read_parquet(path: Path, table_name: CanonicalTableName) -> pa.Table:
    try:
        table = pq.read_table(path)
    except (OSError, pa.ArrowException) as exc:
        raise StorageIntegrityError(f"canonical Parquet is unreadable: {table_name.value}") from exc
    expected = CANONICAL_SCHEMAS[table_name]
    if not table.schema.equals(expected, check_metadata=False):
        raise StorageIntegrityError(f"canonical Parquet schema mismatch: {table_name.value}")
    return table


def table_time_range(
    table_name: CanonicalTableName, table: pa.Table
) -> tuple[int | None, int | None]:
    values: list[int] = []
    for column_name in TIME_COLUMNS.get(table_name, ()):
        result = pc.min_max(table.column(column_name)).as_py()
        if result is None:
            continue
        if result["min"] is not None:
            values.append(int(result["min"]))
        if result["max"] is not None:
            values.append(int(result["max"]))
    return (min(values), max(values)) if values else (None, None)


def _provenance(value: object) -> SourceProvenance:
    if not isinstance(value, dict):
        raise StorageIntegrityError("canonical provenance is malformed")
    retrieved = value.get("retrieved_at")
    published = value.get("source_published_at")
    if not isinstance(retrieved, datetime):
        raise StorageIntegrityError("canonical retrieval time is malformed")
    return SourceProvenance(
        provider=str(value.get("provider")),
        provider_version=str(value.get("provider_version")),
        source=str(value.get("source")),
        retrieved_at=retrieved,
        source_record_id=cast(str | None, value.get("source_record_id")),
        source_published_at=cast(datetime | None, published),
    )


def _session_id(row: dict[str, object]) -> SessionId:
    return SessionId(str(row["session_id"]))


def _driver_id(row: dict[str, object], name: str = "driver_id") -> DriverId:
    return DriverId(str(row[name]))


def decode_records(table_name: CanonicalTableName, table: pa.Table) -> tuple[object, ...]:
    """Decode low-cardinality canonical Parquet rows into immutable domain records."""

    rows = cast(list[dict[str, object]], table.to_pylist())
    decoded: list[object] = []
    for row in rows:
        provenance = _provenance(row["provenance"])
        session_id = _session_id(row)
        if table_name is CanonicalTableName.DRIVERS:
            decoded.append(
                DriverRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    racing_number=cast(int | None, row["racing_number"]),
                    abbreviation=cast(str | None, row["abbreviation"]),
                    full_name=cast(str | None, row["full_name"]),
                    team_name=cast(str | None, row["team_name"]),
                    country_code=cast(str | None, row["country_code"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.DRIVER_CLASSIFICATIONS:
            decoded.append(
                DriverClassificationRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    classified_position=cast(int | None, row["classified_position"]),
                    status=DriverStatus(str(row["status"])),
                    points=cast(float | None, row["points"]),
                    raw_status=cast(str | None, row["raw_status"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.LAPS:
            decoded.append(
                LapRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    lap_number=cast(int, row["lap_number"]),
                    lap_start_time_ms=cast(int | None, row["lap_start_time_ms"]),
                    lap_end_time_ms=cast(int | None, row["lap_end_time_ms"]),
                    lap_time_ms=cast(int | None, row["lap_time_ms"]),
                    sector_1_time_ms=cast(int | None, row["sector_1_time_ms"]),
                    sector_2_time_ms=cast(int | None, row["sector_2_time_ms"]),
                    sector_3_time_ms=cast(int | None, row["sector_3_time_ms"]),
                    stint_number=cast(int | None, row["stint_number"]),
                    compound=TyreCompound(str(row["compound"])),
                    raw_compound=cast(str | None, row["raw_compound"]),
                    tyre_life_laps=cast(float | None, row["tyre_life_laps"]),
                    is_personal_best=cast(bool | None, row["is_personal_best"]),
                    is_accurate=cast(bool | None, row["is_accurate"]),
                    is_generated=cast(bool | None, row["is_generated"]),
                    is_deleted=cast(bool | None, row["is_deleted"]),
                    deleted_reason=cast(str | None, row["deleted_reason"]),
                    raw_track_status=cast(str | None, row["raw_track_status"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.STINTS:
            decoded.append(
                StintRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    stint_number=cast(int, row["stint_number"]),
                    start_lap=cast(int, row["start_lap"]),
                    end_lap=cast(int | None, row["end_lap"]),
                    start_time_ms=cast(int | None, row["start_time_ms"]),
                    end_time_ms=cast(int | None, row["end_time_ms"]),
                    compound=TyreCompound(str(row["compound"])),
                    raw_compound=cast(str | None, row["raw_compound"]),
                    tyre_life_start_laps=cast(float | None, row["tyre_life_start_laps"]),
                    tyre_life_end_laps=cast(float | None, row["tyre_life_end_laps"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.PIT_STOPS:
            decoded.append(
                PitStopRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    stop_number=cast(int, row["stop_number"]),
                    lap_number=cast(int | None, row["lap_number"]),
                    pit_in_time_ms=cast(int | None, row["pit_in_time_ms"]),
                    pit_out_time_ms=cast(int | None, row["pit_out_time_ms"]),
                    pit_lane_duration_ms=cast(int | None, row["pit_lane_duration_ms"]),
                    stationary_duration_ms=cast(int | None, row["stationary_duration_ms"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.WEATHER:
            decoded.append(
                WeatherRecord(
                    session_id=session_id,
                    session_time_ms=cast(int, row["session_time_ms"]),
                    air_temperature_c=cast(float | None, row["air_temperature_c"]),
                    track_temperature_c=cast(float | None, row["track_temperature_c"]),
                    humidity_percent=cast(float | None, row["humidity_percent"]),
                    pressure_hpa=cast(float | None, row["pressure_hpa"]),
                    rainfall=cast(bool | None, row["rainfall"]),
                    wind_speed_mps=cast(float | None, row["wind_speed_mps"]),
                    wind_direction_deg=cast(float | None, row["wind_direction_deg"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.RACE_CONTROL:
            driver = row["driver_id"]
            decoded.append(
                RaceControlRecord(
                    session_id=session_id,
                    session_time_ms=cast(int, row["session_time_ms"]),
                    message=cast(str, row["message"]),
                    track_status=TrackStatus(str(row["track_status"])),
                    raw_status=cast(str | None, row["raw_status"]),
                    category=cast(str | None, row["category"]),
                    scope=cast(str | None, row["scope"]),
                    source_kind=cast(str | None, row["source_kind"]),
                    lap_number=cast(int | None, row["lap_number"]),
                    driver_id=None if driver is None else DriverId(str(driver)),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.RACE_POSITIONS:
            decoded.append(
                RacePositionRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    session_time_ms=cast(int, row["session_time_ms"]),
                    position=cast(int, row["position"]),
                    lap_number=cast(int | None, row["lap_number"]),
                    provenance=provenance,
                )
            )
        elif table_name is CanonicalTableName.TELEMETRY_INDEX:
            decoded.append(
                TelemetryIndexRecord(
                    session_id=session_id,
                    driver_id=_driver_id(row),
                    start_time_ms=cast(int, row["start_time_ms"]),
                    end_time_ms=cast(int, row["end_time_ms"]),
                    data_key=cast(str, row["data_key"]),
                    channel_names=tuple(cast(list[str], row["channel_names"])),
                    sample_count=cast(int, row["sample_count"]),
                    lap_number=cast(int | None, row["lap_number"]),
                    provenance=provenance,
                )
            )
        else:
            raise TypeError("dense track positions are decoded through their Arrow wrapper")
    return tuple(decoded)


__all__ = [
    "canonical_tables",
    "decode_records",
    "file_sha256",
    "read_parquet",
    "table_time_range",
    "write_parquet",
]
