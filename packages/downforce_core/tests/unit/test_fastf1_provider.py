from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import downforce_core.providers.fastf1_tables as fastf1_tables
import fastf1  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.domain.enums import DriverStatus
from downforce_core.domain.identifiers import make_driver_id
from downforce_core.exceptions import ProviderUnavailableError, SessionNotFoundError
from downforce_core.normalization import normalize_session
from downforce_core.normalization.laps import normalize_laps
from downforce_core.normalization.metadata import normalize_metadata
from downforce_core.normalization.observations import normalize_race_control
from downforce_core.providers.base import DatasetAvailability, DatasetName, LoadOptions, SessionRef
from downforce_core.providers.fastf1_provider import FastF1Provider


class _FakeSession:
    def __init__(self) -> None:
        self.event: dict[str, object] = {
            "RoundNumber": 12,
            "Country": "United Kingdom",
            "Location": "Silverstone",
            "OfficialEventName": "FORMULA 1 QATAR AIRWAYS BRITISH GRAND PRIX 2024",
        }
        self.name = "Race"
        self.date = pd.Timestamp("2024-07-07 13:55:00")
        self.api_path = "/static/2024/2024-07-07_British_Grand_Prix/2024-07-07_Race/"
        self.f1_api_support = True
        self.session_info: dict[str, object] = {
            "Meeting": {
                "Country": {"Code": "GBR", "Name": "United Kingdom"},
                "Circuit": {"ShortName": "Silverstone"},
                "Location": "Silverstone",
            },
            "Key": 9558,
        }
        self.session_start_time = timedelta(seconds=30)
        self._t0_date: pd.Timestamp | None = pd.Timestamp("2024-07-07 13:59:30")
        self.t0_accesses = 0
        self.total_laps = 52
        self.results = pd.DataFrame(
            {
                "DriverNumber": ["44"],
                "Abbreviation": ["HAM"],
                "FullName": ["Lewis Hamilton"],
                "TeamName": ["Mercedes"],
                "Position": [1.0],
                "Status": ["Finished"],
                "Undocumented": ["must be dropped"],
            }
        )
        self.laps = pd.DataFrame(
            {
                "Time": [timedelta(seconds=91)],
                "Driver": ["HAM"],
                "DriverNumber": ["44"],
                "LapTime": [timedelta(seconds=91)],
                "LapNumber": [1.0],
                "Stint": [1.0],
                "Compound": ["MEDIUM"],
                "Position": [1.0],
                "UnknownLapColumn": [123],
            }
        )
        self.weather_data = pd.DataFrame(
            {
                "Time": [timedelta(seconds=60)],
                "AirTemp": [18.2],
                "Humidity": [71.0],
                "Pressure": [1007.1],
                "Rainfall": [False],
                "TrackTemp": [26.4],
                "WindDirection": [235],
                "WindSpeed": [2.6],
            }
        )
        self.race_control_messages = pd.DataFrame(
            {
                "Time": [pd.Timestamp("2024-07-07 14:01:00")],
                "Category": ["Flag"],
                "Message": ["GREEN LIGHT - PIT EXIT OPEN"],
                "Status": [None],
                "Flag": ["GREEN"],
                "Scope": ["Track"],
                "Sector": [None],
                "RacingNumber": [None],
                "Lap": [1],
            }
        )
        self.track_status = pd.DataFrame(
            {
                "Time": [timedelta(seconds=31)],
                "Status": ["1"],
                "Message": ["AllClear"],
            }
        )
        self.session_status = pd.DataFrame(
            {
                "Time": [timedelta(seconds=30)],
                "Status": ["Started"],
            }
        )
        self.pos_data = {
            "44": pd.DataFrame(
                {
                    "Date": [pd.Timestamp("2024-07-07 14:01:00")],
                    "Time": [timedelta(seconds=90)],
                    "SessionTime": [timedelta(seconds=90)],
                    "X": [100.0],
                    "Y": [-20.0],
                    "Z": [3.0],
                    "Status": ["OnTrack"],
                    "Source": ["pos"],
                    "Undocumented": ["must be dropped"],
                }
            )
        }
        self.car_data = {
            "44": pd.DataFrame(
                {
                    "Date": [
                        pd.Timestamp("2024-07-07 14:01:00"),
                        pd.Timestamp("2024-07-07 14:01:00.200"),
                    ],
                    "Time": [timedelta(seconds=90), timedelta(seconds=90.2)],
                    "SessionTime": [timedelta(seconds=90), timedelta(seconds=90.2)],
                    "Speed": [305.0, 307.0],
                    "RPM": [11_000.0, 11_100.0],
                    "nGear": [8, 8],
                    "Throttle": [100.0, 100.0],
                    "Brake": [False, False],
                    "DRS": [14, 14],
                    "Source": ["car", "car"],
                    "PrivateChannel": [1, 2],
                }
            )
        }
        self.load_calls: list[dict[str, bool]] = []
        self.load_thread: int | None = None

    @property
    def t0_date(self) -> pd.Timestamp | None:
        self.t0_accesses += 1
        if not self.load_calls or not self.load_calls[-1]["telemetry"]:
            raise fastf1.exceptions.DataNotLoadedError(
                "t0_date is only available after loading telemetry"
            )
        return self._t0_date

    def load(self, **kwargs: bool) -> None:
        self.load_thread = threading.get_ident()
        self.load_calls.append(kwargs)


