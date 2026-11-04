"""Lossless field naming and explicit derived quality for native provider tables."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]

ALIASES = {
    "SessionTime": "session_time",
    "Date": "timestamp",
    "Time": "provider_time",
    "Speed": "speed",
    "RPM": "rpm",
    "nGear": "gear",
    "Throttle": "throttle",
    "Brake": "brake",
    "DRS": "drs",
    "Source": "provider_source",
    "X": "x",
    "Y": "y",
    "Z": "z",
    "Status": "status",
    "DriverNumber": "driver_number",
    "Driver": "driver_abbreviation",
    "DriverId": "driver_id",
    "LapNumber": "lap_number",
    "LapTime": "lap_time",
    "LapStartTime": "lap_start_time",
    "LapStartDate": "lap_start_timestamp",
    "Sector1Time": "sector_1_time",
    "Sector2Time": "sector_2_time",
    "Sector3Time": "sector_3_time",
    "Sector1SessionTime": "sector_1_session_time",
    "Sector2SessionTime": "sector_2_session_time",
    "Sector3SessionTime": "sector_3_session_time",
    "SpeedI1": "speed_i1",
    "SpeedI2": "speed_i2",
    "SpeedFL": "speed_fl",
    "SpeedST": "speed_st",
    "Stint": "stint",
    "Compound": "compound",
    "TyreLife": "tyre_life",
    "FreshTyre": "fresh_tyre",
    "PitInTime": "pit_in_time",
    "PitOutTime": "pit_out_time",
    "TrackStatus": "track_status",
    "Position": "position",
    "Deleted": "deleted",
    "DeletedReason": "deleted_reason",
    "IsAccurate": "is_accurate",
    "FastF1Generated": "provider_generated",
    "AirTemp": "air_temperature",
    "TrackTemp": "track_temperature",
    "Humidity": "humidity",
    "Pressure": "pressure",
    "WindSpeed": "wind_speed",
    "WindDirection": "wind_direction",
    "Rainfall": "rainfall",
    "Category": "category",
    "Message": "message",
    "Flag": "flag",
    "Scope": "scope",
    "RacingNumber": "racing_number",
    "Lap": "lap",
    "Sector": "sector",
    "Distance": "distance",
    "RelativeDistance": "relative_distance",
}
QUALITY_BITS = {
    "speed": 1,
    "rpm": 2,
    "gear": 4,
    "throttle": 8,
    "brake": 16,
    "drs": 32,
    "position": 64,
    "time": 128,
}


def normalize_table(name: str, original: pa.Table) -> tuple[pa.Table, dict[str, Any]]:
    columns = [ALIASES.get(column, column) for column in original.column_names]
    if len(columns) != len(set(columns)) or "quality_flags" in columns:
        raise ValueError("canonical/provider column collision")
    table = original.rename_columns(columns).replace_schema_metadata(original.schema.metadata)
    flags = np.zeros(table.num_rows, dtype=np.uint16)
    for field, low, high in [
        ("speed", 0, 450),
        ("rpm", 0, 22000),
        ("gear", 0, 8),
        ("throttle", 0, 100),
    ]:
        if field not in columns:
            continue
        values = np.asarray(table[field].to_numpy(), dtype=float)
        invalid = ~np.isfinite(values) | (values < low) | (values > high)
        if field == "gear":
            invalid |= np.isfinite(values) & (values != np.floor(values))
        flags[invalid] |= QUALITY_BITS[field]
    if "drs" in columns:
        values = np.asarray(table["drs"].to_numpy(), dtype=float)
        flags[~np.isin(values, [0, 1, 2, 3, 8, 10, 12, 14])] |= QUALITY_BITS["drs"]
    if "brake" in columns:
        if pa.types.is_boolean(table["brake"].type):
            flags[np.asarray(table["brake"].is_null())] |= QUALITY_BITS["brake"]
        else:
            flags[:] |= QUALITY_BITS["brake"]
    if "session_time" in columns:
        if not pa.types.is_duration(table["session_time"].type):
            raise ValueError("native session time must retain a duration dtype")
        if "session_time_ms" in columns:
            raise ValueError("canonical derived-time column collision")
        nanos = table["session_time"].cast(pa.duration("ns")).cast(pa.int64())
        milliseconds = np.asarray(nanos.to_numpy(), dtype=float) / 1_000_000
        table = table.append_column("session_time_ms", pa.array(milliseconds, from_pandas=True))
        invalid = ~np.isfinite(milliseconds) | (milliseconds < 0)
        flags[invalid] |= QUALITY_BITS["time"]
    if name.startswith("pos_data/"):
        for field in ("x", "y", "z"):
            if field in columns:
                flags[~np.isfinite(np.asarray(table[field].to_numpy(), dtype=float))] |= 64
    table = table.append_column("quality_flags", pa.array(flags, type=pa.uint16()))
    # Renaming and appending never changes the source fields, dtypes, row order or precision.
    for before, after in zip(original.column_names, columns, strict=True):
        if not original[before].equals(table[after]):
            raise ValueError("canonical field mapping changed source values")
    metadata = {
        "native_fields": dict(zip(original.column_names, columns, strict=True)),
        "available_fields": table.column_names,
        "driver_number": name.split("/")[1] if "/" in name else None,
        "source_kind": "provider-native FastF1 parsed data",
        "resampled": False,
        "quality_bits": QUALITY_BITS,
        "quality_flagged_samples": int(np.count_nonzero(flags)),
        "coordinates": "local-track-position" if name.startswith("pos_data/") else None,
        "derived_fields": [column for column in table.column_names if column not in columns],
    }
    return table, metadata
