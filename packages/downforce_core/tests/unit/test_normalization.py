from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from random import Random

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.domain import (
    DriverStatus,
    SessionType,
    TrackStatus,
    TyreCompound,
    make_driver_id,
    make_session_id,
)
from downforce_core.exceptions import NormalizationError, SessionDataIncompleteError
from downforce_core.normalization import normalize_session
from downforce_core.normalization.models import CanonicalTrackPositions
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderSession,
    ProviderTable,
    SessionRef,
)


def _table(name: DatasetName, rows: list[dict[str, object]]) -> ProviderTable:
    keys = sorted({key for row in rows for key in row})
    complete_rows = [{key: row.get(key) for key in keys} for row in rows]
    data = pa.Table.from_pylist(complete_rows)
    availability = DatasetAvailability.AVAILABLE if rows else DatasetAvailability.EMPTY
    return ProviderTable(name=name, availability=availability, data=data)


def _base_rows() -> dict[DatasetName, list[dict[str, object]]]:
    drivers = [
        {
            "driver_number": "0",
            "abbreviation": "AAA",
            "full_name": "Ada Apex",
            "team_name": "Analytical GP",
            "country_code": "GBR",
            "position": 1.0,
            "classified_position": "1",
            "status": "Finished",
            "points": 25.0,
        },
        {
            "driver_number": "7",
            "abbreviation": "BBB",
            "full_name": "Bob Brake",
            "team_name": "Boundary Racing",
            "country_code": "FRA",
            "position": 2.0,
            "classified_position": "R",
            "status": "Engine",
            "points": 0.0,
        },
    ]
    laps = [
        {
            "driver_number": "0",
            "lap_number": 1.0,
            "time": timedelta(seconds=90),
            "lap_start_time": timedelta(0),
            "lap_time": timedelta(seconds=90),
            "sector_1_time": timedelta(seconds=30),
            "sector_2_time": timedelta(seconds=29, milliseconds=500),
            "sector_3_time": timedelta(seconds=30, milliseconds=500),
            "stint": 1.0,
            "compound": "SOFT",
            "tyre_life": 1.0,
            "is_personal_best": True,
            "is_accurate": True,
            "fastf1_generated": False,
            "deleted": False,
            "deleted_reason": None,
            "track_status": "1",
            "pit_in_time": timedelta(seconds=85),
            "pit_out_time": None,
        },
        {
            "driver_number": "0",
            "lap_number": 2.0,
            "time": timedelta(seconds=181),
            "lap_start_time": timedelta(seconds=90),
            "lap_time": timedelta(seconds=91),
            "sector_1_time": None,
            "sector_2_time": None,
            "sector_3_time": None,
            "stint": 2.0,
            "compound": "C5",
            "tyre_life": 1.0,
            "is_personal_best": False,
            "is_accurate": None,
            "fastf1_generated": True,
            "deleted": True,
            "deleted_reason": "  TRACK LIMITS  ",
            "track_status": "12",
            "pit_in_time": None,
            "pit_out_time": timedelta(seconds=95),
        },
        {
            "driver_number": "7",
            "lap_number": 1.0,
            "time": timedelta(seconds=91),
            "lap_start_time": timedelta(0),
            "lap_time": None,
            "stint": 1.0,
            "compound": "HARD",
            "tyre_life": 4.0,
            "position": 2.0,
        },
    ]
    return {
        DatasetName.DRIVERS: drivers,
        DatasetName.LAPS: laps,
        DatasetName.WEATHER: [
            {
                "time": timedelta(seconds=10),
                "air_temp": 22.5,
                "track_temp": 31.0,
                "humidity": 54.0,
                "pressure": 1012.5,
                "rainfall": False,
                "wind_speed": 2.0,
                "wind_direction": 180.0,
            },
            {
                "time": timedelta(seconds=70),
                "air_temp": None,
                "track_temp": 31.5,
                "humidity": 55.0,
                "pressure": 1012.0,
                "rainfall": True,
                "wind_speed": 2.5,
                "wind_direction": 181.0,
            },
        ],
        DatasetName.RACE_CONTROL: [
            {
                "source_kind": "track_status",
                "session_time": timedelta(seconds=5),
                "status": "2",
            },
            {
                "source_kind": "session_status",
                "session_time": timedelta(seconds=6),
                "status": "Started",
            },
            {
                "source_kind": "race_control_message",
                "utc_time": datetime(2024, 7, 7, 14, 0, 7, tzinfo=UTC),
                "category": "Flag",
                "message": "RED FLAG",
                "flag": "RED",
                "scope": "Track",
                "lap": 1.0,
                "racing_number": "0",
            },
        ],
        DatasetName.RACE_POSITIONS: [
            {
                "time": timedelta(seconds=90),
                "driver_number": "0",
                "lap_number": 1.0,
                "position": 1.0,
            },
            {
                "time": timedelta(seconds=91),
                "driver_number": "7",
                "lap_number": 1.0,
                "position": 2.0,
            },
        ],
        DatasetName.TRACK_POSITIONS: [
            {
                "driver_number": "0",
                "session_time": timedelta(seconds=1),
                "x": 1000.0,
                "y": 2000.0,
                "z": 50.0,
                "status": "OnTrack",
                "source": "pos",
            },
            {
                "driver_number": "0",
                "session_time": timedelta(seconds=2),
                "x": 1010.0,
                "y": 2010.0,
                "z": 50.0,
                "status": "OffTrack",
                "source": "pos",
            },
        ],
        DatasetName.CAR_TELEMETRY: [
            {
                "driver_number": "0",
                "start_time": timedelta(seconds=1),
                "end_time": timedelta(seconds=180),
                "data_key": "fastf1-car-driver-0",
                "channel_names": ["Speed", "RPM", "nGear"],
                "sample_count": 1800,
            }
        ],
    }


