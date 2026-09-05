from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from downforce_core.domain import (
    DataQuality,
    DriverClassificationRecord,
    DriverRecord,
    DriverStatus,
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
    SessionMetadata,
    SessionType,
    SourceProvenance,
    StintRecord,
    TrackStatus,
    TyreCompound,
    WeatherRecord,
    make_driver_id,
    make_session_id,
)
from downforce_core.domain.events import RaceEvent, RaceEventType
from downforce_core.domain.state import ReplayDriverStatus
from downforce_core.exceptions import ReplayCursorError
from downforce_core.normalization.models import CanonicalTrackPositions, NormalizedSession
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    SessionRef,
)
from downforce_core.replay import CanonicalTimeline, ReplayEngine, build_timeline, state_to_dict


def _provenance(source: str = "fixture") -> SourceProvenance:
    return SourceProvenance(
        provider="fixture",
        provider_version="1.0",
        source=source,
        retrieved_at=datetime(2024, 1, 2, tzinfo=UTC),
    )


def _session() -> NormalizedSession:
    session_id = make_session_id(2024, "Replay Grand Prix", SessionType.RACE)
    first = make_driver_id(session_id, 1)
    second = make_driver_id(session_id, 2)
    metadata = SessionMetadata(
        session_id=session_id,
        season=2024,
        event_name="Replay Grand Prix",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=_provenance("fixture.metadata"),
        round_number=1,
        session_origin_utc=datetime(2024, 1, 1, tzinfo=UTC),
        data_quality=DataQuality.COMPLETE,
    )
    drivers = (
        DriverRecord(
            session_id=session_id,
            driver_id=first,
            racing_number=1,
            abbreviation="AAA",
            full_name="Ada Apex",
            team_name="Analytical GP",
            provenance=_provenance("fixture.drivers"),
        ),
        DriverRecord(
            session_id=session_id,
            driver_id=second,
            racing_number=2,
            abbreviation="BBB",
            full_name="Bob Brake",
            team_name="Boundary Racing",
            provenance=_provenance("fixture.drivers"),
        ),
    )
    classifications = (
        DriverClassificationRecord(
            session_id=session_id,
            driver_id=first,
            classified_position=1,
            status=DriverStatus.FINISHED,
            provenance=_provenance("fixture.final-classification"),
        ),
        DriverClassificationRecord(
            session_id=session_id,
            driver_id=second,
            status=DriverStatus.RETIRED,
            provenance=_provenance("fixture.final-classification"),
        ),
    )
    laps = (
        LapRecord(
            session_id=session_id,
            driver_id=first,
            lap_number=1,
            lap_start_time_ms=0,
            lap_end_time_ms=90_000,
            lap_time_ms=90_000,
            stint_number=1,
            compound=TyreCompound.SOFT,
            tyre_life_laps=1,
            provenance=_provenance("fixture.laps"),
        ),
        LapRecord(
            session_id=session_id,
            driver_id=first,
            lap_number=2,
            lap_start_time_ms=90_000,
            lap_end_time_ms=180_000,
            lap_time_ms=90_000,
            stint_number=2,
            compound=TyreCompound.MEDIUM,
            tyre_life_laps=1,
            provenance=_provenance("fixture.laps"),
        ),
        LapRecord(
            session_id=session_id,
            driver_id=second,
            lap_number=1,
            lap_start_time_ms=0,
            lap_end_time_ms=91_000,
            lap_time_ms=91_000,
            stint_number=1,
            compound=TyreCompound.HARD,
            tyre_life_laps=3,
            provenance=_provenance("fixture.laps"),
        ),
    )
    stints = (
        StintRecord(
            session_id=session_id,
            driver_id=first,
            stint_number=1,
            start_lap=1,
            end_lap=1,
            start_time_ms=0,
            end_time_ms=90_000,
            compound=TyreCompound.SOFT,
            tyre_life_start_laps=0,
            provenance=_provenance("fixture.stints"),
        ),
        StintRecord(
            session_id=session_id,
            driver_id=first,
            stint_number=2,
            start_lap=2,
            end_lap=2,
            start_time_ms=95_000,
            end_time_ms=180_000,
            compound=TyreCompound.MEDIUM,
            tyre_life_start_laps=0,
            provenance=_provenance("fixture.stints"),
        ),
        StintRecord(
            session_id=session_id,
            driver_id=second,
            stint_number=1,
            start_lap=1,
            end_lap=1,
            start_time_ms=0,
            end_time_ms=91_000,
            compound=TyreCompound.HARD,
            tyre_life_start_laps=2,
            provenance=_provenance("fixture.stints"),
        ),
    )
    pit_stops = (
        PitStopRecord(
            session_id=session_id,
            driver_id=first,
            stop_number=1,
            lap_number=1,
            pit_in_time_ms=85_000,
            pit_out_time_ms=95_000,
            pit_lane_duration_ms=10_000,
            provenance=_provenance("fixture.pits"),
        ),
    )
    weather = (
        WeatherRecord(
            session_id=session_id,
            session_time_ms=10_000,
            air_temperature_c=20,
            rainfall=False,
            provenance=_provenance("fixture.weather"),
        ),
        WeatherRecord(
            session_id=session_id,
            session_time_ms=30_000,
            air_temperature_c=19,
            rainfall=True,
            provenance=_provenance("fixture.weather"),
        ),
    )
    controls = (
        RaceControlRecord(
            session_id=session_id,
            session_time_ms=5_000,
            message="TRACK CLEAR",
            track_status=TrackStatus.CLEAR,
            provenance=_provenance("fixture.control"),
        ),
        RaceControlRecord(
            session_id=session_id,
            session_time_ms=50_000,
            message="SAFETY CAR",
            track_status=TrackStatus.SAFETY_CAR,
            provenance=_provenance("fixture.control"),
        ),
        RaceControlRecord(
            session_id=session_id,
            session_time_ms=70_000,
            message="GREEN FLAG",
            track_status=TrackStatus.CLEAR,
            provenance=_provenance("fixture.control"),
        ),
    )
    positions = (
        RacePositionRecord(
            session_id=session_id,
            driver_id=first,
            session_time_ms=90_000,
            position=1,
            lap_number=1,
            provenance=_provenance("fixture.positions"),
        ),
        RacePositionRecord(
            session_id=session_id,
            driver_id=second,
            session_time_ms=91_000,
            position=2,
            lap_number=1,
            provenance=_provenance("fixture.positions"),
        ),
        RacePositionRecord(
            session_id=session_id,
            driver_id=first,
            session_time_ms=180_000,
            position=1,
            lap_number=2,
            provenance=_provenance("fixture.positions"),
        ),
    )
    capabilities = ProviderCapabilities(
        drivers=True,
        laps=True,
        weather=True,
        race_control=True,
        race_positions=True,
        track_positions=False,
        car_telemetry=False,
        live=False,
    )
    completeness = {
        name: (
            DatasetAvailability.UNSUPPORTED
            if name in {DatasetName.TRACK_POSITIONS, DatasetName.CAR_TELEMETRY}
            else DatasetAvailability.AVAILABLE
        )
        for name in DatasetName
    }
    return NormalizedSession(
        metadata=metadata,
        drivers=drivers,
        classifications=classifications,
        laps=laps,
        stints=stints,
        pit_stops=pit_stops,
        weather=weather,
        race_control=controls,
        race_positions=positions,
        track_positions=CanonicalTrackPositions.empty(
            session_id=session_id,
            provider_name="fixture",
            provider_version="1.0",
            retrieved_at=datetime(2024, 1, 2, tzinfo=UTC),
            source="fixture.track-positions",
        ),
        telemetry_index=(),
        capabilities=capabilities,
        completeness=completeness,
        warnings=(),
        provider_name="fixture",
        provider_version="1.0",
        retrieved_at=datetime(2024, 1, 2, tzinfo=UTC),
        provider_metadata={},
        requested_session=SessionRef(2024, 1, "R"),
    )