def _install_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    fake_session: _FakeSession,
    *,
    get_session: Callable[..., object] | None = None,
) -> tuple[list[tuple[str, bool]], list[tuple[int, int | str, str, str]]]:
    cache_calls: list[tuple[str, bool]] = []
    get_calls: list[tuple[int, int | str, str, str]] = []

    def fake_enable_cache(path: str, *, force_renew: bool = False, **_: object) -> None:
        cache_calls.append((path, force_renew))

    def fake_get_session(
        season: int,
        event: int | str,
        session: str,
        *,
        backend: str,
    ) -> object:
        get_calls.append((season, event, session, backend))
        if get_session is not None:
            return get_session(season, event, session, backend=backend)
        return fake_session

    monkeypatch.setattr(fastf1.Cache, "enable_cache", fake_enable_cache)
    monkeypatch.setattr(fastf1, "get_session", fake_get_session)
    return cache_calls, get_calls


def test_adapter_uses_project_cache_fastf1_backend_and_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    cache_calls, get_calls = _install_fake_backend(monkeypatch, fake_session)
    main_thread = threading.get_ident()
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.DRIVERS, DatasetName.WEATHER})),
        )
    )

    expected_cache = tmp_path / ".downforce" / "cache" / "fastf1"
    assert expected_cache.is_dir()
    assert cache_calls == [(str(expected_cache), False)]
    assert get_calls == [(2024, 12, "R", "fastf1")]
    assert fake_session.load_calls == [
        {"laps": False, "telemetry": False, "weather": True, "messages": False}
    ]
    assert fake_session.load_thread is not None and fake_session.load_thread != main_thread
    assert provider.name == loaded.provider_name == "fastf1"
    assert provider.version == loaded.provider_version == "3.8.3"
    capabilities = provider.capabilities
    assert (
        capabilities.drivers,
        capabilities.laps,
        capabilities.weather,
        capabilities.race_control,
        capabilities.race_positions,
        capabilities.track_positions,
        capabilities.car_telemetry,
        capabilities.live,
    ) == (True, True, True, True, True, True, True, False)


def test_adapter_returns_explicit_owned_stable_tables_and_scalar_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(provider.load_session(SessionRef(2024, "British", "Race")))

    assert set(loaded.tables) == set(DatasetName)
    assert all(
        table.availability is DatasetAvailability.AVAILABLE for table in loaded.tables.values()
    )
    drivers = loaded.table(DatasetName.DRIVERS).data
    laps = loaded.table(DatasetName.LAPS).data
    assert drivers is not None and laps is not None
    assert "Undocumented" not in drivers.column_names
    assert "undocumented" not in drivers.column_names
    assert "unknown_lap_column" not in laps.column_names
    assert drivers.schema.metadata is None
    assert drivers.column_names[:4] == [
        "driver_number",
        "broadcast_name",
        "abbreviation",
        "driver_id",
    ]

    fake_session.results.loc[0, "FullName"] = "mutated after return"
    assert drivers.column("full_name").to_pylist() == ["Lewis Hamilton"]

    assert loaded.retrieved_at.tzinfo is UTC
    assert loaded.metadata["scheduled_start_utc"] == datetime(2024, 7, 7, 13, 55, tzinfo=UTC)
    assert loaded.metadata["session_start_time_ms"] == 30_000
    assert loaded.metadata["session_origin_utc"] == datetime(2024, 7, 7, 13, 59, 30, tzinfo=UTC)
    assert loaded.metadata["event_name"] == "FORMULA 1 QATAR AIRWAYS BRITISH GRAND PRIX 2024"
    assert loaded.metadata["requested_event"] == "British"
    assert loaded.metadata["coordinate_scale_to_m"] == 0.1
    assert all(
        table.data is None or isinstance(table.data, pa.Table) for table in loaded.tables.values()
    )
    assert not any(
        isinstance(value, (_FakeSession, pd.DataFrame)) for value in loaded.metadata.values()
    )