def _provider_session(
    *,
    rows: dict[DatasetName, list[dict[str, object]]] | None = None,
    states: dict[DatasetName, DatasetAvailability] | None = None,
) -> ProviderSession:
    source_rows = rows or _base_rows()
    overrides = states or {}
    tables: dict[DatasetName, ProviderTable] = {}
    for name in DatasetName:
        if name in overrides:
            state = overrides[name]
            if state is DatasetAvailability.ERROR:
                tables[name] = ProviderTable(name=name, availability=state, error="fixture failure")
            elif state in {DatasetAvailability.NOT_REQUESTED, DatasetAvailability.UNSUPPORTED}:
                tables[name] = ProviderTable(name=name, availability=state)
            else:
                tables[name] = _table(name, [])
        else:
            tables[name] = _table(name, source_rows.get(name, []))
    return ProviderSession(
        session=SessionRef(2024, "Silverstone", "R"),
        provider_name="fastf1",
        provider_version="3.8.3",
        retrieved_at=datetime(2024, 7, 8, tzinfo=UTC),
        metadata={
            "event_name": "British Grand Prix",
            "session_name": "Race",
            "season": 2024,
            "round_number": 12,
            "country_code": "GBR",
            "circuit_name": "Silverstone Circuit",
            "scheduled_start_utc": datetime(2024, 7, 7, 14, tzinfo=UTC),
            "session_origin_utc": datetime(2024, 7, 7, 14, tzinfo=UTC),
            "provider_source_id": "2024-12-R",
            "requested_event": "Silverstone",
            "coordinate_scale_to_m": 0.1,
        },
        tables=tables,
    )


def _shuffle_rows(
    rows: dict[DatasetName, list[dict[str, object]]], seed: int
) -> dict[DatasetName, list[dict[str, object]]]:
    random = Random(seed)
    shuffled: dict[DatasetName, list[dict[str, object]]] = {}
    for name, values in rows.items():
        copied = list(values)
        random.shuffle(copied)
        shuffled[name] = copied
    return shuffled


def test_normalization_builds_canonical_records_without_final_result_leakage() -> None:
    normalized = normalize_session(_provider_session())
    session_id = make_session_id(2024, "British Grand Prix", SessionType.RACE)

    assert normalized.metadata.session_id == session_id
    assert normalized.metadata.event_name == "British Grand Prix"
    assert normalized.metadata.session_origin_utc == datetime(2024, 7, 7, 14, tzinfo=UTC)
    assert normalized.requested_session.event == "Silverstone"
    assert normalized.provider_metadata["requested_event"] == "Silverstone"
    assert normalized.completeness[DatasetName.WEATHER] is DatasetAvailability.AVAILABLE
    assert normalized.telemetry_materialized is False

    assert [driver.racing_number for driver in normalized.drivers] == [0, 7]
    assert normalized.drivers[0].driver_id == make_driver_id(session_id, 0)
    assert {field.name for field in fields(type(normalized.drivers[0]))}.isdisjoint(
        {"classified_position", "status", "points", "raw_status"}
    )
    assert [item.status for item in normalized.classifications] == [
        DriverStatus.FINISHED,
        DriverStatus.RETIRED,
    ]
    assert normalized.classifications[1].raw_status == "Engine"
    assert normalized.drivers[0].provenance.source != (
        normalized.classifications[0].provenance.source
    )

    lap = normalized.laps[1]
    assert lap.driver_id == make_driver_id(session_id, 0)
    assert lap.lap_number == 2
    assert lap.lap_time_ms == 91_000
    assert lap.compound is TyreCompound.UNKNOWN
    assert lap.raw_compound == "C5"
    assert lap.is_generated is True
    assert lap.is_deleted is True
    assert lap.deleted_reason == "TRACK LIMITS"
    assert lap.raw_track_status == "12"
    assert normalized.laps[2].lap_time_ms is None


