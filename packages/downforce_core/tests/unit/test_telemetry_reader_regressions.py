from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.telemetry.reader import TelemetryReader
from test_telemetry_reader import publish


def test_reader_catalog_only_active_production_and_laps_scoped(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path)
    service = TelemetryReader(store)
    assert service.sessions()[0]["revision"] == revision
    _, rows = service.laps(identity, "44", revision=revision)
    assert [row["lap_number"] for row in rows] == [1, 2]
    assert rows[0]["qualifying_phase"] is None


def test_reader_position_alignment_never_extrapolates_and_retains_source_sensor(
    tmp_path: Path,
) -> None:
    store, identity, revision = publish(tmp_path)
    manifest = store.manifest(identity)
    tables = {
        name: store.read_table(identity, name, revision=revision) for name in manifest["tables"]
    }
    tables["car-data-44"] = tables["car-data-44"].append_column(
        "provider_source", pa.array(["car"] * 23)
    )
    tables["pos-data-44"] = pa.table(
        {
            "session_time": pa.array([10_000_000_000, 12_000_000_000], pa.duration("ns")),
            "x": [1.0, 3.0],
            "y": [2.0, 4.0],
        }
    )
    revision = store.publish(identity, tables, manifest["metadata"])
    loaded = TelemetryReader(store).load_lap(identity, "44", 1, revision=revision)
    assert loaded.source.channels["x"][0] is None
    assert loaded.source.channels["x"][2] == 2.0
    assert loaded.source_sensors[0] == "car"
    assert loaded.channel_availability["z"] == "unavailable"


def test_reader_ambiguous_lap_is_not_arbitrarily_selected(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path)
    laps = store.read_table(identity, "laps")
    store.publish(
        identity,
        {
            "laps": pa.concat_tables([laps, laps.slice(0, 1)]),
            "car-data-44": store.read_table(identity, "car-data-44"),
        },
        store.manifest(identity)["metadata"],
    )
    with pytest.raises(ValueError, match="ambiguous"):
        TelemetryReader(store).load_lap(identity, "44", 1)


def test_reader_raw_provider_time_is_not_a_session_clock(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path)
    car = store.read_table(identity, "car-data-44").rename_columns(
        ["provider_time", "speed", "gear", "brake", "drs"]
    )
    store.publish(
        identity,
        {"laps": store.read_table(identity, "laps"), "car-data-44": car},
        store.manifest(identity)["metadata"],
    )
    with pytest.raises(ValueError):
        TelemetryReader(store).load_lap(identity, "44", 1)
