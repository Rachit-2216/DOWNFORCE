"""Read-only detailed bridges use genuine metadata and targeted leased Parquet."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.manifest import RuntimeManifest


class FileReader:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[str] = []

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        self.calls.append(relative_path)
        with (self.source / relative_path).open("rb") as handle:
            while chunk := handle.read(min(4096, max_bytes + 1)):
                yield chunk


def runtime_cache(
    source: Path, root: Path, *, exclude: tuple[str, ...] = (), max_disk_bytes: int = 512 * 1024**2
) -> tuple[RuntimeBlobCache, FileReader]:
    files = {
        path.relative_to(source).as_posix(): {
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".parquet"}
        and path.relative_to(source).as_posix() not in exclude
    }
    body = json.dumps({"schema_version": 1, "files": files}).encode()
    manifest = RuntimeManifest.from_bytes(body, hashlib.sha256(body).hexdigest())
    reader = FileReader(source)
    return RuntimeBlobCache(root, manifest, reader, max_disk_bytes=max_disk_bytes), reader


def detailed_fixture(tmp_path: Path) -> tuple[DetailedStore, str]:
    source = tmp_path / "source"
    source.mkdir()
    local = DetailedStore(source)
    revision = local.publish(
        "2024-01-race",
        {
            "car-44": pa.table({"session_time_ms": [100, 200, 300], "speed": [100, 110, 120]}),
            "car-1": pa.table({"session_time_ms": [100], "speed": [105]}),
        },
        {"source": "native"},
    )
    local.publish("2024-02-race", {"laps": pa.table({"lap": [1]})}, {})
    return local, revision


def test_detailed_bridge_exists() -> None:
    module = importlib.import_module("downforce_core.runtime_blob.stores_detailed")
    assert issubclass(module.BlobDetailedStore, DetailedStore)


def test_detailed_metadata_and_selected_driver_equal_local(tmp_path: Path, monkeypatch) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    monkeypatch.setenv("DOWNFORCE_DETAILED_DATA_ROOT", str(tmp_path / "unrelated"))
    remote = BlobDetailedStore(cache)
    assert remote.root == cache.release_root / ".downforce/detailed/canonical"
    assert not cache.release_root.exists()
    assert remote.session_ids() == ("2024-01-race", "2024-02-race")
    assert remote.has_session("2024-01-race")
    assert not remote.has_session("2024-03-race")
    assert reader.calls == []
    assert remote.manifest("2024-01-race") == local.manifest("2024-01-race")
    assert len(reader.calls) == 2
    expected = local.read_table("2024-01-race", "car-44", time_range=(150, 250), columns=["speed"])
    assert remote.read_table(
        "2024-01-race", "car-44", time_range=(150, 250), columns=["speed"]
    ).equals(expected, check_metadata=True)
    assert len(reader.calls) == 3
    assert reader.calls[-1].endswith("/car-44.parquet")
    assert all("2024-02-race" not in path and "car-1.parquet" not in path for path in reader.calls)
    assert remote.validate("2024-01-race", revision=revision) == local.validate("2024-01-race")
    assert len(reader.calls) == 4


def test_detailed_pin_uses_no_pointer_and_missing_entries_stay_missing(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    missing = ".downforce/detailed/canonical/2024-01-race/active.json"
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime", exclude=(missing,))
    remote = BlobDetailedStore(cache)
    assert remote.session_ids() == ("2024-02-race",)
    assert remote.manifest("2024-01-race", revision=revision) == local.manifest("2024-01-race")
    assert reader.calls == [
        f".downforce/detailed/canonical/2024-01-race/revisions/{revision}/manifest.json"
    ]
    with pytest.raises(StorageIntegrityError):
        remote.manifest("2024-01-race")
    assert not (cache.release_root / missing).exists()


def test_detailed_corrupt_other_partition_does_not_block_selected_driver(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    other = local.root / "2024-01-race/revisions" / revision / "car-1.parquet"
    other.write_bytes(b"corrupt")
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    remote = BlobDetailedStore(cache)
    assert remote.read_table("2024-01-race", "car-44").num_rows == 3
    with pytest.raises(StorageIntegrityError):
        remote.validate("2024-01-race")
    with pytest.raises(KeyError):
        remote.read_table("2024-01-race", "not-available")


def test_detailed_read_only_publish(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    detailed_fixture(tmp_path)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError, match="read-only"):
        BlobDetailedStore(cache).publish("new-race", {"laps": pa.table({"lap": [1]})}, {})
    assert reader.calls == []
    assert not cache.release_root.exists()


@pytest.mark.parametrize("payload", [[], None, "invalid"])
def test_detailed_nonobject_remote_manifest_is_safe_integrity_failure(
    tmp_path: Path, payload: object
) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    path = local.root / "2024-01-race" / "revisions" / revision / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError) as caught:
        BlobDetailedStore(cache).manifest("2024-01-race")
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None


def test_detailed_final_arrow_open_is_inside_partition_lease(tmp_path: Path, monkeypatch) -> None:
    import downforce_core.detailed.storage as storage
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    detailed_fixture(tmp_path)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    original = storage.pq.read_table
    opened = []

    def checked_read(path, **kwargs):
        opened.append(path)
        assert Path(path) in cache._state.leased
        return original(path, **kwargs)

    monkeypatch.setattr(storage.pq, "read_table", checked_read)
    assert BlobDetailedStore(cache).read_table("2024-01-race", "car-44").num_rows == 3
    assert len(opened) == 1
    assert cache._state.leased == {}


@pytest.mark.parametrize("field", ["tables", "bytes", "sha256", "rows", "schema"])
def test_detailed_semantic_manifest_cannot_bypass_selected_table_validation(
    tmp_path: Path, field: str
) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    root = local.root / "2024-01-race/revisions" / revision
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    if field == "tables":
        manifest["tables"] = None
    else:
        manifest["tables"]["car-44"][field] = {
            "bytes": 1,
            "sha256": "a" * 64,
            "rows": 1,
            "schema": "wrong",
        }[field]
    semantic = {key: value for key, value in manifest.items() if key != "revision"}
    replacement = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    manifest["revision"] = replacement
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    root.rename(root.with_name(replacement))
    (local.root / "2024-01-race/active.json").write_text(
        json.dumps({"revision": replacement}), encoding="utf-8"
    )
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError):
        BlobDetailedStore(cache).read_table("2024-01-race", "car-44")


@pytest.mark.parametrize("time_range", [(2, 1), (float("nan"), 1), (1, float("inf"))])
def test_detailed_invalid_time_range_precedes_all_remote_access(tmp_path: Path, time_range) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    detailed_fixture(tmp_path)
    cache, reader = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(ValueError, match="invalid detailed time range"):
        BlobDetailedStore(cache).read_table("unavailable-race", "car-44", time_range=time_range)
    assert reader.calls == []
    assert not cache.release_root.exists()


def test_detailed_missing_column_preserves_original_arrow_invalid(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, _ = detailed_fixture(tmp_path)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(pa.ArrowInvalid) as expected:
        local.read_table("2024-01-race", "car-44", columns=["not-a-column"])
    with pytest.raises(type(expected.value)):
        BlobDetailedStore(cache).read_table("2024-01-race", "car-44", columns=["not-a-column"])


def test_detailed_missing_time_filter_field_preserves_original_arrow_invalid(
    tmp_path: Path,
) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, _ = detailed_fixture(tmp_path)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(pa.ArrowInvalid) as expected:
        local.read_table("2024-02-race", "laps", time_range=(1, 2))
    with pytest.raises(type(expected.value)):
        BlobDetailedStore(cache).read_table("2024-02-race", "laps", time_range=(1, 2))


@pytest.mark.parametrize("part", ["pointer", "manifest"])
def test_detailed_deep_remote_json_is_safe_integrity_failure(tmp_path: Path, part: str) -> None:
    from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore

    local, revision = detailed_fixture(tmp_path)
    root = local.root / "2024-01-race"
    path = (
        root / "active.json"
        if part == "pointer"
        else root / "revisions" / revision / "manifest.json"
    )
    path.write_bytes(b"[" * 10000 + b"0" + b"]" * 10000)
    cache, _ = runtime_cache(tmp_path / "source", tmp_path / "runtime")
    with pytest.raises(StorageIntegrityError) as caught:
        BlobDetailedStore(cache).manifest("2024-01-race")
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in str(caught.value)