def test_actual_session_start_requires_origin_and_observed_start_offset() -> None:
    source = _provider_session()
    provider_metadata = dict(source.metadata)
    provider_metadata.update(
        {
            "scheduled_start_utc": datetime(2024, 7, 7, 13, 55, tzinfo=UTC),
            "session_origin_utc": datetime(2024, 7, 7, 13, 59, 30, tzinfo=UTC),
            "session_start_time_ms": 30_000,
        }
    )

    def with_metadata(values: dict[str, object]) -> ProviderSession:
        return ProviderSession(
            session=source.session,
            provider_name=source.provider_name,
            provider_version=source.provider_version,
            retrieved_at=source.retrieved_at,
            metadata=values,
            tables=source.tables,
        )

    complete = normalize_session(with_metadata(provider_metadata))
    assert complete.metadata.scheduled_start_utc == datetime(2024, 7, 7, 13, 55, tzinfo=UTC)
    assert complete.metadata.session_start_utc == datetime(2024, 7, 7, 14, tzinfo=UTC)

    without_origin = dict(provider_metadata)
    without_origin.pop("session_origin_utc")
    without_offset = dict(provider_metadata)
    without_offset.pop("session_start_time_ms")
    assert normalize_session(with_metadata(without_origin)).metadata.session_start_utc is None
    assert normalize_session(with_metadata(without_offset)).metadata.session_start_utc is None


def test_stints_pits_weather_control_and_position_meanings_remain_distinct() -> None:
    normalized = normalize_session(_provider_session())

    stint_ranges = [
        (stint.stint_number, stint.start_lap, stint.end_lap) for stint in normalized.stints
    ]
    assert stint_ranges == [
        (1, 1, 1),
        (2, 2, 2),
        (1, 1, 1),
    ]
    assert len(normalized.pit_stops) == 1
    pit = normalized.pit_stops[0]
    assert pit.pit_in_time_ms == 85_000
    assert pit.pit_out_time_ms == 95_000
    assert pit.pit_lane_duration_ms == 10_000
    assert pit.stationary_duration_ms is None

    assert normalized.weather[1].air_temperature_c is None
    assert normalized.weather[1].track_temperature_c == 31.5
    assert [record.source_kind for record in normalized.race_control] == [
        "track_status",
        "session_status",
        "race_control_message",
    ]
    assert normalized.race_control[0].track_status is TrackStatus.YELLOW
    assert normalized.race_control[1].track_status is TrackStatus.UNKNOWN
    assert normalized.race_control[2].track_status is TrackStatus.RED_FLAG

    assert normalized.race_positions[0].position == 1
    assert normalized.race_positions[0].session_time_ms == 90_000
    assert isinstance(normalized.track_positions, CanonicalTrackPositions)
    assert normalized.track_positions.table.num_rows == 2
    assert normalized.track_positions[0].x_m == 100.0
    assert normalized.track_positions[0].y_m == 200.0
    assert not hasattr(normalized.race_positions[0], "x_m")
    assert not hasattr(normalized.track_positions[0], "position")

    index = normalized.telemetry_index[0]
    assert index.sample_count == 1800
    assert index.channel_names == ("RPM", "Speed", "nGear")
    assert index.data_key == "fastf1-car-driver-0"


def test_normalization_is_idempotent_and_independent_of_source_row_order() -> None:
    rows = _base_rows()
    rows[DatasetName.LAPS].append(dict(rows[DatasetName.LAPS][0]))
    first = normalize_session(_provider_session(rows=rows))
    second = normalize_session(_provider_session(rows=rows))
    shuffled = normalize_session(_provider_session(rows=_shuffle_rows(rows, seed=42)))

    assert first == second == shuffled
    assert len(first.laps) == 3
    assert [lap.provenance.source_record_id for lap in first.laps] == [
        lap.provenance.source_record_id for lap in shuffled.laps
    ]
    record_tables = (
        first.drivers,
        first.classifications,
        first.laps,
        first.stints,
        first.pit_stops,
        first.weather,
        first.race_control,
        first.race_positions,
        first.track_positions,
        first.telemetry_index,
    )
    assert all(
        record.provenance.source_record_id for records in record_tables for record in records
    )


