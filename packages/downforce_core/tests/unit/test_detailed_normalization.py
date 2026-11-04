import math

import pyarrow as pa
import pytest
from downforce_core.detailed.normalization import normalize_table


def test_car_normalization_preserves_every_native_value_and_sample() -> None:
    original = pa.table(
        {
            "source_index": [2, 3],
            "SessionTime": pa.array([1234567, 2234567], type=pa.duration("ns")),
            "Speed": [10.0000000000001, -2.5],
            "nGear": [1.5, 8.0],
            "Throttle": [101.0, None],
            "Brake": [True, None],
            "DRS": [42, 12],
            "new_provider_column": ["keep", "everything"],
        }
    )
    result, metadata = normalize_table("car_data/44", original)
    assert result.num_rows == original.num_rows
    assert result["speed"].to_pylist() == original["Speed"].to_pylist()
    assert result["gear"].to_pylist() == [1.5, 8.0]
    assert result["session_time"].type == pa.duration("ns")
    assert result["session_time_ms"].to_pylist() == [1.234567, 2.234567]
    assert result["new_provider_column"].to_pylist() == ["keep", "everything"]
    assert all(value != 0 for value in result["quality_flags"].to_pylist())
    assert metadata["driver_number"] == "44"
    assert metadata["resampled"] is False


def test_position_uses_local_coordinates_and_missing_channels_are_unavailable() -> None:
    table, metadata = normalize_table("pos_data/44", pa.table({"X": [1], "Y": [2]}))
    assert table.column_names[:2] == ["x", "y"]
    assert metadata["coordinates"] == "local-track-position"
    assert "z" not in metadata["available_fields"]


def test_unknown_columns_and_nulls_keep_arrow_metadata() -> None:
    original = pa.table(
        {"LapTime": pa.array([None, 123456789], type=pa.duration("ns")), "custom": [None, math.pi]}
    ).replace_schema_metadata({b"native": b"test"})
    result, _ = normalize_table("laps", original)
    assert result["lap_time"].type == original["LapTime"].type
    assert result["lap_time"].to_pylist() == original["LapTime"].to_pylist()
    assert result.schema.metadata[b"native"] == b"test"


def test_provider_column_alias_collisions_fail_closed() -> None:
    with pytest.raises(ValueError, match="collision"):
        normalize_table("laps", pa.table({"LapTime": [1], "lap_time": [2]}))
