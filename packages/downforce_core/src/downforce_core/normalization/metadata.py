"""Canonical session metadata, roster, and classification normalization."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from downforce_core.domain.enums import DataQuality, SessionType
from downforce_core.domain.identifiers import DriverId, make_driver_id, make_session_id
from downforce_core.domain.models import (
    DriverClassificationRecord,
    DriverRecord,
    SessionMetadata,
)
from downforce_core.normalization._shared import dedupe_rows, provenance, rows_for
from downforce_core.normalization.values import (
    as_float,
    as_int,
    as_session_time_ms,
    as_text,
    as_utc_datetime,
    normalize_status,
)
from downforce_core.providers.base import DatasetAvailability, DatasetName, ProviderSession


def _metadata_value(metadata: Mapping[str, object], name: str) -> object:
    return metadata.get(name)


def _data_quality(session: ProviderSession) -> DataQuality:
    states = tuple(table.availability for table in session.tables.values())
    if any(state is DatasetAvailability.ERROR for state in states):
        return DataQuality.DEGRADED
    if any(
        state
        in {
            DatasetAvailability.EMPTY,
            DatasetAvailability.NOT_REQUESTED,
            DatasetAvailability.UNSUPPORTED,
        }
        for state in states
    ):
        return DataQuality.PARTIAL
    return DataQuality.COMPLETE


def normalize_metadata(session: ProviderSession) -> SessionMetadata:
    metadata = session.metadata
    event_name = as_text(_metadata_value(metadata, "event_name"))
    if event_name is None:
        raise ValueError("provider metadata is missing official event_name")
    session_name = as_text(_metadata_value(metadata, "session_name"))
    if session_name is None:
        raise ValueError("provider metadata is missing official session_name")
    season = as_int(_metadata_value(metadata, "season"))
    if season is None:
        season = session.session.season
    session_type = SessionType.from_raw(session_name)
    if session_type is SessionType.UNKNOWN:
        session_type = session.session.session_type
    session_id = make_session_id(season, event_name, session_type)
    source_id = as_text(_metadata_value(metadata, "provider_source_id"))
    source_value: object = source_id if source_id is not None else dict(metadata)
    scheduled_start = as_utc_datetime(_metadata_value(metadata, "scheduled_start_utc"))
    session_origin = as_utc_datetime(_metadata_value(metadata, "session_origin_utc"))
    session_start_offset_ms = as_session_time_ms(_metadata_value(metadata, "session_start_time_ms"))
    actual_start = (
        session_origin + timedelta(milliseconds=session_start_offset_ms)
        if session_origin is not None and session_start_offset_ms is not None
        else None
    )
    return SessionMetadata(
        session_id=session_id,
        season=season,
        event_name=event_name,
        session_name=session_name,
        session_type=session_type,
        provenance=provenance(session, f"{session.provider_name}.session", source_value),
        round_number=as_int(_metadata_value(metadata, "round_number")),
        country_code=as_text(_metadata_value(metadata, "country_code")),
        circuit_name=as_text(_metadata_value(metadata, "circuit_name")),
        scheduled_start_utc=scheduled_start,
        session_start_utc=actual_start,
        session_origin_utc=session_origin,
        data_quality=_data_quality(session),
    )


def normalize_drivers(
    session: ProviderSession,
    metadata: SessionMetadata,
    warnings: list[str],
) -> tuple[
    tuple[DriverRecord, ...],
    tuple[DriverClassificationRecord, ...],
    Mapping[int, DriverId],
]:
    rows = rows_for(session, DatasetName.DRIVERS, warnings)

    def driver_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (as_int(row.get("driver_number")),)

    selected = dedupe_rows(
        rows,
        key=driver_key,
        table="drivers",
        warnings=warnings,
    )
    roster: list[DriverRecord] = []
    classifications: list[DriverClassificationRecord] = []
    driver_ids: dict[int, DriverId] = {}
    for row in selected:
        number = as_int(row.get("driver_number"))
        if number is None:
            raise ValueError("driver row is missing driver_number")
        driver_id = make_driver_id(metadata.session_id, number)
        driver_ids[number] = driver_id
        roster.append(
            DriverRecord(
                session_id=metadata.session_id,
                driver_id=driver_id,
                provenance=provenance(session, f"{session.provider_name}.drivers", row),
                racing_number=number,
                abbreviation=as_text(row.get("abbreviation")),
                full_name=as_text(row.get("full_name")),
                team_name=as_text(row.get("team_name")),
                country_code=as_text(row.get("country_code")),
            )
        )

        raw_classified = as_text(row.get("classified_position"))
        raw_status = as_text(row.get("status"))
        position = as_int(row.get("position"))
        points = as_float(row.get("points"))
        if all(value is None for value in (raw_classified, raw_status, position, points)):
            continue
        classified_position: int | None = None
        if raw_classified is not None:
            try:
                classified_position = as_int(raw_classified)
            except ValueError:
                classified_position = None
        if classified_position is None:
            classified_position = position
        classifications.append(
            DriverClassificationRecord(
                session_id=metadata.session_id,
                driver_id=driver_id,
                provenance=provenance(
                    session,
                    f"{session.provider_name}.classification",
                    row,
                ),
                classified_position=classified_position,
                status=normalize_status(raw_classified, raw_status),
                points=points,
                raw_status=raw_status,
            )
        )
    roster.sort(key=lambda record: (record.racing_number is None, record.racing_number or 0))
    driver_order = {driver_id: number for number, driver_id in sorted(driver_ids.items())}
    classifications.sort(key=lambda record: driver_order.get(record.driver_id, 10**9))
    return tuple(roster), tuple(classifications), driver_ids


__all__ = ["normalize_drivers", "normalize_metadata"]