def test_track_positions_keep_only_observed_pos_sources_deterministically() -> None:
    rows = _base_rows()
    rows[DatasetName.TRACK_POSITIONS] = [
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=0),
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "status": "OnTrack",
            "source": "POS",
        },
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=1),
            "x": 1000.0,
            "y": 2000.0,
            "z": 50.0,
            "status": "OnTrack",
            "source": "pos",
        },
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=2),
            "x": 2000.0,
            "y": 3000.0,
            "z": 60.0,
            "source": "interpolation",
        },
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=3),
            "x": 3000.0,
            "y": 4000.0,
            "z": 70.0,
            "source": "synthetic",
        },
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=4),
            "x": 4000.0,
            "y": 5000.0,
            "z": 80.0,
            "source": "padded",
        },
        {
            "driver_number": "0",
            "session_time": timedelta(seconds=5),
            "x": 5000.0,
            "y": 6000.0,
            "z": 90.0,
            "source": "mystery",
        },
    ]

    forward = normalize_session(_provider_session(rows=rows))
    reverse = normalize_session(_provider_session(rows=_shuffle_rows(rows, seed=9)))

    assert forward == reverse
    assert [record.session_time_ms for record in forward.track_positions] == [0, 1_000]
    assert (forward.track_positions[0].x_m, forward.track_positions[0].y_m) == (0.0, 0.0)
    source_warnings = [warning for warning in forward.warnings if "non-observed-source" in warning]
    assert len(source_warnings) == 4
    assert all("samples omitted" in warning for warning in source_warnings)


def test_conflicting_provider_duplicates_have_stable_winner_and_warning() -> None:
    rows = _base_rows()
    conflict = dict(rows[DatasetName.LAPS][0])
    conflict["compound"] = "MEDIUM"
    rows[DatasetName.LAPS].append(conflict)

    forward = normalize_session(_provider_session(rows=rows))
    reverse = normalize_session(_provider_session(rows=_shuffle_rows(rows, seed=7)))

    assert forward == reverse
    assert any("conflicting-duplicate" in warning for warning in forward.warnings)


def test_partial_pit_observations_remain_partial_and_warn() -> None:
    rows = _base_rows()
    rows[DatasetName.LAPS][1]["pit_out_time"] = None
    normalized = normalize_session(_provider_session(rows=rows))

    assert normalized.pit_stops[0].pit_in_time_ms == 85_000
    assert normalized.pit_stops[0].pit_out_time_ms is None
    assert normalized.pit_stops[0].pit_lane_duration_ms is None
    assert normalized.pit_stops[0].stationary_duration_ms is None
    assert any("partial-pit-stop" in warning for warning in normalized.warnings)


def test_pit_exit_pairs_with_most_recent_unmatched_entry_without_crossing() -> None:
    rows = _base_rows()
    driver_laps = [row for row in rows[DatasetName.LAPS] if row["driver_number"] == "0"]
    driver_laps[0]["pit_in_time"] = timedelta(seconds=80)
    driver_laps[0]["pit_out_time"] = None
    driver_laps[1]["pit_in_time"] = timedelta(seconds=170)
    driver_laps[1]["pit_out_time"] = None
    driver_laps.append(
        {
            "driver_number": "0",
            "lap_number": 3.0,
            "time": timedelta(seconds=270),
            "lap_start_time": timedelta(seconds=181),
            "lap_time": timedelta(seconds=89),
            "stint": 2.0,
            "compound": "C5",
            "pit_in_time": None,
            "pit_out_time": timedelta(seconds=180),
        }
    )
    rows[DatasetName.LAPS] = driver_laps + [
        row for row in rows[DatasetName.LAPS] if row["driver_number"] != "0"
    ]

    normalized = normalize_session(_provider_session(rows=rows))

    assert len(normalized.pit_stops) == 2
    older, latest = normalized.pit_stops
    assert (older.lap_number, older.pit_in_time_ms, older.pit_out_time_ms) == (
        1,
        80_000,
        None,
    )
    assert (latest.lap_number, latest.pit_in_time_ms, latest.pit_out_time_ms) == (
        2,
        170_000,
        180_000,
    )
    assert any("partial-pit-stop" in warning for warning in normalized.warnings)


