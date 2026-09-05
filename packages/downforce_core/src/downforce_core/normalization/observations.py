"""Weather and race-control observation normalization without filling or text guessing."""

from __future__ import annotations

from collections.abc import Mapping

from downforce_core.domain.enums import TrackStatus
from downforce_core.domain.identifiers import DriverId, make_driver_id
from downforce_core.domain.models import RaceControlRecord, SessionMetadata, WeatherRecord
from downforce_core.normalization._shared import (
    dedupe_rows,
    provenance,
    rows_for,
    stable_record_id,
)
from downforce_core.normalization.values import (
    as_bool,
    as_float,
    as_int,
    as_session_time_ms,
    as_text,
)
from downforce_core.providers.base import DatasetName, ProviderSession


def normalize_weather(
    session: ProviderSession,
    metadata: SessionMetadata,
    warnings: list[str],
) -> tuple[WeatherRecord, ...]:
    rows = rows_for(session, DatasetName.WEATHER, warnings)

    def weather_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (as_session_time_ms(row.get("time")),)

    selected = dedupe_rows(rows, key=weather_key, table="weather", warnings=warnings)
    records: list[WeatherRecord] = []
    for row in selected:
        session_time_ms = as_session_time_ms(row.get("time"))
        if session_time_ms is None:
            raise ValueError("weather row is missing time")
        records.append(
            WeatherRecord(
                session_id=metadata.session_id,
                session_time_ms=session_time_ms,
                provenance=provenance(session, f"{session.provider_name}.weather", row),
                air_temperature_c=as_float(row.get("air_temp")),
                track_temperature_c=as_float(row.get("track_temp")),
                humidity_percent=as_float(row.get("humidity")),
                pressure_hpa=as_float(row.get("pressure")),
                rainfall=as_bool(row.get("rainfall")),
                wind_speed_mps=as_float(row.get("wind_speed")),
                wind_direction_deg=as_float(row.get("wind_direction")),
            )
        )
    records.sort(
        key=lambda record: (
            record.session_time_ms,
            record.provenance.source_record_id or "",
        )
    )
    return tuple(records)


def _raw_time(row: Mapping[str, object]) -> object:
    session_time = row.get("session_time")
    return session_time if session_time is not None else row.get("time")


def _race_control_time(row: Mapping[str, object], metadata: SessionMetadata) -> int:
    relative = as_session_time_ms(_raw_time(row))
    if relative is not None:
        return relative
    absolute = row.get("utc_time")
    if absolute is None:
        absolute = row.get("date")
    converted = as_session_time_ms(absolute, origin=metadata.session_origin_utc)
    if converted is None:
        raise ValueError("race-control row is missing a usable time")
    return converted


def normalize_race_control(
    session: ProviderSession,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[RaceControlRecord, ...]:
    rows = rows_for(session, DatasetName.RACE_CONTROL, warnings)
    timed_rows: list[dict[str, object]] = []
    for row in rows:
        try:
            _race_control_time(row, metadata)
        except ValueError:
            row_id = stable_record_id(f"{session.provider_name}.race-control", row)
            warnings.append(f"race_control.unplaced-time: row={row_id}")
        else:
            timed_rows.append(row)

    def control_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (
            as_text(row.get("source_kind")),
            _race_control_time(row, metadata),
            as_text(row.get("message")),
            as_text(row.get("status")),
            as_text(row.get("flag")),
            as_text(row.get("category")),
            as_text(row.get("scope")),
            as_int(row.get("racing_number")),
            as_int(row.get("lap")),
        )

    selected = dedupe_rows(
        timed_rows,
        key=control_key,
        table="race_control",
        warnings=warnings,
    )
    records: list[RaceControlRecord] = []
    for row in selected:
        source_kind = as_text(row.get("source_kind"))
        if source_kind is None:
            raise ValueError("race-control row is missing source_kind")
        raw_status = as_text(row.get("status"))
        raw_flag = as_text(row.get("flag"))
        message = as_text(row.get("message"))
        if message is None:
            # Status-only feeds carry their complete raw observation in this field.
            message = raw_status or raw_flag
        if message is None:
            warnings.append(
                f"race_control.missing-message: source_kind={source_kind},"
                f"time={_race_control_time(row, metadata)}"
            )
            continue
        normalized_track_status = TrackStatus.UNKNOWN
        if source_kind == "track_status":
            normalized_track_status = TrackStatus.from_raw(raw_status)
        elif source_kind == "race_control_message":
            normalized_track_status = TrackStatus.from_raw(raw_flag)
        number = as_int(row.get("racing_number"))
        driver_id = None
        if number is not None:
            driver_id = driver_ids.get(number, make_driver_id(metadata.session_id, number))
        records.append(
            RaceControlRecord(
                session_id=metadata.session_id,
                session_time_ms=_race_control_time(row, metadata),
                message=message,
                provenance=provenance(
                    session,
                    f"{session.provider_name}.race-control.{source_kind}",
                    row,
                ),
                track_status=normalized_track_status,
                raw_status=raw_status or raw_flag,
                category=as_text(row.get("category")),
                scope=as_text(row.get("scope")),
                source_kind=source_kind,
                lap_number=as_int(row.get("lap")),
                driver_id=driver_id,
            )
        )
    records.sort(
        key=lambda record: (
            record.session_time_ms,
            record.source_kind or "",
            record.provenance.source_record_id or "",
        )
    )
    return tuple(records)


__all__ = ["normalize_race_control", "normalize_weather"]
