import gzip
import json
from pathlib import Path

import pyarrow as pa
import pytest
from downforce_core.detailed.openf1 import (
    encode_response,
    enrichment_table,
    measure_enrichment,
)
from downforce_core.storage.parquet import file_sha256
from test_detailed_proofs import save, study


def historical_fixture(tmp_path):
    paths, _ = study(tmp_path, modern=True)
    root = paths.root
    source = json.loads((root / "openf1-inventory.json").read_text())
    source["sessions"][0]["session_key"] = 1
    save(root / "openf1-inventory.json", source)
    catalog = json.loads((root / "reconciled-inventory.json").read_text())
    catalog["sessions"][0]["openf1_session_key"] = 1
    catalog["provider_entries"][0]["session_key"] = 1
    catalog["openf1_inventory_sha256"] = file_sha256(root / "openf1-inventory.json")
    save(root / "reconciled-inventory.json", catalog)
    plan = json.loads((root / "study/sample-plan.json").read_text())
    plan["selected_sessions"] = catalog["sessions"]
    plan["inventory_sha256"] = file_sha256(root / "reconciled-inventory.json")
    save(root / "study/sample-plan.json", plan)
    return root, catalog["sessions"][0]


def test_enrichment_keeps_lapped_gaps_distinct_from_seconds() -> None:
    rows = [
        {"driver_number": 1, "interval": 1.125, "gap_to_leader": None},
        {"driver_number": 44, "interval": "+1 LAP", "gap_to_leader": "+2 LAPS"},
    ]
    table = enrichment_table(rows)
    assert table["interval_kind"].to_pylist() == ["seconds", "laps-down"]
    assert table["interval_seconds"].to_pylist() == [1.125, None]
    assert table["interval_laps_down"].to_pylist() == [None, 1]
    assert table["interval_provider_json"].to_pylist() == ["1.125", '"+1 LAP"']


def test_enrichment_does_not_drop_fields_absent_in_first_record() -> None:
    table = enrichment_table([{"a": 1}, {"a": 2, "later_field": [3, 4]}])
    assert table["later_field"].to_pylist() == [None, [3, 4]]
    assert table["a"].type == pa.int64()


def test_compressed_http_evidence_restores_exact_wire_body() -> None:
    original = b' [ { "speed" : 123.0000000000001, "custom" : null } ]\n'
    packed = encode_response(original)
    assert gzip.decompress(packed) == original
    assert encode_response(original) == packed
    assert json.loads(gzip.decompress(packed))[0]["speed"] == 123.0000000000001


