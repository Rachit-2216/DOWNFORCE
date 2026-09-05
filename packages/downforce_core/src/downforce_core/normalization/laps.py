"""Lap normalization and explicit stint/pit derivations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from downforce_core.domain.enums import TyreCompound
from downforce_core.domain.identifiers import DriverId
from downforce_core.domain.models import LapRecord, PitStopRecord, SessionMetadata, StintRecord
from downforce_core.normalization._shared import dedupe_rows, provenance, rows_for
from downforce_core.normalization.values import (
    as_bool,
    as_float,
    as_int,
    as_session_time_ms,
    as_text,
)
from downforce_core.providers.base import DatasetName, ProviderSession


@dataclass(frozen=True, slots=True)
class _PitObservation:
    driver_id: DriverId
    lap_number: int
    pit_in_time_ms: int | None
    pit_out_time_ms: int | None
    source_record_id: str


def normalize_laps(
    session: ProviderSession,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[tuple[LapRecord, ...], tuple[_PitObservation, ...]]:
    rows = rows_for(session, DatasetName.LAPS, warnings)

    def lap_key(row: Mapping[str, object]) -> tuple[object, ...]:
        return (as_int(row.get("driver_number")), as_int(row.get("lap_number")))

    selected = dedupe_rows(rows, key=lap_key, table="laps", warnings=warnings)
    laps: list[LapRecord] = []
    pit_observations: list[_PitObservation] = []
    driver_order = {number: index for index, number in enumerate(sorted(driver_ids))}
    for row in selected:
        number = as_int(row.get("driver_number"))
        lap_number = as_int(row.get("lap_number"))
        if number is None or lap_number is None:
            raise ValueError("lap row is missing driver_number or lap_number")
        driver_id = driver_ids.get(number, make_unrostered_driver_id(metadata, number))
        raw_compound = as_text(row.get("compound"))
        lap_start_time_ms = as_session_time_ms(row.get("lap_start_time"))
        if lap_start_time_ms is None and row.get("lap_start_date") is not None:
            if metadata.session_origin_utc is None:
                warnings.append(f"laps.unplaced-start-date: driver={number},lap={lap_number}")
            else:
                lap_start_time_ms = as_session_time_ms(
                    row.get("lap_start_date"),
                    origin=metadata.session_origin_utc,
                )
        lap = LapRecord(
            session_id=metadata.session_id,
            driver_id=driver_id,
            lap_number=lap_number,
            provenance=provenance(session, f"{session.provider_name}.laps", row),
            lap_start_time_ms=lap_start_time_ms,
            lap_end_time_ms=as_session_time_ms(row.get("time")),
            lap_time_ms=as_session_time_ms(row.get("lap_time")),
            sector_1_time_ms=as_session_time_ms(row.get("sector_1_time")),
            sector_2_time_ms=as_session_time_ms(row.get("sector_2_time")),
            sector_3_time_ms=as_session_time_ms(row.get("sector_3_time")),
            stint_number=as_int(row.get("stint")),
            compound=TyreCompound.from_raw(raw_compound),
            raw_compound=raw_compound,
            tyre_life_laps=as_float(row.get("tyre_life")),
            is_personal_best=as_bool(row.get("is_personal_best")),
            is_accurate=as_bool(row.get("is_accurate")),
            is_generated=as_bool(row.get("fastf1_generated")),
            is_deleted=as_bool(row.get("deleted")),
            deleted_reason=as_text(row.get("deleted_reason")),
            raw_track_status=as_text(row.get("track_status")),
        )
        laps.append(lap)
        pit_in = as_session_time_ms(row.get("pit_in_time"))
        pit_out = as_session_time_ms(row.get("pit_out_time"))
        if pit_in is not None or pit_out is not None:
            source_record_id = lap.provenance.source_record_id
            if source_record_id is None:
                raise ValueError("lap provenance requires a stable source record id")
            pit_observations.append(
                _PitObservation(driver_id, lap_number, pit_in, pit_out, source_record_id)
            )
    laps.sort(
        key=lambda record: (
            driver_order.get(_number_for_driver(record.driver_id, driver_ids), len(driver_order)),
            record.lap_number,
        )
    )
    pit_observations.sort(
        key=lambda item: (
            str(item.driver_id),
            min(
                value for value in (item.pit_in_time_ms, item.pit_out_time_ms) if value is not None
            ),
            item.lap_number,
        )
    )
    return tuple(laps), tuple(pit_observations)


def make_unrostered_driver_id(metadata: SessionMetadata, number: int) -> DriverId:
    # Retain the reference so validation can report the missing roster row.
    from downforce_core.domain.identifiers import make_driver_id

    return make_driver_id(metadata.session_id, number)


def _number_for_driver(driver_id: DriverId, driver_ids: Mapping[int, DriverId]) -> int:
    return next(
        (number for number, candidate in driver_ids.items() if candidate == driver_id),
        10**9,
    )


def derive_stints(
    session: ProviderSession,
    laps: tuple[LapRecord, ...],
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[StintRecord, ...]:
    grouped: dict[tuple[DriverId, int], list[LapRecord]] = defaultdict(list)
    for lap in laps:
        if lap.stint_number is not None:
            grouped[(lap.driver_id, lap.stint_number)].append(lap)
    derived: list[StintRecord] = []
    for (driver_id, stint_number), stint_laps in grouped.items():
        stint_laps.sort(key=lambda lap: lap.lap_number)
        first = stint_laps[0]
        last = stint_laps[-1]
        if first.lap_start_time_ms is None:
            warnings.append(f"stints.partial-start: driver={driver_id},stint={stint_number}")
        if last.lap_end_time_ms is None:
            warnings.append(f"stints.partial-end: driver={driver_id},stint={stint_number}")
        compounds = sorted({lap.raw_compound for lap in stint_laps if lap.raw_compound is not None})
        if len(compounds) > 1:
            warnings.append(f"stints.conflicting-compound: driver={driver_id},stint={stint_number}")
        raw_compound = compounds[0] if compounds else None
        source_ids = tuple(lap.provenance.source_record_id for lap in stint_laps)
        derived.append(
            StintRecord(
                session_id=first.session_id,
                driver_id=driver_id,
                stint_number=stint_number,
                start_lap=first.lap_number,
                end_lap=last.lap_number,
                start_time_ms=first.lap_start_time_ms,
                end_time_ms=last.lap_end_time_ms,
                compound=TyreCompound.from_raw(raw_compound),
                raw_compound=raw_compound,
                tyre_life_start_laps=first.tyre_life_laps,
                tyre_life_end_laps=last.tyre_life_laps,
                provenance=provenance(
                    session,
                    f"{session.provider_name}.laps.derived-stint",
                    {
                        "driver_id": driver_id,
                        "stint_number": stint_number,
                        "source_record_ids": source_ids,
                    },
                ),
            )
        )
    driver_order = {
        driver_id: index for index, (_, driver_id) in enumerate(sorted(driver_ids.items()))
    }
    derived.sort(
        key=lambda record: (
            driver_order.get(record.driver_id, len(driver_order)),
            record.stint_number,
        )
    )
    return tuple(derived)


def derive_pit_stops(
    session: ProviderSession,
    metadata: SessionMetadata,
    observations: tuple[_PitObservation, ...],
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> tuple[PitStopRecord, ...]:
    grouped: dict[DriverId, list[_PitObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.driver_id].append(observation)
    records: list[PitStopRecord] = []
    driver_order = {
        driver_id: index for index, (_, driver_id) in enumerate(sorted(driver_ids.items()))
    }
    for driver_id, driver_observations in grouped.items():
        events: list[tuple[int, int, int, str, _PitObservation]] = []
        for observation in driver_observations:
            if observation.pit_in_time_ms is not None:
                events.append(
                    (
                        observation.pit_in_time_ms,
                        0,
                        observation.lap_number,
                        observation.source_record_id,
                        observation,
                    )
                )
            if observation.pit_out_time_ms is not None:
                events.append(
                    (
                        observation.pit_out_time_ms,
                        1,
                        observation.lap_number,
                        observation.source_record_id,
                        observation,
                    )
                )
        events.sort(key=lambda event: event[:4])
        unmatched_entries: list[tuple[int, _PitObservation]] = []
        paired: list[tuple[int | None, int | None, int, tuple[str, ...]]] = []
        for event_time, event_kind, _, _, observation in events:
            if event_kind == 0:
                unmatched_entries.append((event_time, observation))
                continue
            if not unmatched_entries:
                paired.append(
                    (
                        None,
                        event_time,
                        observation.lap_number,
                        (observation.source_record_id,),
                    )
                )
                continue
            entry_time, entry = unmatched_entries[-1]
            # Lap numbers are contextual guards. Never skip a later unmatched entry to
            # connect an exit with an older stop; an impossible lap direction remains partial.
            if entry.lap_number > observation.lap_number:
                paired.append(
                    (
                        None,
                        event_time,
                        observation.lap_number,
                        (observation.source_record_id,),
                    )
                )
                continue
            if observation.lap_number not in {entry.lap_number, entry.lap_number + 1}:
                warnings.append(
                    "pits.nonadjacent-observations: "
                    f"driver={driver_id},entry_lap={entry.lap_number},"
                    f"exit_lap={observation.lap_number}: observations left partial"
                )
                paired.append(
                    (
                        None,
                        event_time,
                        observation.lap_number,
                        (observation.source_record_id,),
                    )
                )
                continue
            unmatched_entries.pop()
            source_ids = tuple(
                dict.fromkeys((entry.source_record_id, observation.source_record_id))
            )
            paired.append(
                (
                    entry_time,
                    event_time,
                    entry.lap_number,
                    source_ids,
                )
            )
        for entry_time, entry in unmatched_entries:
            paired.append(
                (
                    entry_time,
                    None,
                    entry.lap_number,
                    (entry.source_record_id,),
                )
            )
        paired.sort(
            key=lambda item: (
                min(value for value in item[:2] if value is not None),
                item[2],
                item[3],
            )
        )
        for stop_number, (
            paired_entry_time,
            paired_exit_time,
            lap_number,
            source_ids,
        ) in enumerate(paired, start=1):
            if paired_entry_time is None or paired_exit_time is None:
                warnings.append(f"pits.partial-pit-stop: driver={driver_id},stop={stop_number}")
            lane_duration = (
                paired_exit_time - paired_entry_time
                if paired_entry_time is not None and paired_exit_time is not None
                else None
            )
            records.append(
                PitStopRecord(
                    session_id=metadata.session_id,
                    driver_id=driver_id,
                    stop_number=stop_number,
                    lap_number=lap_number,
                    pit_in_time_ms=paired_entry_time,
                    pit_out_time_ms=paired_exit_time,
                    pit_lane_duration_ms=lane_duration,
                    stationary_duration_ms=None,
                    provenance=provenance(
                        session,
                        f"{session.provider_name}.laps.derived-pit",
                        {
                            "driver_id": driver_id,
                            "stop_number": stop_number,
                            "source_record_ids": source_ids,
                        },
                    ),
                )
            )
    records.sort(
        key=lambda record: (
            driver_order.get(record.driver_id, len(driver_order)),
            record.stop_number,
        )
    )
    return tuple(records)


__all__ = ["derive_pit_stops", "derive_stints", "normalize_laps"]