def test_nonadjacent_pit_observations_across_interruption_remain_partial() -> None:
    rows = _base_rows()
    driver_laps = [row for row in rows[DatasetName.LAPS] if row["driver_number"] == "0"]
    driver_laps[0]["pit_in_time"] = timedelta(seconds=80)
    driver_laps[0]["pit_out_time"] = None
    driver_laps[1]["pit_in_time"] = None
    driver_laps[1]["pit_out_time"] = None
    driver_laps.append(
        {
            "driver_number": "0",
            "lap_number": 8.0,
            "time": timedelta(seconds=900),
            "lap_start_time": timedelta(seconds=810),
            "lap_time": timedelta(seconds=90),
            "stint": 2.0,
            "compound": "C5",
            "pit_in_time": None,
            "pit_out_time": timedelta(seconds=820),
        }
    )
    rows[DatasetName.LAPS] = driver_laps + [
        row for row in rows[DatasetName.LAPS] if row["driver_number"] != "0"
    ]

    normalized = normalize_session(_provider_session(rows=rows))

    assert len(normalized.pit_stops) == 2
    entry, exit_ = normalized.pit_stops
    assert (entry.pit_in_time_ms, entry.pit_out_time_ms) == (80_000, None)
    assert (exit_.pit_in_time_ms, exit_.pit_out_time_ms) == (None, 820_000)
    assert any("nonadjacent-observations" in warning for warning in normalized.warnings)
    assert sum("partial-pit-stop" in warning for warning in normalized.warnings) == 2


def test_observed_lap_start_date_uses_t0_only_when_relative_start_is_missing() -> None:
    rows = _base_rows()
    rows[DatasetName.LAPS][0]["lap_start_time"] = None
    rows[DatasetName.LAPS][0]["lap_start_date"] = datetime(
        2024,
        7,
        7,
        14,
        tzinfo=UTC,
    )

    normalized = normalize_session(_provider_session(rows=rows))

    assert normalized.laps[0].lap_start_time_ms == 0


def test_optional_dataset_states_are_preserved_without_becoming_unsupported() -> None:
    states = {
        DatasetName.WEATHER: DatasetAvailability.EMPTY,
        DatasetName.RACE_CONTROL: DatasetAvailability.ERROR,
        DatasetName.RACE_POSITIONS: DatasetAvailability.NOT_REQUESTED,
        DatasetName.TRACK_POSITIONS: DatasetAvailability.UNSUPPORTED,
        DatasetName.CAR_TELEMETRY: DatasetAvailability.NOT_REQUESTED,
    }
    normalized = normalize_session(_provider_session(states=states))

    assert normalized.weather == ()
    assert normalized.race_control == ()
    assert normalized.race_positions == ()
    assert normalized.track_positions == ()
    assert normalized.telemetry_index == ()
    assert dict(normalized.completeness) == {
        DatasetName.DRIVERS: DatasetAvailability.AVAILABLE,
        DatasetName.LAPS: DatasetAvailability.AVAILABLE,
        **states,
    }
    assert any("fixture failure" in warning for warning in normalized.warnings)


def test_absolute_control_message_without_session_origin_is_omitted_with_warning() -> None:
    source = _provider_session()
    metadata = dict(source.metadata)
    metadata.pop("session_origin_utc")
    without_origin = ProviderSession(
        session=source.session,
        provider_name=source.provider_name,
        provider_version=source.provider_version,
        retrieved_at=source.retrieved_at,
        metadata=metadata,
        tables=source.tables,
    )

    normalized = normalize_session(without_origin)

    assert [record.source_kind for record in normalized.race_control] == [
        "track_status",
        "session_status",
    ]
    assert any("unplaced-time" in warning for warning in normalized.warnings)


def test_unrostered_lap_reference_is_an_unsafe_validation_error() -> None:
    rows = _base_rows()
    rows[DatasetName.LAPS][0]["driver_number"] = "99"

    with pytest.raises(NormalizationError, match="unknown-driver-reference"):
        normalize_session(_provider_session(rows=rows))


@pytest.mark.parametrize(
    ("dataset", "state"),
    [
        (DatasetName.DRIVERS, DatasetAvailability.EMPTY),
        (DatasetName.LAPS, DatasetAvailability.ERROR),
        (DatasetName.LAPS, DatasetAvailability.NOT_REQUESTED),
    ],
)
def test_required_dataset_absence_raises_incomplete_error(
    dataset: DatasetName, state: DatasetAvailability
) -> None:
    with pytest.raises(SessionDataIncompleteError):
        normalize_session(_provider_session(states={dataset: state}))