def test_state_reconstructs_weather_control_laps_position_stint_and_pit() -> None:
    session = _session()
    engine = ReplayEngine(session, checkpoint_interval=2)
    first_driver = session.drivers[0].driver_id

    early = engine.state_at(20_000)
    assert early.weather is not None
    assert early.weather.observed_at_ms == 10_000
    assert early.weather.rainfall is False
    assert early.track_status is TrackStatus.CLEAR
    assert early.drivers[first_driver].compound is TyreCompound.SOFT
    assert early.drivers[first_driver].status is ReplayDriverStatus.ACTIVE
    assert early.drivers[first_driver].laps_completed == 0

    assert engine.state_at(84_999).drivers[first_driver].in_pit is False
    at_entry = engine.state_at(85_000).drivers[first_driver]
    assert at_entry.in_pit is True
    assert at_entry.status is ReplayDriverStatus.IN_PIT
    assert at_entry.pit_stop_count == 1
    at_exit = engine.state_at(95_000).drivers[first_driver]
    assert at_exit.in_pit is False
    assert at_exit.status is ReplayDriverStatus.ACTIVE
    assert at_exit.compound is TyreCompound.MEDIUM

    lap_end = engine.state_at_lap(1, phase="end")
    assert lap_end.reference_lap == 1
    assert lap_end.drivers[first_driver].laps_completed == 1
    assert lap_end.drivers[first_driver].position == 1
    assert engine.state_at_lap(1, phase="start").session_time_ms == 0