def test_adapter_combines_control_sources_and_derives_lap_end_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    options = LoadOptions(
        datasets=frozenset({DatasetName.RACE_CONTROL, DatasetName.RACE_POSITIONS})
    )

    loaded = asyncio.run(provider.load_session(SessionRef(2024, 12, "R"), options))

    control = loaded.table(DatasetName.RACE_CONTROL).data
    positions = loaded.table(DatasetName.RACE_POSITIONS).data
    assert control is not None and positions is not None
    assert set(control.column("source_kind").to_pylist()) == {
        "race_control_message",
        "track_status",
        "session_status",
    }
    assert str(control.schema.field("utc_time").type) == "timestamp[ns, tz=UTC]"
    assert loaded.metadata["session_origin_utc"] == datetime(2024, 7, 7, 13, 59, 30, tzinfo=UTC)
    control_rows = control.to_pylist()
    message_row = next(row for row in control_rows if row["source_kind"] == "race_control_message")
    assert message_row["session_time"] == timedelta(seconds=90)
    message_time = message_row["utc_time"]
    assert isinstance(message_time, datetime)
    assert message_time.tzinfo is not None and message_time.utcoffset() == timedelta(0)
    assert positions.to_pydict() == {
        "time": [timedelta(seconds=91)],
        "driver_number": ["44"],
        "lap_number": [1.0],
        "position": [1.0],
    }
    assert fake_session.load_calls[-1] == {
        "laps": True,
        "telemetry": True,
        "weather": False,
        "messages": True,
    }


def test_race_control_only_uses_loaded_t0_to_normalize_absolute_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    raw = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.RACE_CONTROL})),
        )
    )
    metadata = normalize_metadata(raw)
    warnings: list[str] = []
    normalized = normalize_race_control(raw, metadata, {}, warnings)

    assert fake_session.load_calls[-1] == {
        "laps": True,
        "telemetry": True,
        "weather": False,
        "messages": True,
    }
    assert fake_session.t0_accesses == 1
    assert raw.table(DatasetName.TRACK_POSITIONS).availability is (
        DatasetAvailability.NOT_REQUESTED
    )
    assert raw.table(DatasetName.CAR_TELEMETRY).availability is (DatasetAvailability.NOT_REQUESTED)
    assert metadata.session_origin_utc == datetime(2024, 7, 7, 13, 59, 30, tzinfo=UTC)
    absolute_message = next(
        record for record in normalized if record.source_kind == "race_control_message"
    )
    assert absolute_message.session_time_ms == 90_000
    assert warnings == []


def test_race_control_only_without_t0_omits_absolute_message_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    fake_session._t0_date = None
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    raw = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.RACE_CONTROL})),
        )
    )
    metadata = normalize_metadata(raw)
    warnings = list(cast(tuple[str, ...], raw.metadata["warnings"]))
    normalized = normalize_race_control(raw, metadata, {}, warnings)

    assert raw.metadata["scheduled_start_utc"] == datetime(2024, 7, 7, 13, 55, tzinfo=UTC)
    assert metadata.session_origin_utc is None
    assert {record.source_kind for record in normalized} == {
        "track_status",
        "session_status",
    }
    assert any("session origin unavailable" in warning for warning in warnings)
    assert any("race_control.unplaced-time" in warning for warning in warnings)


