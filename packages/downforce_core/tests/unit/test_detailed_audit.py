"""Provider inventory and sample reports must survive independent failures."""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from downforce_core.detailed.audit import inventory, write_sample_table


def test_failed_season_does_not_erase_other_seasons() -> None:
    def fetch(year: int) -> list[dict[str, object]]:
        if year == 2019:
            raise ValueError("provider unavailable")
        return [{"RoundNumber": 1, "Session1": "Race", "Session1DateUtc": f"{year}-03-01"}]

    result = inventory(fetch, first_year=2018, now=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(result["sessions"]) == 2
    assert result["failed_years"] == [2019]
    assert result["complete_inventory"] is False
    assert result["summary"]["completed"] == 0
    assert result["summary"]["past-unverified"] == 1
    assert result["summary"]["future"] == 1


def test_columnar_sample_keeps_null_discrete_types_and_source_index(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {"Time": pd.to_timedelta([1, 2], unit="s"), "nGear": [3, 4], "Speed": [None, 200]},
        index=[10, 11],
    )
    result = write_sample_table(frame, tmp_path / "car.parquet")
    stored = pq.read_table(tmp_path / "car.parquet")
    assert result["rows"] == 2
    assert result["bytes"] > 0
    assert len(result["sha256"]) == 64
    assert stored.column("source_index").to_pylist() == [10, 11]
    assert stored.column("Speed").to_pylist() == [None, 200.0]
    assert stored.column("nGear").to_pylist() == [3, 4]
    assert (
        pq.ParquetFile(tmp_path / "car.parquet").metadata.row_group(0).column(0).compression
        == "ZSTD"
    )


def test_sample_writer_does_not_overwrite_evidence(tmp_path: Path) -> None:
    path = tmp_path / "laps.parquet"
    write_sample_table(pd.DataFrame({"a": [1]}), path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_sample_table(pd.DataFrame({"a": [2]}), path)
    assert path.read_bytes() == before


def test_native_sample_nested_types_and_metadata_survive_parquet(tmp_path: Path) -> None:
    frame = pd.DataFrame({"provider_segments": [[1, 2], None], "opaque": [{"a": 1}, None]})
    native = frame.copy()
    native.insert(0, "source_index", native.index)
    expected = pa.Table.from_pandas(native, preserve_index=False)
    path = tmp_path / "nested.parquet"
    write_sample_table(frame, path)
    assert pq.read_table(path).equals(expected, check_metadata=True)


def test_interrupted_native_write_does_not_publish_partial_parquet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(table: object, handle: object, **kwargs: object) -> None:
        handle.write(b"partial")
        raise OSError("interrupted native write")

    monkeypatch.setattr("downforce_core.detailed.audit.pq.write_table", interrupted)
    path = tmp_path / "car.parquet"
    with pytest.raises(OSError, match="interrupted"):
        write_sample_table(pd.DataFrame({"Speed": [1]}), path)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_native_writer_closes_owned_reader_before_link_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import downforce_core.detailed.audit as audit

    parquet_file = pq.ParquetFile
    link = audit.os.link
    readers = []

    class OwnedReader:
        def __init__(self, path):
            self.reader = parquet_file(path)
            self.closed = False
            readers.append(self)

        def __enter__(self):
            return self

        def read(self):
            return self.reader.read()

        def __exit__(self, *_):
            self.reader.close()
            self.closed = True

    def publish(source, target):
        assert readers and all(reader.closed for reader in readers)
        link(source, target)

    monkeypatch.setattr(audit.pq, "ParquetFile", OwnedReader)
    monkeypatch.setattr(audit.os, "link", publish)
    write_sample_table(pd.DataFrame({"Speed": [1.25]}), tmp_path / "car.parquet")
    assert all(reader.closed for reader in readers)
    assert list(tmp_path.iterdir()) == [tmp_path / "car.parquet"]


def test_native_cleanup_denial_keeps_original_winerror_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_unlink = Path.unlink
    denial = PermissionError(13, "controlled sharing violation")
    denial.winerror = 32

    def deny_temporary(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise denial
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_temporary)
    with pytest.raises(PermissionError) as caught:
        write_sample_table(pd.DataFrame({"Status": ["Finalised"]}), tmp_path / "status.parquet")
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert any(
        "cleanup" in note and "published" in note for note in getattr(caught.value, "__notes__", [])
    )
    assert (tmp_path / "status.parquet").exists()


@pytest.mark.parametrize("cleanup_type", [PermissionError, OSError])
def test_native_primary_denial_survives_secondary_cleanup_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_type: type[OSError]
) -> None:
    denial = PermissionError(13, "native write denied", "original-native.tmp")
    denial.winerror = 5
    cleanup_denial = cleanup_type(5, "cleanup sharing violation", "cleanup-native.tmp")
    cleanup_denial.winerror = 32
    original_unlink = Path.unlink

    def denied_write(*args, **kwargs):
        raise denial

    def denied_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise cleanup_denial
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr("downforce_core.detailed.audit.pq.write_table", denied_write)
    monkeypatch.setattr(Path, "unlink", denied_cleanup)
    with pytest.raises(PermissionError) as caught:
        write_sample_table(pd.DataFrame({"Status": ["Finalised"]}), tmp_path / "status.parquet")
    assert caught.value is denial
    assert caught.value.winerror == 5
    assert caught.value.filename == "original-native.tmp"
    assert any(
        "cleanup" in note and "winerror=32" in note and "cleanup-native.tmp" in note
        for note in getattr(caught.value, "__notes__", [])
    )
    assert not (tmp_path / "status.parquet").exists()
