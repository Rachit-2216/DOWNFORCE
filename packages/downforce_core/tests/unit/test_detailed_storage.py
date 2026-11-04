"""Immutable revisions and targeted partition reads for detailed telemetry."""

import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pytest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import StorageIntegrityError


def test_publish_is_idempotent_and_reads_only_requested_driver(tmp_path: Path) -> None:
    store = DetailedStore(tmp_path)
    tables = {
        "car-44": pa.table({"session_time_ms": [100, 200, 300], "speed": [100, 110, 120]}),
        "car-1": pa.table({"session_time_ms": [100], "speed": [105]}),
    }
    revision = store.publish("2024-01-race", tables, {"source": "fastf1"})
    assert store.publish("2024-01-race", tables, {"source": "fastf1"}) == revision
    assert len(list((store.root / "2024-01-race" / "revisions").iterdir())) == 1
    # Corruption in a different partition must be caught by validate, not loaded by this query.
    other = store.root / "2024-01-race" / "revisions" / revision / "car-1.parquet"
    other.write_bytes(b"corrupt")
    trace = store.read_table("2024-01-race", "car-44", time_range=(150, 250))
    assert trace.to_pydict() == {"session_time_ms": [200], "speed": [110]}
    with pytest.raises(StorageIntegrityError):
        store.validate("2024-01-race")


def test_manifest_tampering_is_detected(tmp_path: Path) -> None:
    store = DetailedStore(tmp_path)
    revision = store.publish("2024-01-race", {"laps": pa.table({"lap": [1]})}, {})
    path = store.root / "2024-01-race" / "revisions" / revision / "manifest.json"
    data = json.loads(path.read_text())
    data["metadata"] = {"evil": True}
    path.write_text(json.dumps(data))
    with pytest.raises(StorageIntegrityError):
        store.manifest("2024-01-race")


@pytest.mark.parametrize("identifier", ["../outside", "CON", "a/b", "a%2fb", ""])
def test_paths_are_rejected(tmp_path: Path, identifier: str) -> None:
    with pytest.raises((ValueError, StorageIntegrityError)):
        DetailedStore(tmp_path).publish(identifier, {"laps": pa.table({"lap": [1]})}, {})


def test_failed_publication_keeps_previous_active_revision(tmp_path: Path) -> None:
    store = DetailedStore(tmp_path)
    revision = store.publish("2024-01-race", {"laps": pa.table({"lap": [1]})}, {})
    with pytest.raises(ValueError):
        store.publish("2024-01-race", {"../bad": pa.table({"lap": [2]})}, {})
    assert store.manifest("2024-01-race")["revision"] == revision


def test_read_rejects_invalid_time_range(tmp_path: Path) -> None:
    store = DetailedStore(tmp_path)
    store.publish("2024-01-race", {"car-44": pa.table({"session_time_ms": [1]})}, {})
    with pytest.raises(ValueError):
        store.read_table("2024-01-race", "car-44", time_range=(100, 0))


def test_canonical_round_trip_preserves_arrow_metadata_and_exact_values(tmp_path: Path) -> None:
    table = pa.table({"speed": [None, 1.0000000000001], "gear": [1, None]}).replace_schema_metadata(
        {b"source": b"provider-native", b"precision": b"float64"}
    )
    store = DetailedStore(tmp_path)
    store.publish("2024-01-race", {"car-44": table}, {})
    assert store.read_table("2024-01-race", "car-44").equals(table, check_metadata=True)


def test_explicit_detailed_root_only_changes_this_store(tmp_path: Path) -> None:
    alternate = tmp_path / "separate-detailed"
    store = DetailedStore(tmp_path, detailed_root=alternate)
    assert store.root == alternate / "canonical"
    assert not (tmp_path / ".downforce").exists()


def test_unknown_nested_provider_fields_keep_child_names(tmp_path: Path) -> None:
    nested = pa.array([[1, 2], None], type=pa.list_(pa.field("provider-code", pa.int64())))
    original = pa.table({"unknown_nested": nested})
    store = DetailedStore(tmp_path)
    store.publish("2024-01-race", {"laps": original}, {})
    assert store.read_table("2024-01-race", "laps").equals(original, check_metadata=True)


def test_canonical_write_denial_survives_owned_temporary_cleanup_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denial = PermissionError(13, "canonical write denied", "canonical-original.tmp")
    denial.winerror = 32
    cleanup_denial = PermissionError(13, "cleanup denied", "cleanup.tmp")
    cleanup_denial.winerror = 5

    def denied_write(*args, **kwargs):
        raise denial

    def denied_cleanup(*args, **kwargs):
        raise cleanup_denial

    monkeypatch.setattr("downforce_core.detailed.storage.pq.write_table", denied_write)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(denied_cleanup))
    store = DetailedStore(tmp_path)
    with pytest.raises(PermissionError) as caught:
        store.publish("2024-01-race", {"laps": pa.table({"lap": [1]})}, {})
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert caught.value.filename == "canonical-original.tmp"
    assert any(
        "cleanup" in note and "winerror=5" in note and "cleanup.tmp" in note
        for note in getattr(denial, "__notes__", [])
    )
    assert not (store.root / "2024-01-race/active.json").exists()