def test_laps_without_race_control_load_messages_for_deletion_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CorrectedLapsSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.race_control_messages = pd.DataFrame(
                {
                    "Message": [
                        "CAR 44 LAP TIME 1:31.000 DELETED - TRACK LIMITS AT TURN 4",
                        "CAR 44 LAP TIME 1:32.000 DELETED - TRACK LIMITS AT TURN 4",
                        "CAR 44 LAP TIME 1:32.000 REINSTATED",
                    ]
                }
            )
            self.laps = pd.DataFrame(
                {
                    "Time": [timedelta(seconds=91), timedelta(seconds=183)],
                    "Driver": ["HAM", "HAM"],
                    "DriverNumber": ["44", "44"],
                    "LapTime": [timedelta(seconds=91), timedelta(seconds=92)],
                    "LapNumber": [1.0, 2.0],
                    "Stint": [1.0, 1.0],
                    "LapStartTime": [timedelta(0), timedelta(seconds=91)],
                    "Compound": ["MEDIUM", "MEDIUM"],
                    "Position": [1.0, 1.0],
                }
            )

        def load(self, **kwargs: bool) -> None:
            super().load(**kwargs)
            if kwargs["messages"]:
                # FastF1 applies race-control deletion and reinstatement messages while loading.
                self.laps["Deleted"] = [True, False]
                self.laps["DeletedReason"] = ["TRACK LIMITS AT TURN 4", ""]

    fake_session = CorrectedLapsSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    raw = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.LAPS})),
        )
    )
    metadata = normalize_metadata(raw)
    normalized, _ = normalize_laps(
        raw,
        metadata,
        {44: make_driver_id(metadata.session_id, 44)},
        [],
    )

    assert fake_session.load_calls[-1] == {
        "laps": True,
        "telemetry": False,
        "weather": False,
        "messages": True,
    }
    assert fake_session.t0_accesses == 0
    assert raw.table(DatasetName.RACE_CONTROL).availability is DatasetAvailability.NOT_REQUESTED
    assert "session_origin_utc" not in raw.metadata
    assert not any(
        "session origin unavailable" in warning
        for warning in cast(tuple[str, ...], raw.metadata["warnings"])
    )
    assert [lap.is_deleted for lap in normalized] == [True, False]
    assert [lap.deleted_reason for lap in normalized] == [
        "TRACK LIMITS AT TURN 4",
        None,
    ]


def test_race_positions_only_loads_lap_corrections_without_probing_t0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    raw = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.RACE_POSITIONS})),
        )
    )

    assert fake_session.load_calls[-1] == {
        "laps": True,
        "telemetry": False,
        "weather": False,
        "messages": True,
    }
    assert fake_session.t0_accesses == 0
    assert raw.table(DatasetName.RACE_POSITIONS).availability is DatasetAvailability.AVAILABLE
    assert "session_origin_utc" not in raw.metadata
    assert not any(
        "session origin unavailable" in warning
        for warning in cast(tuple[str, ...], raw.metadata["warnings"])
    )


def test_adapter_emits_raw_position_samples_and_car_index_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    options = LoadOptions(
        datasets=frozenset({DatasetName.TRACK_POSITIONS, DatasetName.CAR_TELEMETRY})
    )

    loaded = asyncio.run(provider.load_session(SessionRef(2024, 12, "R"), options))

    positions = loaded.table(DatasetName.TRACK_POSITIONS).data
    telemetry = loaded.table(DatasetName.CAR_TELEMETRY).data
    assert positions is not None and telemetry is not None
    assert positions.column_names == [
        "driver_number",
        "date",
        "time",
        "session_time",
        "x",
        "y",
        "z",
        "status",
        "source",
    ]
    assert positions.column("x").to_pylist() == [100.0]
    assert positions.column("source").to_pylist() == ["pos"]
    assert telemetry.column_names == [
        "driver_number",
        "start_time",
        "end_time",
        "data_key",
        "channel_names",
        "sample_count",
    ]
    assert telemetry.column("sample_count").to_pylist() == [2]
    assert telemetry.column("channel_names").to_pylist() == [
        [
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
        ]
    ]
    assert "speed" not in telemetry.column_names


def test_car_index_counts_valid_timestamps_and_falls_back_per_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    fake_session.car_data["44"] = pd.DataFrame(
        {
            "SessionTime": [timedelta(seconds=90), None, "invalid"],
            "Time": [timedelta(seconds=89), timedelta(seconds=91), "also-invalid"],
            "Speed": [300.0, 301.0, 302.0],
            "Source": ["car", "car", "car"],
        }
    )
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.CAR_TELEMETRY})),
        )
    )

    telemetry = loaded.table(DatasetName.CAR_TELEMETRY)
    assert telemetry.availability is DatasetAvailability.AVAILABLE
    assert telemetry.data is not None
    assert telemetry.data.column("sample_count").to_pylist() == [2]
    assert telemetry.data.column("start_time").to_pylist() == [timedelta(seconds=90)]
    assert telemetry.data.column("end_time").to_pylist() == [timedelta(seconds=91)]
    warnings = cast(tuple[str, ...], loaded.metadata["warnings"])
    assert any("discarded 1" in warning and "invalid" in warning for warning in warnings)


