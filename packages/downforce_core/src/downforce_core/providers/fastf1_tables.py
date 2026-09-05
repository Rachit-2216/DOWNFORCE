"""Provider-local extraction of documented FastF1 frames into owned Arrow tables."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    LoadOptions,
    ProviderTable,
    SessionRef,
)
from downforce_core.providers.fastf1_schemas import (
    CAR_INDEX_SPECS,
    DOCUMENTED_CAR_CHANNELS,
    DRIVER_SPECS,
    LAP_SPECS,
    RACE_CONTROL_SPECS,
    RACE_POSITION_SPECS,
    TRACK_POSITION_SPECS,
    WEATHER_SPECS,
    ColumnSpec,
)
from downforce_core.providers.fastf1_values import (
    clean_message,
    exception_detail,
    read_attribute,
)


def extract_tables(
    session: object,
    reference: SessionRef,
    options: LoadOptions,
    *,
    api_supported: bool,
    session_origin: datetime | None,
    warnings: list[str],
) -> dict[DatasetName, ProviderTable]:
    """Extract every requested dataset and classify every dataset explicitly."""

    tables = {
        name: ProviderTable(name=name, availability=DatasetAvailability.NOT_REQUESTED)
        for name in DatasetName
    }
    requested = options.datasets

    if DatasetName.DRIVERS in requested:
        tables[DatasetName.DRIVERS] = _table_from_property(
            session,
            "results",
            DatasetName.DRIVERS,
            DRIVER_SPECS,
            required_columns=("DriverNumber",),
        )

    detail_datasets = set(DatasetName) - {DatasetName.DRIVERS}
    if not api_supported:
        for name in requested & detail_datasets:
            tables[name] = ProviderTable(
                name=name,
                availability=DatasetAvailability.UNSUPPORTED,
            )
        if requested & detail_datasets:
            warnings.append("FastF1 timing API does not support detailed data for this session")
        return tables

    if DatasetName.LAPS in requested:
        tables[DatasetName.LAPS] = _table_from_property(
            session,
            "laps",
            DatasetName.LAPS,
            LAP_SPECS,
            required_columns=("DriverNumber", "LapNumber"),
        )
    if DatasetName.WEATHER in requested:
        tables[DatasetName.WEATHER] = _table_from_property(
            session,
            "weather_data",
            DatasetName.WEATHER,
            WEATHER_SPECS,
            required_columns=("Time",),
        )
    if DatasetName.RACE_CONTROL in requested:
        tables[DatasetName.RACE_CONTROL] = _extract_safely(
            DatasetName.RACE_CONTROL,
            lambda: _extract_race_control(session, session_origin, warnings),
        )
    if DatasetName.RACE_POSITIONS in requested:
        tables[DatasetName.RACE_POSITIONS] = _extract_safely(
            DatasetName.RACE_POSITIONS,
            lambda: _extract_race_positions(session),
        )
    if DatasetName.TRACK_POSITIONS in requested:
        if reference.season < 2020:
            tables[DatasetName.TRACK_POSITIONS] = ProviderTable(
                name=DatasetName.TRACK_POSITIONS,
                availability=DatasetAvailability.UNSUPPORTED,
            )
            warnings.append(
                "FastF1 position coordinates are not exposed before 2020 because their "
                "documented 0.1 m scale is not established"
            )
        else:
            tables[DatasetName.TRACK_POSITIONS] = _extract_safely(
                DatasetName.TRACK_POSITIONS,
                lambda: _extract_track_positions(session, warnings),
            )
    if DatasetName.CAR_TELEMETRY in requested:
        tables[DatasetName.CAR_TELEMETRY] = _extract_safely(
            DatasetName.CAR_TELEMETRY,
            lambda: _extract_car_index(session, warnings),
        )
    return tables


def _table_from_property(
    session: object,
    attribute: str,
    name: DatasetName,
    specs: Sequence[ColumnSpec],
    *,
    required_columns: Sequence[str] = (),
) -> ProviderTable:
    value, error = read_attribute(session, attribute)
    if error is not None:
        return _error_table(name, f"FastF1 property {attribute!r} failed", error)
    if not isinstance(value, pd.DataFrame):
        return _error_table(
            name,
            f"FastF1 property {attribute!r} was not tabular",
            TypeError(type(value).__name__),
        )
    # Empty is an observed state even if the upstream omitted its otherwise documented columns.
    if value.empty:
        return _table_from_frame(name, value, specs)
    missing = [column for column in required_columns if column not in value.columns]
    if missing:
        return _error_table(
            name,
            f"FastF1 property {attribute!r} omitted required columns",
            ValueError(", ".join(missing)),
        )
    return _table_from_frame(name, value, specs)


def _extract_race_positions(session: object) -> ProviderTable:
    value, error = read_attribute(session, "laps")
    if error is not None:
        return _error_table(
            DatasetName.RACE_POSITIONS,
            "FastF1 lap-end position extraction failed",
            error,
        )
    if not isinstance(value, pd.DataFrame):
        return _error_table(
            DatasetName.RACE_POSITIONS,
            "FastF1 laps were not tabular",
            TypeError(type(value).__name__),
        )
    if value.empty:
        return _table_from_frame(DatasetName.RACE_POSITIONS, value, RACE_POSITION_SPECS)
    required = ("Time", "DriverNumber", "Position")
    missing = [column for column in required if column not in value.columns]
    if missing:
        return _error_table(
            DatasetName.RACE_POSITIONS,
            "FastF1 laps omitted lap-end position columns",
            ValueError(", ".join(missing)),
        )
    valid = value.loc[value.loc[:, list(required)].notna().all(axis=1)].copy(deep=True)
    return _table_from_frame(DatasetName.RACE_POSITIONS, valid, RACE_POSITION_SPECS)


def _extract_race_control(
    session: object,
    session_origin: datetime | None,
    warnings: list[str],
) -> ProviderTable:
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    source_specs = (
        ("race_control_message", "race_control_messages"),
        ("track_status", "track_status"),
        ("session_status", "session_status"),
    )
    for source_kind, attribute in source_specs:
        value, error = read_attribute(session, attribute)
        if error is not None:
            detail = f"{source_kind}: {exception_detail(error)}"
            errors.append(detail)
            warnings.append(f"FastF1 race-control source unavailable: {detail}")
            continue
        if not isinstance(value, pd.DataFrame):
            detail = f"{source_kind}: expected DataFrame, got {type(value).__name__}"
            errors.append(detail)
            warnings.append(f"FastF1 race-control source unavailable: {detail}")
            continue
        frames.append(_race_control_frame(value, source_kind, session_origin))

    if not frames:
        return ProviderTable(
            name=DatasetName.RACE_CONTROL,
            availability=DatasetAvailability.ERROR,
            error=clean_message(
                "FastF1 race-control extraction failed for every source: " + "; ".join(errors)
            ),
        )
    records: list[dict[str, object]] = []
    for frame in frames:
        records.extend(cast(list[dict[str, object]], frame.to_dict(orient="records")))
    combined = pd.DataFrame.from_records(records)
    return _table_from_frame(DatasetName.RACE_CONTROL, combined, RACE_CONTROL_SPECS)


def _race_control_frame(
    source: pd.DataFrame,
    source_kind: str,
    session_origin: datetime | None,
) -> pd.DataFrame:
    result = pd.DataFrame(index=source.index)
    result["source_kind"] = pd.Series(source_kind, index=source.index, dtype="string")
    result["session_time"] = pd.Series(pd.NaT, index=source.index, dtype="timedelta64[ns]")
    result["utc_time"] = pd.Series(pd.NaT, index=source.index, dtype="datetime64[ns, UTC]")

    if source_kind == "race_control_message":
        if "Time" in source.columns:
            result["utc_time"] = pd.to_datetime(source["Time"], errors="coerce", utc=True)
            if session_origin is not None:
                result["session_time"] = result["utc_time"] - pd.Timestamp(session_origin)
    elif "Time" in source.columns:
        result["session_time"] = pd.to_timedelta(source["Time"], errors="coerce")
        if session_origin is not None:
            result["utc_time"] = pd.Timestamp(session_origin) + result["session_time"]

    string_columns = {
        "Category": "category",
        "Message": "message",
        "Status": "status",
        "Flag": "flag",
        "Scope": "scope",
        "RacingNumber": "racing_number",
    }
    for source_name, output_name in string_columns.items():
        result[output_name] = (
            source[source_name].astype("string")
            if source_name in source.columns
            else pd.Series(pd.NA, index=source.index, dtype="string")
        )
    for source_name, output_name in (("Sector", "sector"), ("Lap", "lap")):
        result[output_name] = (
            pd.to_numeric(source[source_name], errors="coerce").astype("Int64")
            if source_name in source.columns
            else pd.Series(pd.NA, index=source.index, dtype="Int64")
        )
    return result


def _extract_track_positions(session: object, warnings: list[str]) -> ProviderTable:
    value, error = read_attribute(session, "pos_data")
    if error is not None:
        return _error_table(
            DatasetName.TRACK_POSITIONS,
            "FastF1 position-data property failed",
            error,
        )
    if not isinstance(value, Mapping):
        return _error_table(
            DatasetName.TRACK_POSITIONS,
            "FastF1 position data was not keyed by driver number",
            TypeError(type(value).__name__),
        )

    tables: list[pa.Table] = []
    invalid_entries = 0
    for raw_driver, raw_frame in sorted(value.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_frame, pd.DataFrame):
            invalid_entries += 1
            warnings.append(
                f"FastF1 position data for driver {raw_driver!s} was not tabular and was skipped"
            )
            continue
        if raw_frame.empty:
            continue
        missing = [
            column for column in ("SessionTime", "X", "Y") if column not in raw_frame.columns
        ]
        if missing:
            invalid_entries += 1
            warnings.append(
                "FastF1 position data for driver "
                f"{raw_driver!s} omitted {', '.join(missing)} and was skipped"
            )
            continue
        tables.append(_track_table_from_frame(raw_frame, str(raw_driver)))

    if not tables and invalid_entries:
        return ProviderTable(
            name=DatasetName.TRACK_POSITIONS,
            availability=DatasetAvailability.ERROR,
            error="FastF1 position data contained no valid driver tables",
        )
    combined = (
        pa.concat_tables(tables)
        if tables
        else pa.Table.from_arrays(
            [pa.array([], type=arrow_type) for _, _, arrow_type in TRACK_POSITION_SPECS],
            names=[output_name for _, output_name, _ in TRACK_POSITION_SPECS],
        )
    )
    if combined.num_rows == 0:
        warnings.append("FastF1 returned no position samples for this session")
    availability = DatasetAvailability.AVAILABLE if combined.num_rows else DatasetAvailability.EMPTY
    return ProviderTable(
        name=DatasetName.TRACK_POSITIONS,
        availability=availability,
        data=combined,
    )


def _track_table_from_frame(frame: pd.DataFrame, driver_number: str) -> pa.Table:
    """Build one zero-concat Arrow chunk without copying the whole pandas frame."""

    arrays: list[pa.Array] = []
    fields: list[pa.Field] = []
    for source_name, output_name, arrow_type in TRACK_POSITION_SPECS:
        fields.append(pa.field(output_name, arrow_type))
        if output_name == "driver_number":
            arrays.append(pa.repeat(pa.scalar(driver_number, type=pa.string()), len(frame)))
        elif source_name in frame.columns:
            prepared = _prepare_series(frame[source_name], arrow_type)
            arrays.append(pa.array(prepared, type=arrow_type, from_pandas=True, safe=True))
        else:
            arrays.append(pa.nulls(len(frame), type=arrow_type))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _extract_car_index(session: object, warnings: list[str]) -> ProviderTable:
    value, error = read_attribute(session, "car_data")
    if error is not None:
        return _error_table(
            DatasetName.CAR_TELEMETRY,
            "FastF1 car-data property failed",
            error,
        )
    if not isinstance(value, Mapping):
        return _error_table(
            DatasetName.CAR_TELEMETRY,
            "FastF1 car data was not keyed by driver number",
            TypeError(type(value).__name__),
        )

    rows: list[dict[str, object]] = []
    invalid_entries = 0
    for raw_driver, raw_frame in sorted(value.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_frame, pd.DataFrame):
            invalid_entries += 1
            warnings.append(
                f"FastF1 car data for driver {raw_driver!s} was not tabular and was skipped"
            )
            continue
        if raw_frame.empty:
            continue
        available_time_columns = [
            column for column in ("SessionTime", "Time") if column in raw_frame.columns
        ]
        if not available_time_columns:
            invalid_entries += 1
            warnings.append(
                f"FastF1 car data for driver {raw_driver!s} had no documented time channel"
            )
            continue
        resolved_times = pd.Series(pd.NaT, index=raw_frame.index, dtype="timedelta64[ns]")
        for time_column in available_time_columns:
            candidate = pd.to_timedelta(raw_frame[time_column], errors="coerce")
            resolved_times = resolved_times.fillna(candidate)
        times = resolved_times.dropna()
        if times.empty:
            invalid_entries += 1
            warnings.append(f"FastF1 car data for driver {raw_driver!s} had no valid time samples")
            continue
        discarded = len(raw_frame) - len(times)
        if discarded:
            warnings.append(
                "FastF1 car data for driver "
                f"{raw_driver!s} discarded {discarded} samples with invalid timestamps"
            )
        driver_number = str(raw_driver)
        rows.append(
            {
                "driver_number": driver_number,
                "start_time": times.min(),
                "end_time": times.max(),
                "data_key": _telemetry_data_key(driver_number),
                "channel_names": [
                    channel for channel in DOCUMENTED_CAR_CHANNELS if channel in raw_frame.columns
                ],
                "sample_count": len(times),
            }
        )

    if not rows and invalid_entries:
        return ProviderTable(
            name=DatasetName.CAR_TELEMETRY,
            availability=DatasetAvailability.ERROR,
            error="FastF1 car telemetry contained no valid driver indexes",
        )
    return _table_from_frame(
        DatasetName.CAR_TELEMETRY,
        pd.DataFrame(rows),
        CAR_INDEX_SPECS,
    )


def _table_from_frame(
    name: DatasetName,
    frame: pd.DataFrame,
    specs: Sequence[ColumnSpec],
) -> ProviderTable:
    try:
        prepared = pd.DataFrame(index=frame.index)
        fields: list[pa.Field] = []
        for source_name, output_name, arrow_type in specs:
            fields.append(pa.field(output_name, arrow_type))
            if source_name in frame.columns:
                prepared[output_name] = _prepare_series(frame[source_name], arrow_type)
            else:
                prepared[output_name] = pd.Series(None, index=frame.index, dtype="object")
        table = pa.Table.from_pandas(
            prepared,
            schema=pa.schema(fields),
            preserve_index=False,
            safe=True,
        ).replace_schema_metadata()
    except Exception as exc:
        return _error_table(name, "FastF1 table conversion failed", exc)
    availability = DatasetAvailability.AVAILABLE if table.num_rows else DatasetAvailability.EMPTY
    return ProviderTable(name=name, availability=availability, data=table)


def _prepare_series(series: pd.Series[object], arrow_type: pa.DataType) -> pd.Series[object]:
    if pa.types.is_timestamp(arrow_type):
        return cast("pd.Series[object]", pd.to_datetime(series, errors="coerce", utc=True))
    if pa.types.is_duration(arrow_type):
        return cast("pd.Series[object]", pd.to_timedelta(series, errors="coerce"))
    if pa.types.is_string(arrow_type):
        return cast("pd.Series[object]", series.astype("string"))
    if pa.types.is_boolean(arrow_type):
        return cast("pd.Series[object]", series.astype("boolean"))
    if pa.types.is_integer(arrow_type):
        return cast(
            "pd.Series[object]",
            pd.to_numeric(series, errors="coerce").astype("Int64"),
        )
    if pa.types.is_floating(arrow_type):
        return cast("pd.Series[object]", pd.to_numeric(series, errors="coerce"))
    return series


def _telemetry_data_key(driver_number: str) -> str:
    safe_driver = re.sub(r"[^a-z0-9]+", "-", driver_number.casefold()).strip("-")
    if not safe_driver:
        safe_driver = "unknown"
    return f"fastf1-car-data-{safe_driver}"


def _extract_safely(
    name: DatasetName,
    extractor: Callable[[], ProviderTable],
) -> ProviderTable:
    try:
        return extractor()
    except Exception as exc:
        return _error_table(name, "FastF1 dataset extraction failed", exc)


def _error_table(name: DatasetName, context: str, error: Exception) -> ProviderTable:
    return ProviderTable(
        name=name,
        availability=DatasetAvailability.ERROR,
        error=clean_message(f"{context}: {exception_detail(error)}"),
    )


__all__ = ["extract_tables"]
