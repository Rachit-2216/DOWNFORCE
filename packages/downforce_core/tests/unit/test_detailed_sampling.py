import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow.parquet as pq
import pytest


def loaded_session() -> SimpleNamespace:
    return SimpleNamespace(
        results=pd.DataFrame({"DriverNumber": ["44"]}),
        laps=pd.DataFrame({"LapNumber": [1.0], "DriverNumber": ["44"]}),
        session_status=pd.DataFrame({"Status": ["Started", "Finalised"]}),
        track_status=pd.DataFrame({"Status": ["1"]}),
        weather_data=pd.DataFrame({"AirTemp": [23.125]}),
        race_control_messages=pd.DataFrame({"Message": ["TRACK CLEAR"]}),
        car_data={"44": pd.DataFrame({"Speed": [123.125, 125.5], "nGear": [4, 5]})},
        pos_data={},
    )


def test_capture_keeps_all_native_rows_and_discrete_values(tmp_path: Path) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    session = loaded_session()
    report = capture_native_tables(session, tmp_path)
    table = pq.read_table(tmp_path / "car_data/44.parquet")
    assert table.num_rows == 2
    assert table["nGear"].to_pylist() == [4, 5]
    assert table["Speed"].to_pylist() == [123.125, 125.5]
    assert report["tables"]["car_data/44"]["rows"] == 2
    assert report["channel_availability"]["pos_data"]["state"] == "unavailable"


def test_capture_rejects_partial_serialization_instead_of_publishing_biased_size(
    tmp_path: Path,
) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    session = loaded_session()
    session.car_data["44"]["unserializable"] = [object(), object()]
    with pytest.raises(ValueError, match="serialization"):
        capture_native_tables(session, tmp_path)
    assert not (tmp_path / "measurement.json").exists()
    assert json.loads((tmp_path / "capture-failures.json").read_text())["failures"]


def test_capture_requires_core_tables_and_car_observations(tmp_path: Path) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    session = loaded_session()
    session.car_data = {}
    with pytest.raises(ValueError, match="car telemetry"):
        capture_native_tables(session, tmp_path)


def test_capture_does_not_overwrite_existing_native_table(tmp_path: Path) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    session = loaded_session()
    capture_native_tables(session, tmp_path)
    original = (tmp_path / "car_data/44.parquet").read_bytes()
    capture_native_tables(session, tmp_path)
    assert (tmp_path / "car_data/44.parquet").read_bytes() == original
    session.car_data["44"].loc[0, "Speed"] = 999
    with pytest.raises(ValueError, match="existing"):
        capture_native_tables(session, tmp_path)
    assert (tmp_path / "car_data/44.parquet").read_bytes() == original


def test_nullable_circuit_info_is_explicit_optional_unavailability(tmp_path: Path) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    session = loaded_session()
    session.get_circuit_info = lambda: None
    report = capture_native_tables(session, tmp_path)
    assert report["channel_availability"]["corners"] == {
        "state": "unavailable",
        "reason": "provider returned no circuit information",
    }
    assert "corners" not in report["tables"]
    assert not (tmp_path / "corners.parquet").exists()


@pytest.mark.parametrize("stage", ["getter", "writer", "circuit"])
def test_permission_denial_stops_capture_without_later_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    denial = PermissionError(13, "controlled denial")
    session = loaded_session()
    accesses = []

    class Session:
        def __getattr__(self, name):
            accesses.append(name)
            if stage == "getter" and name == "results":
                raise denial
            if name == "get_circuit_info":

                def circuit():
                    raise denial

                return circuit
            return getattr(session, name)

    if stage == "writer":

        def denied(*_):
            raise denial

        monkeypatch.setattr("downforce_core.detailed.sampling.write_sample_table", denied)
    with pytest.raises(PermissionError) as caught:
        capture_native_tables(Session(), tmp_path)
    assert caught.value is denial
    if stage != "circuit":
        assert accesses == ["results"]
    assert not (tmp_path / "capture-failures.json").exists()


def test_native_denial_with_generic_cleanup_failure_stops_all_later_getters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed.sampling import capture_native_tables

    denial = PermissionError(13, "native sharing violation", "native-original.tmp")
    denial.winerror = 32
    cleanup_error = OSError(5, "cleanup failed", "cleanup.tmp")
    original_unlink = Path.unlink
    session = loaded_session()
    accesses = []

    class Session:
        def __getattr__(self, name):
            accesses.append(name)
            return getattr(session, name)

    def denied_write(*args, **kwargs):
        raise denial

    def denied_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise cleanup_error
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr("downforce_core.detailed.audit.pq.write_table", denied_write)
    monkeypatch.setattr(Path, "unlink", denied_cleanup)
    with pytest.raises(PermissionError) as caught:
        capture_native_tables(Session(), tmp_path)
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert caught.value.filename == "native-original.tmp"
    assert accesses == ["results"]
    assert not (tmp_path / "capture-failures.json").exists()
    assert any("cleanup.tmp" in note for note in getattr(denial, "__notes__", []))
