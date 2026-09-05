"""Ordinal/geometric position normalization and lazy car-telemetry indexing."""

from __future__ import annotations

from collections.abc import Mapping

from downforce_core.domain.identifiers import DriverId, make_driver_id
from downforce_core.domain.models import (
    RacePositionRecord,
    SessionMetadata,
    TelemetryIndexRecord,
)
from downforce_core.normalization._shared import dedupe_rows, provenance, rows_for
from downforce_core.normalization.track_positions import normalize_track_positions
from downforce_core.normalization.values import (
    as_int,
    as_session_time_ms,
    as_text,
    normalize_missing,
)
from downforce_core.providers.base import DatasetName, ProviderSession


def _driver_id(
    number: int,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
) -> DriverId:
    return driver_ids.get(number, make_driver_id(metadata.session_id, number))


def normalize_race_positions(
    session: ProviderSession,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[RacePositionRecord, ...]:
    rows = rows_for(session, DatasetName.RACE_POSITIONS, warnings)

    def position_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (
            as_int(row.get("driver_number")),
            as_session_time_ms(row.get("time")),
            as_int(row.get("lap_number")),
        )

    selected = dedupe_rows(
        rows,
        key=position_key,
        table="race_positions",
        warnings=warnings,
    )
    records: list[RacePositionRecord] = []
    for row in selected:
        number = as_int(row.get("driver_number"))
        time_ms = as_session_time_ms(row.get("time"))
        position = as_int(row.get("position"))
        if number is None or time_ms is None or position is None:
            raise ValueError("race-position row is missing driver, time, or ordinal position")
        records.append(
            RacePositionRecord(
                session_id=metadata.session_id,
                driver_id=_driver_id(number, metadata, driver_ids),
                session_time_ms=time_ms,
                position=position,
                lap_number=as_int(row.get("lap_number")),
                provenance=provenance(
                    session,
                    f"{session.provider_name}.race-positions.lap-end",
                    row,
                ),
            )
        )
    order = {driver_id: index for index, (_, driver_id) in enumerate(sorted(driver_ids.items()))}
    records.sort(
        key=lambda record: (
            record.session_time_ms,
            order.get(record.driver_id, len(order)),
            record.lap_number or 0,
        )
    )
    return tuple(records)


def _channels(value: object) -> tuple[str, ...]:
    normalized = normalize_missing(value)
    if normalized is None:
        return ()
    if isinstance(normalized, str):
        values: tuple[object, ...] = (normalized,)
    elif isinstance(normalized, (list, tuple)):
        values = tuple(normalized)
    else:
        raise TypeError("channel_names must be a list or tuple")
    channels = {channel for value in values if (channel := as_text(value)) is not None}
    return tuple(sorted(channels))


def normalize_telemetry_index(
    session: ProviderSession,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[TelemetryIndexRecord, ...]:
    rows = rows_for(session, DatasetName.CAR_TELEMETRY, warnings)

    def telemetry_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (
            as_int(row.get("driver_number")),
            as_text(row.get("data_key")),
            as_int(row.get("lap_number")),
        )

    selected = dedupe_rows(
        rows,
        key=telemetry_key,
        table="car_telemetry",
        warnings=warnings,
    )
    records: list[TelemetryIndexRecord] = []
    for row in selected:
        number = as_int(row.get("driver_number"))
        start = as_session_time_ms(row.get("start_time"))
        end = as_session_time_ms(row.get("end_time"))
        data_key = as_text(row.get("data_key"))
        sample_count = as_int(row.get("sample_count"))
        if None in (number, start, end, data_key, sample_count):
            raise ValueError("telemetry index row is missing required range metadata")
        if not isinstance(number, int) or not isinstance(start, int) or not isinstance(end, int):
            raise TypeError("telemetry index identifiers and times must be integers")
        if not isinstance(data_key, str) or not isinstance(sample_count, int):
            raise TypeError("telemetry index data key/count have invalid types")
        records.append(
            TelemetryIndexRecord(
                session_id=metadata.session_id,
                driver_id=_driver_id(number, metadata, driver_ids),
                start_time_ms=start,
                end_time_ms=end,
                data_key=data_key,
                channel_names=_channels(row.get("channel_names")),
                sample_count=sample_count,
                lap_number=as_int(row.get("lap_number")),
                provenance=provenance(
                    session,
                    f"{session.provider_name}.car-telemetry-index",
                    row,
                ),
            )
        )
    order = {driver_id: index for index, (_, driver_id) in enumerate(sorted(driver_ids.items()))}
    records.sort(
        key=lambda record: (
            order.get(record.driver_id, len(order)),
            record.start_time_ms,
            record.data_key,
        )
    )
    return tuple(records)


__all__ = [
    "normalize_race_positions",
    "normalize_telemetry_index",
    "normalize_track_positions",
]
