import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.exceptions import StorageIntegrityError


def test_raw_pack_round_trip_is_exact_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "data.bin").write_bytes(bytes(range(256)) * 200)
    (source / "empty").write_bytes(b"")
    original = source_manifest(source)
    store = RawEvidenceStore(tmp_path / "evidence")
    packed = store.pack(
        source, session_id="2024-01-race", provider="fastf1", provider_version="3.8.3"
    )
    assert (
        store.pack(source, session_id="2024-01-race", provider="fastf1", provider_version="3.8.3")
        == packed
    )
    destination = tmp_path / "restored"
    store.restore(packed, destination)
    assert source_manifest(destination) == original
    assert store.validate(packed)["session_id"] == "2024-01-race"
    assert source_manifest(source) == original


@pytest.mark.parametrize("damage", ["truncate", "flip"])
def test_corrupt_bundle_is_rejected_before_restore(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample").write_bytes(b"raw-data" * 100)
    store = RawEvidenceStore(tmp_path / "evidence")
    bundle = store.pack(
        source, session_id="2024-01-race", provider="fastf1", provider_version="3.8.3"
    )
    path = store.root / f"{bundle}.zip"
    data = path.read_bytes()
    path.write_bytes(
        data[: len(data) // 2] if damage == "truncate" else bytes([data[0] ^ 1]) + data[1:]
    )
    with pytest.raises(StorageIntegrityError):
        store.restore(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_untrusted_archive_path_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "bad.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../outside", b"payload")
        archive.writestr("manifest.json", json.dumps({"files": []}))
    store = RawEvidenceStore(tmp_path / "evidence")
    with pytest.raises((ValueError, StorageIntegrityError)):
        store.restore("../bad", tmp_path / "restored")
    assert not (tmp_path / "outside").exists()


def test_restore_refuses_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample").write_bytes(b"data")
    store = RawEvidenceStore(tmp_path / "evidence")
    bundle = store.pack(
        source, session_id="2024-01-race", provider="fastf1", provider_version="3.8.3"
    )
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        store.restore(bundle, target)


def test_manifest_member_timestamp_is_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample").write_bytes(b"data")
    store = RawEvidenceStore(tmp_path / "evidence")
    packed = store.pack(
        source, session_id="2024-01-race", provider="fastf1", provider_version="3.8.3"
    )
    with zipfile.ZipFile(store.root / f"{packed}.zip") as archive:
        assert archive.getinfo("manifest.json").date_time == (1980, 1, 1, 0, 0, 0)


@pytest.mark.parametrize("member", ["../outside", "C:/outside", "nested\\escape", "CON", "same."])
def test_self_consistent_untrusted_pack_cannot_escape_restore(tmp_path: Path, member: str) -> None:
    store = RawEvidenceStore(tmp_path / "evidence")
    store.root.mkdir()  # Fixture writes its untrusted archive; constructors are read-only.
    path = store.root / "untrusted.zip"
    payload = b"raw"
    manifest = {
        "schema_version": "raw-evidence-zip-v1",
        "session_id": "2024-01-race",
        "provider": "fastf1",
        "provider_version": "3.8.3",
        "directories": [],
        "files": [
            {
                "path": member,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mtime_ns": 1,
                "mode": 0o666,
            }
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("source/" + member, payload)
        archive.writestr("manifest.json", json.dumps(manifest))
    identity = hashlib.sha256(path.read_bytes()).hexdigest()
    path.rename(store.root / f"{identity}.zip")
    with pytest.raises(StorageIntegrityError):
        store.validate(identity)
    with pytest.raises(StorageIntegrityError):
        store.restore(identity, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("operation", ["pack", "restore"])
def test_raw_operation_denial_survives_owned_temporary_cleanup_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import downforce_core.detailed.raw_evidence as module

    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"original")
    original = source_manifest(source)
    store = RawEvidenceStore(tmp_path / "raw")
    bundle = store.pack(
        source, session_id="2024-01-race", provider="fastf1", provider_version="test"
    )
    denial = PermissionError(13, "raw operation denied", "raw-original.tmp")
    denial.winerror = 32
    cleanup_denial = PermissionError(13, "cleanup denied", "cleanup.tmp")
    cleanup_denial.winerror = 5

    def denied(*args, **kwargs):
        raise denial

    def denied_cleanup(*args, **kwargs):
        raise cleanup_denial

    if operation == "pack":
        monkeypatch.setattr(module.zipfile, "ZipFile", denied)
    else:
        monkeypatch.setattr(module.shutil, "copyfileobj", denied)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", staticmethod(denied_cleanup))
    with pytest.raises(PermissionError) as caught:
        if operation == "pack":
            store.pack(
                source, session_id="2024-01-race", provider="fastf1", provider_version="test"
            )
        else:
            store.restore(bundle, tmp_path / "restored")
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert caught.value.filename == "raw-original.tmp"
    assert any(
        "cleanup" in note and "winerror=5" in note and "cleanup.tmp" in note
        for note in getattr(denial, "__notes__", [])
    )
    assert source_manifest(source) == original
    assert not (tmp_path / "restored").exists()
