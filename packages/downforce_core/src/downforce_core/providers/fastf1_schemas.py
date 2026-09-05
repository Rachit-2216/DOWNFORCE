"""Stable documented FastF1 columns admitted at the provider boundary."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

type ColumnSpec = tuple[str, str, pa.DataType]

DRIVER_SPECS: tuple[ColumnSpec, ...] = (
    ("DriverNumber", "driver_number", pa.string()),
    ("BroadcastName", "broadcast_name", pa.string()),
    ("Abbreviation", "abbreviation", pa.string()),
    ("DriverId", "driver_id", pa.string()),
    ("TeamName", "team_name", pa.string()),
    ("TeamColor", "team_color", pa.string()),
    ("TeamId", "team_id", pa.string()),
    ("FirstName", "first_name", pa.string()),
    ("LastName", "last_name", pa.string()),
    ("FullName", "full_name", pa.string()),
    ("HeadshotUrl", "headshot_url", pa.string()),
    ("CountryCode", "country_code", pa.string()),
    ("Position", "position", pa.float64()),
    ("ClassifiedPosition", "classified_position", pa.string()),
    ("GridPosition", "grid_position", pa.float64()),
    ("Q1", "q1", pa.duration("ns")),
    ("Q2", "q2", pa.duration("ns")),
    ("Q3", "q3", pa.duration("ns")),
    ("Time", "time", pa.duration("ns")),
    ("Status", "status", pa.string()),
    ("Points", "points", pa.float64()),
    ("Laps", "laps", pa.float64()),
)

LAP_SPECS: tuple[ColumnSpec, ...] = (
    ("Time", "time", pa.duration("ns")),
    ("Driver", "driver", pa.string()),
    ("DriverNumber", "driver_number", pa.string()),
    ("LapTime", "lap_time", pa.duration("ns")),
    ("LapNumber", "lap_number", pa.float64()),
    ("Stint", "stint", pa.float64()),
    ("PitOutTime", "pit_out_time", pa.duration("ns")),
    ("PitInTime", "pit_in_time", pa.duration("ns")),
    ("Sector1Time", "sector_1_time", pa.duration("ns")),
    ("Sector2Time", "sector_2_time", pa.duration("ns")),
    ("Sector3Time", "sector_3_time", pa.duration("ns")),
    ("Sector1SessionTime", "sector_1_session_time", pa.duration("ns")),
    ("Sector2SessionTime", "sector_2_session_time", pa.duration("ns")),
    ("Sector3SessionTime", "sector_3_session_time", pa.duration("ns")),
    ("SpeedI1", "speed_i1", pa.float64()),
    ("SpeedI2", "speed_i2", pa.float64()),
    ("SpeedFL", "speed_fl", pa.float64()),
    ("SpeedST", "speed_st", pa.float64()),
    ("IsPersonalBest", "is_personal_best", pa.bool_()),
    ("Compound", "compound", pa.string()),
    ("TyreLife", "tyre_life", pa.float64()),
    ("FreshTyre", "fresh_tyre", pa.bool_()),
    ("Team", "team", pa.string()),
    ("LapStartTime", "lap_start_time", pa.duration("ns")),
    ("LapStartDate", "lap_start_date", pa.timestamp("ns", tz="UTC")),
    ("TrackStatus", "track_status", pa.string()),
    ("Position", "position", pa.float64()),
    ("Deleted", "deleted", pa.bool_()),
    ("DeletedReason", "deleted_reason", pa.string()),
    ("FastF1Generated", "fastf1_generated", pa.bool_()),
    ("IsAccurate", "is_accurate", pa.bool_()),
)

WEATHER_SPECS: tuple[ColumnSpec, ...] = (
    ("Time", "time", pa.duration("ns")),
    ("AirTemp", "air_temp", pa.float64()),
    ("Humidity", "humidity", pa.float64()),
    ("Pressure", "pressure", pa.float64()),
    ("Rainfall", "rainfall", pa.bool_()),
    ("TrackTemp", "track_temp", pa.float64()),
    ("WindDirection", "wind_direction", pa.float64()),
    ("WindSpeed", "wind_speed", pa.float64()),
)

RACE_CONTROL_SPECS: tuple[ColumnSpec, ...] = (
    ("source_kind", "source_kind", pa.string()),
    ("session_time", "session_time", pa.duration("ns")),
    ("utc_time", "utc_time", pa.timestamp("ns", tz="UTC")),
    ("category", "category", pa.string()),
    ("message", "message", pa.string()),
    ("status", "status", pa.string()),
    ("flag", "flag", pa.string()),
    ("scope", "scope", pa.string()),
    ("sector", "sector", pa.int64()),
    ("racing_number", "racing_number", pa.string()),
    ("lap", "lap", pa.int64()),
)

RACE_POSITION_SPECS: tuple[ColumnSpec, ...] = (
    ("Time", "time", pa.duration("ns")),
    ("DriverNumber", "driver_number", pa.string()),
    ("LapNumber", "lap_number", pa.float64()),
    ("Position", "position", pa.float64()),
)

TRACK_POSITION_SPECS: tuple[ColumnSpec, ...] = (
    ("driver_number", "driver_number", pa.string()),
    ("Date", "date", pa.timestamp("ns", tz="UTC")),
    ("Time", "time", pa.duration("ns")),
    ("SessionTime", "session_time", pa.duration("ns")),
    ("X", "x", pa.float64()),
    ("Y", "y", pa.float64()),
    ("Z", "z", pa.float64()),
    ("Status", "status", pa.string()),
    ("Source", "source", pa.string()),
)

CAR_INDEX_SPECS: tuple[ColumnSpec, ...] = (
    ("driver_number", "driver_number", pa.string()),
    ("start_time", "start_time", pa.duration("ns")),
    ("end_time", "end_time", pa.duration("ns")),
    ("data_key", "data_key", pa.string()),
    ("channel_names", "channel_names", pa.list_(pa.string())),
    ("sample_count", "sample_count", pa.int64()),
)

DOCUMENTED_CAR_CHANNELS = (
    "Date",
    "Time",
    "SessionTime",
    "Speed",
    "RPM",
    "nGear",
    "Throttle",
    "Brake",
    "DRS",
    "Source",
)

__all__ = [
    "CAR_INDEX_SPECS",
    "DOCUMENTED_CAR_CHANNELS",
    "DRIVER_SPECS",
    "LAP_SPECS",
    "RACE_CONTROL_SPECS",
    "RACE_POSITION_SPECS",
    "TRACK_POSITION_SPECS",
    "WEATHER_SPECS",
    "ColumnSpec",
]