def test_future_events_weather_pits_compounds_and_classification_cannot_leak() -> None:
    session = _session()
    timeline = build_timeline(session)
    cursor = 80_000
    baseline = ReplayEngine(session, timeline).state_at(cursor)
    driver_id = session.drivers[0].driver_id
    future = RaceEvent(
        session_id=session.metadata.session_id,
        session_time_ms=999_999,
        sequence=len(timeline.events),
        event_type=RaceEventType.DRIVER_STATUS_CHANGED,
        driver_id=driver_id,
        source="fixture.future",
        source_key="future-retirement",
        payload={"status": ReplayDriverStatus.RETIRED.value},
    )
    extended = CanonicalTimeline(
        session_id=timeline.session_id,
        events=timeline.events + (future,),
        lap_cursors=timeline.lap_cursors,
    )
    replayed = ReplayEngine(session, extended).state_at(cursor)

    assert replayed == baseline
    assert replayed.weather is not None and replayed.weather.observed_at_ms == 30_000
    assert replayed.drivers[driver_id].pit_stop_count == 0
    assert replayed.drivers[driver_id].compound is TyreCompound.SOFT
    assert replayed.drivers[driver_id].status is ReplayDriverStatus.ACTIVE
    assert session.classifications[0].status is DriverStatus.FINISHED


def test_terminal_status_cannot_be_reactivated_by_later_malformed_lap() -> None:
    session = _session()
    driver_id = session.drivers[0].driver_id
    events = (
        RaceEvent(
            session_id=session.metadata.session_id,
            session_time_ms=100,
            sequence=0,
            event_type=RaceEventType.DRIVER_STATUS_CHANGED,
            driver_id=driver_id,
            source="fixture",
            source_key="retired",
            payload={"status": ReplayDriverStatus.RETIRED.value},
        ),
        RaceEvent(
            session_id=session.metadata.session_id,
            session_time_ms=200,
            sequence=1,
            event_type=RaceEventType.DRIVER_LAP_COMPLETED,
            driver_id=driver_id,
            source="fixture",
            source_key="bad-lap",
            payload={
                "lap_number": 99,
                "lap_time_ms": 1,
                "stint_number": 1,
                "compound": TyreCompound.SOFT.value,
                "tyre_age_laps": 99,
            },
        ),
    )
    timeline = CanonicalTimeline(
        session_id=session.metadata.session_id,
        events=events,
        lap_cursors={},
    )
    state = ReplayEngine(session, timeline).state_at(200)
    assert state.drivers[driver_id].status is ReplayDriverStatus.RETIRED


def test_timeline_and_state_serialization_are_input_order_independent() -> None:
    session = _session()
    reversed_session = replace(
        session,
        laps=tuple(reversed(session.laps)),
        stints=tuple(reversed(session.stints)),
        weather=tuple(reversed(session.weather)),
        race_control=tuple(reversed(session.race_control)),
        race_positions=tuple(reversed(session.race_positions)),
    )
    first = build_timeline(session)
    second = build_timeline(reversed_session)
    assert first.events == second.events
    simultaneous = [event.event_type for event in first.events if event.session_time_ms == 90_000]
    assert simultaneous.index(RaceEventType.DRIVER_POSITION_CHANGED) < simultaneous.index(
        RaceEventType.DRIVER_LAP_COMPLETED
    )

    engine = ReplayEngine(session, first, checkpoint_interval=2)
    first_json = json.dumps(state_to_dict(engine.state_at(95_000)), sort_keys=True)
    second_json = json.dumps(state_to_dict(engine.state_at(95_000)), sort_keys=True)
    assert first_json == second_json


def test_red_flag_and_restart_track_status_transitions_are_cursor_exact() -> None:
    session = _session()
    events = tuple(
        RaceEvent(
            session_id=session.metadata.session_id,
            session_time_ms=time_ms,
            sequence=sequence,
            event_type=RaceEventType.TRACK_STATUS_CHANGED,
            source="fixture.control",
            source_key=f"status-{sequence}",
            payload={"track_status": status.value},
        )
        for sequence, (time_ms, status) in enumerate(
            (
                (10, TrackStatus.CLEAR),
                (20, TrackStatus.RED_FLAG),
                (30, TrackStatus.CLEAR),
            )
        )
    )
    timeline = CanonicalTimeline(
        session_id=session.metadata.session_id,
        events=events,
        lap_cursors={},
    )
    engine = ReplayEngine(session, timeline)

    assert engine.state_at(19).track_status is TrackStatus.CLEAR
    assert engine.state_at(20).track_status is TrackStatus.RED_FLAG
    assert engine.state_at(30).track_status is TrackStatus.CLEAR


def test_invalid_and_ambiguous_cursors_fail_clearly() -> None:
    session = _session()
    engine = ReplayEngine(session)
    with pytest.raises(ReplayCursorError, match="nonnegative"):
        engine.state_at(-1)
    with pytest.raises(ReplayCursorError, match="exceeds"):
        engine.state_at(engine.timeline.max_time_ms + 1)
    with pytest.raises(ReplayCursorError, match="phase"):
        engine.state_at_lap(1, phase="middle")
    with pytest.raises(ReplayCursorError, match="no unambiguous"):
        engine.state_at_lap(99)

    no_positions = replace(session, race_positions=())
    no_mapping = ReplayEngine(no_positions)
    with pytest.raises(ReplayCursorError, match="no unambiguous"):
        no_mapping.state_at_lap(1)