def test_car_index_with_no_valid_timestamp_is_dataset_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    fake_session.car_data["44"] = pd.DataFrame(
        {
            "SessionTime": [None, "invalid"],
            "Time": [None, "also-invalid"],
            "Speed": [300.0, 301.0],
        }
    )
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.CAR_TELEMETRY})),
        )
    )

    telemetry = loaded.table(DatasetName.CAR_TELEMETRY)
    assert telemetry.availability is DatasetAvailability.ERROR
    assert telemetry.error is not None and "no valid" in telemetry.error


def test_pre_2020_track_positions_are_unsupported_without_loading_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    options = LoadOptions(datasets=frozenset({DatasetName.TRACK_POSITIONS}))

    loaded = asyncio.run(provider.load_session(SessionRef(2019, 1, "R"), options))

    assert loaded.table(DatasetName.TRACK_POSITIONS).availability is DatasetAvailability.UNSUPPORTED
    assert fake_session.load_calls[-1]["telemetry"] is False
    warnings = cast(tuple[str, ...], loaded.metadata["warnings"])
    assert any("2020" in warning for warning in warnings)


def test_property_failure_is_scoped_to_its_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenWeatherSession(_FakeSession):
        @property
        def weather_data(self) -> pd.DataFrame:
            raise RuntimeError("weather endpoint missing")

        @weather_data.setter
        def weather_data(self, _: pd.DataFrame) -> None:
            pass

    fake_session = BrokenWeatherSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    options = LoadOptions(datasets=frozenset({DatasetName.DRIVERS, DatasetName.WEATHER}))

    loaded = asyncio.run(provider.load_session(SessionRef(2024, 12, "R"), options))

    assert loaded.table(DatasetName.DRIVERS).availability is DatasetAvailability.AVAILABLE
    weather = loaded.table(DatasetName.WEATHER)
    assert weather.availability is DatasetAvailability.ERROR
    assert weather.error is not None and "weather endpoint missing" in weather.error
    assert loaded.table(DatasetName.LAPS).availability is DatasetAvailability.NOT_REQUESTED


@pytest.mark.parametrize(
    ("dataset", "extractor"),
    [
        (DatasetName.RACE_CONTROL, "_extract_race_control"),
        (DatasetName.RACE_POSITIONS, "_extract_race_positions"),
        (DatasetName.TRACK_POSITIONS, "_extract_track_positions"),
        (DatasetName.CAR_TELEMETRY, "_extract_car_index"),
    ],
)
def test_unexpected_extractor_failure_is_scoped_to_requested_dataset(
    dataset: DatasetName,
    extractor: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)

    def fail_extraction(*_: object, **__: object) -> object:
        raise RuntimeError("malformed dataset payload")

    monkeypatch.setattr(fastf1_tables, extractor, fail_extraction)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.DRIVERS, dataset})),
        )
    )

    assert loaded.table(DatasetName.DRIVERS).availability is DatasetAvailability.AVAILABLE
    failed = loaded.table(dataset)
    assert failed.availability is DatasetAvailability.ERROR
    assert failed.error is not None and "malformed dataset payload" in failed.error


def test_empty_requested_frame_is_empty_with_a_stable_arrow_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    fake_session.weather_data = pd.DataFrame()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(
        provider.load_session(
            SessionRef(2024, 12, "R"),
            LoadOptions(datasets=frozenset({DatasetName.WEATHER})),
        )
    )

    weather = loaded.table(DatasetName.WEATHER)
    assert weather.availability is DatasetAvailability.EMPTY
    assert weather.data is not None and weather.data.num_rows == 0
    assert weather.data.column_names == [
        "time",
        "air_temp",
        "humidity",
        "pressure",
        "rainfall",
        "track_temp",
        "wind_direction",
        "wind_speed",
    ]


def test_race_control_keeps_successful_sources_when_one_property_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PartialControlSession(_FakeSession):
        @property
        def track_status(self) -> pd.DataFrame:
            raise RuntimeError("track status unavailable")

        @track_status.setter
        def track_status(self, _: pd.DataFrame) -> None:
            pass

    fake_session = PartialControlSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    options = LoadOptions(datasets=frozenset({DatasetName.RACE_CONTROL}))

    loaded = asyncio.run(provider.load_session(SessionRef(2024, 12, "R"), options))

    control = loaded.table(DatasetName.RACE_CONTROL)
    assert control.availability is DatasetAvailability.AVAILABLE
    assert control.data is not None
    assert set(control.data.column("source_kind").to_pylist()) == {
        "race_control_message",
        "session_status",
    }
    warnings = cast(tuple[str, ...], loaded.metadata["warnings"])
    assert any("track status unavailable" in warning for warning in warnings)