def test_interrupted_normalization_reuses_verified_wire_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("downforce_core.detailed.openf1.ENDPOINTS", ("drivers",))
    tmp_path, session = historical_fixture(tmp_path)

    class SourceClient:
        def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
            return (
                b'[{"session_key":1,"driver_number":44,"segments":[2048,2049]}]',
                "https://api.openf1.org/v1/drivers?session_key=1",
            )

    import downforce_core.detailed.openf1 as module

    writer = module.pq.write_table

    def interrupted(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted conversion")

    monkeypatch.setattr(module.pq, "write_table", interrupted)
    with pytest.raises(OSError):
        measure_enrichment(tmp_path, session, SourceClient())
    monkeypatch.setattr(module.pq, "write_table", writer)

    class OfflineClient:
        def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
            raise AssertionError("cached source must not be fetched again")

    result = measure_enrichment(tmp_path, session, OfflineClient())
    assert result["openf1_measured"] is True
    assert result["endpoints"]["drivers"]["rows"] == 1


def test_not_found_endpoint_is_preserved_as_unavailable_not_a_fake_empty_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    monkeypatch.setattr("downforce_core.detailed.openf1.ENDPOINTS", ("starting_grid",))
    tmp_path, session = historical_fixture(tmp_path)

    class MissingClient:
        def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
            raise HTTPError(
                "https://api.openf1.org/v1/starting_grid?session_key=1",
                404,
                "Not Found",
                {},
                BytesIO(b'{"detail":"Not Found"}'),
            )

    result = measure_enrichment(
        tmp_path,
        session,
        MissingClient(),
    )
    record = result["endpoints"]["starting_grid"]
    assert record["http_status"] == 404
    assert record["availability"] == "unavailable"
    assert record["source_body_bytes"] > 0


@pytest.mark.parametrize("availability", ["empty", "unavailable", "populated"])
def test_owned_parquet_verification_reader_closes_before_publication(
    tmp_path, monkeypatch, availability
):
    from io import BytesIO
    from urllib.error import HTTPError

    import downforce_core.detailed.openf1 as module

    monkeypatch.setattr(module, "ENDPOINTS", ("overtakes",))
    tmp_path, session = historical_fixture(tmp_path)
    rows = (
        []
        if availability != "populated"
        else [
            {"session_key": 1, "driver_number": 1, "segments": [2, 3], "later": None},
            {"session_key": 1, "driver_number": 44, "segments": [], "later": "retained"},
        ]
    )
    expected = enrichment_table(rows).replace_schema_metadata({b"verification": b"exact\xff"})
    monkeypatch.setattr(module, "enrichment_table", lambda _rows: expected)
    real_reader = module.pq.ParquetFile
    readers = []

    class OwnedReader(real_reader):
        def __init__(self, source, **kwargs):
            super().__init__(source, **kwargs)
            self.source_path = Path(source)
            readers.append(self)

    monkeypatch.setattr(module.pq, "ParquetFile", OwnedReader)
    rename = module.os.rename
    publications = []

    def publish(source, destination):
        if Path(source).suffix == ".parquet":
            staged_readers = [r for r in readers if r.source_path == Path(source)]
            assert len(staged_readers) == 1, "staging verification must own its reader"
            assert staged_readers[0].closed, "verification reader must close before rename"
            publications.append(Path(destination))
        return rename(source, destination)

    monkeypatch.setattr(module.os, "rename", publish)

    class Client:
        def fetch(self, endpoint, session_key):
            url = "https://api.openf1.org/v1/overtakes?session_key=1"
            if availability == "unavailable":
                raise HTTPError(url, 404, "Not Found", {}, BytesIO(b'{"detail":"Not Found"}'))
            return json.dumps(rows).encode(), url

    report = measure_enrichment(tmp_path, session, Client())
    assert len(publications) == 1
    assert all(reader.closed for reader in readers)
    with real_reader(publications[0]) as reader:
        assert reader.read().equals(expected, check_metadata=True)
    record = report["endpoints"]["overtakes"]
    assert record["rows"] == len(rows)
    assert record["http_status"] == (404 if availability == "unavailable" else 200)
    before = {p: p.read_bytes() for p in publications[0].parent.iterdir() if p.is_file()}

    class OfflineClient:
        def fetch(self, endpoint, session_key):
            raise AssertionError("immutable published evidence must not be fetched again")

    assert measure_enrichment(tmp_path, session, OfflineClient()) == report
    assert {p: p.read_bytes() for p in before} == before
    assert len(publications) == 1


@pytest.mark.parametrize("rows", [[], [{"session_key": 1, "driver_number": 44}]])
@pytest.mark.parametrize("failure", ["read", "metadata"])
def test_owned_reader_closes_on_verification_failure_and_wire_resume_is_preserved(
    tmp_path, monkeypatch, rows, failure
):
    import downforce_core.detailed.openf1 as module

    monkeypatch.setattr(module, "ENDPOINTS", ("overtakes",))
    tmp_path, session = historical_fixture(tmp_path)
    real_reader = module.pq.ParquetFile
    readers = []
    fail = True

    class OwnedReader(real_reader):
        def __init__(self, source, **kwargs):
            super().__init__(source, **kwargs)
            self.source_path = Path(source)
            readers.append(self)

        def read(self, **kwargs):
            table = super().read(**kwargs)
            if fail and self.source_path.name == "response.parquet":
                if failure == "read":
                    raise OSError("verification reader failed")
                return table.replace_schema_metadata({b"unexpected": b"metadata"})
            return table

    monkeypatch.setattr(module.pq, "ParquetFile", OwnedReader)
    calls = []

    class Client:
        def fetch(self, endpoint, session_key):
            calls.append(endpoint)
            return json.dumps(rows).encode(), "https://api.openf1.org/v1/overtakes?session_key=1"

    expected_error = OSError if failure == "read" else ValueError
    with pytest.raises(expected_error, match="verification reader|staging changed"):
        measure_enrichment(tmp_path, session, Client())
    assert readers and all(reader.closed for reader in readers)
    directory = tmp_path / "study/openf1/2024-01-race"
    assert not list(directory.glob("*.parquet"))
    assert not (directory / "overtakes.json").exists()
    assert not (directory / "measurement.json").exists()
    assert not list(directory.glob(".columnar-*"))
    wire_before = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert any(p.name.endswith("-source.json") for p in wire_before)
    assert any(p.suffix == ".gz" for p in wire_before)
    fail = False

    class OfflineClient:
        def fetch(self, endpoint, session_key):
            raise AssertionError("verification failure must resume saved wire without HTTP")

    report = measure_enrichment(tmp_path, session, OfflineClient())
    assert report["endpoints"]["overtakes"]["rows"] == len(rows)
    assert calls == ["overtakes"]
    assert all(reader.closed for reader in readers)
    assert {p: p.read_bytes() for p in wire_before} == wire_before