def test_concurrent_loads_are_serialized_and_do_not_reconfigure_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ConcurrentSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.active_loads = 0
            self.max_active_loads = 0
            self.counter_lock = threading.Lock()

        def load(self, **kwargs: bool) -> None:
            with self.counter_lock:
                self.active_loads += 1
                self.max_active_loads = max(self.max_active_loads, self.active_loads)
            try:
                time.sleep(0.02)
                super().load(**kwargs)
            finally:
                with self.counter_lock:
                    self.active_loads -= 1

    fake_session = ConcurrentSession()
    cache_calls, _ = _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)
    reference = SessionRef(2024, 12, "R")
    options = LoadOptions(datasets=frozenset({DatasetName.DRIVERS}))

    async def load_both() -> None:
        await asyncio.gather(
            provider.load_session(reference, options),
            provider.load_session(reference, options),
        )

    asyncio.run(load_both())

    assert fake_session.max_active_loads == 1
    assert len(fake_session.load_calls) == 2
    assert cache_calls == [(str(provider.cache_path), False)]


def test_force_refresh_is_forwarded_to_fastf1_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    cache_calls, _ = _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    reference = SessionRef(2024, 12, "R")
    options = LoadOptions(
        datasets=frozenset({DatasetName.DRIVERS}),
        force_refresh=True,
    )
    asyncio.run(provider.load_session(reference, options))
    asyncio.run(provider.load_session(reference, options))

    assert [force_refresh for _, force_refresh in cache_calls] == [False, True, True]


def test_backend_lookup_and_load_failures_map_to_typed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()

    def missing(*_: object, **__: object) -> object:
        raise ValueError("Session type 'R' does not exist for this event")

    _install_fake_backend(monkeypatch, fake_session, get_session=missing)
    provider = FastF1Provider(tmp_path)
    with pytest.raises(SessionNotFoundError, match="does not exist"):
        asyncio.run(provider.load_session(SessionRef(2024, 12, "R")))

    class BrokenLoadSession(_FakeSession):
        def load(self, **_: bool) -> None:
            raise RuntimeError("upstream is offline")

    broken = BrokenLoadSession()
    _install_fake_backend(monkeypatch, broken)
    provider = FastF1Provider(tmp_path)
    with pytest.raises(ProviderUnavailableError, match="upstream is offline"):
        asyncio.run(provider.load_session(SessionRef(2024, 12, "R")))


def test_non_api_session_marks_requested_detail_datasets_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    fake_session.f1_api_support = False
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    loaded = asyncio.run(provider.load_session(SessionRef(1960, 1, "R")))

    assert loaded.table(DatasetName.DRIVERS).availability is DatasetAvailability.AVAILABLE
    for name in set(DatasetName) - {DatasetName.DRIVERS}:
        assert loaded.table(name).availability is DatasetAvailability.UNSUPPORTED


def test_adapter_snapshot_normalizes_end_to_end_without_materializing_car_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_session = _FakeSession()
    _install_fake_backend(monkeypatch, fake_session)
    provider = FastF1Provider(tmp_path)

    raw = asyncio.run(provider.load_session(SessionRef(2024, "British", "Race")))
    normalized = normalize_session(raw)

    assert normalized.metadata.event_name == ("FORMULA 1 QATAR AIRWAYS BRITISH GRAND PRIX 2024")
    assert normalized.metadata.scheduled_start_utc == datetime(2024, 7, 7, 13, 55, tzinfo=UTC)
    assert normalized.metadata.session_start_utc == datetime(2024, 7, 7, 14, tzinfo=UTC)
    assert normalized.drivers[0].racing_number == 44
    assert normalized.classifications[0].status is DriverStatus.FINISHED
    assert normalized.laps[0].lap_time_ms == 91_000
    assert normalized.weather[0].session_time_ms == 60_000
    assert {record.source_kind for record in normalized.race_control} == {
        "race_control_message",
        "track_status",
        "session_status",
    }
    assert normalized.race_positions[0].position == 1
    assert normalized.track_positions[0].x_m == 10.0
    assert normalized.telemetry_index[0].sample_count == 2
    assert normalized.telemetry_materialized is False
