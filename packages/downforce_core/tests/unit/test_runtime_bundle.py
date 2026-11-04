"""Offline visitor bundles are authenticated, bounded and fail closed."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.export import RuntimeRelease, prepare_runtime_release
from downforce_core.runtime_blob.manifest import RuntimeManifest
from test_runtime_blob_export import _file_snapshot, fixture


def _module() -> ModuleType:
    assert importlib.util.find_spec("downforce_core.runtime_blob.bundle") is not None
    return importlib.import_module("downforce_core.runtime_blob.bundle")


def _release(tmp_path: Path) -> RuntimeRelease:
    root = tmp_path / "source"
    content = b"genuine stream bytes" * 7000
    paths = (".downforce/analytics/manifest.json", ".downforce/analytics/driver-race.parquet")
    sources = {}
    entries = {}
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        sources[name] = path
        entries[name] = {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    generated = {".downforce/analytics/circuit-identities.json": b"{}"}
    entries.update(
        (name, {"size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        for name, body in generated.items()
    )
    body = json.dumps({"schema_version": 1, "files": entries}, sort_keys=True).encode()
    digest = hashlib.sha256(body).hexdigest()
    return RuntimeRelease(
        root, body, digest, RuntimeManifest.from_bytes(body, digest), sources, generated
    )


def test_bundle_genuine_visitor_fixture_is_exact_product_only_and_readonly(tmp_path: Path) -> None:
    module = _module()
    source, _, _, _ = fixture(tmp_path)
    for name in (".downforce/prompts/private.txt", ".downforce/cache/private.bin"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private")
    release = prepare_runtime_release(source, include_detailed=False)
    before = _file_snapshot(source)
    root = module.write_bundle(release, tmp_path / "product-data")
    assert _file_snapshot(source) == before
    assert set(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) == {
        "release.json",
        "release.sha256",
        *(f"objects/{entry.sha256}.bin" for entry in release.manifest.files.values()),
    }
    assert not (root / ".downforce").exists()
    assert (root / "release.json").read_bytes() == release.body
    assert (root / "release.sha256").read_bytes() == (release.sha256 + "\n").encode()
    assert str(source).encode() not in release.body
    reader = module.BundledReleaseReader(root)
    assert reader.release_body == release.body
    assert reader.manifest.files == release.manifest.files
    for name, entry in release.manifest.files.items():
        assert not name.startswith(".downforce/detailed/")
        chunks = tuple(reader.iter_bytes(name, max_bytes=entry.size_bytes))
        assert all(len(chunk) <= 65536 for chunk in chunks)
        assert b"".join(chunks) == b"".join(
            release.reader().iter_bytes(name, max_bytes=entry.size_bytes)
        )
    assert b"".join(reader.iter_bytes("release.json", max_bytes=len(release.body))) == release.body


def test_bundle_deduplicates_reuses_identical_without_any_writes(tmp_path: Path) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    assert len(list((root / "objects").iterdir())) == 2
    before = _file_snapshot(tmp_path)
    directory_times = {
        path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_dir()
    }
    assert module.write_bundle(release, root) == root
    assert before == _file_snapshot(tmp_path)
    assert directory_times == {path: path.stat().st_mtime_ns for path in directory_times}
    reader = module.BundledReleaseReader(root, expected_sha256=release.sha256)
    reader.validate_inventory()


@pytest.mark.parametrize("reuse", [False, True])
def test_bundle_verifies_even_duplicate_source_streams(tmp_path: Path, reuse: bool) -> None:
    module = _module()
    release = _release(tmp_path)
    output = tmp_path / "bundle"
    if reuse:
        module.write_bundle(release, output)
    duplicate = list(release.source_files.values())[1]
    duplicate.write_bytes(b"changed duplicate source")
    before = _file_snapshot(output) if reuse else None
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, output)
    if reuse:
        assert before == _file_snapshot(output)
    else:
        assert not (output / "release.sha256").exists()


@pytest.mark.parametrize("failure", ["unknown", "incomplete", "corrupt", "different"])
def test_bundle_refuses_existing_unknown_incomplete_corrupt_unchanged(
    tmp_path: Path, failure: str
) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    if failure == "unknown":
        (root / "user-owned.txt").write_bytes(b"keep")
    elif failure == "incomplete":
        (root / "release.sha256").unlink()
    elif failure == "corrupt":
        next((root / "objects").iterdir()).write_bytes(b"corrupt")
    else:
        (root / "release.json").write_bytes(release.body + b" ")
    before = _file_snapshot(root)
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, root)
    assert before == _file_snapshot(root)


@pytest.mark.parametrize("failure", ["missing", "tampered", "oversized", "truncated"])
def test_bundle_reader_rejects_object_mutation_per_read(tmp_path: Path, failure: str) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    reader = module.BundledReleaseReader(root)
    name, entry = next(iter(release.manifest.files.items()))
    obj = root / "objects" / (entry.sha256 + ".bin")
    if failure == "missing":
        obj.unlink()
    elif failure == "tampered":
        obj.write_bytes(b"z" * entry.size_bytes)
    elif failure == "oversized":
        obj.write_bytes(obj.read_bytes() + b"z")
    else:
        obj.write_bytes(obj.read_bytes()[:-1])
    with pytest.raises(StorageIntegrityError):
        b"".join(reader.iter_bytes(name, max_bytes=entry.size_bytes))


@pytest.mark.parametrize(
    "pin", [b"", b"a" * 64, b"a" * 64 + b"\nextra", b"A" * 64 + b"\n", b"g" * 64 + b"\n"]
)
def test_bundle_reader_requires_strict_pin_even_with_expected_hash(
    tmp_path: Path, pin: bytes
) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    (root / "release.sha256").write_bytes(pin)
    for expected in (None, release.sha256):
        with pytest.raises(StorageIntegrityError):
            module.BundledReleaseReader(root, expected_sha256=expected)


@pytest.mark.parametrize(
    "failure",
    [
        "missing-pin",
        "wrong-pin",
        "wrong-expected",
        "extra-object",
        "object-dir",
        "missing-object",
        "nested-dir",
        "extra-root",
    ],
)
def test_bundle_reader_rejects_incomplete_or_unknown_inventory(
    tmp_path: Path, failure: str
) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    expected = None
    if failure == "missing-pin":
        (root / "release.sha256").unlink()
    elif failure == "wrong-pin":
        (root / "release.sha256").write_bytes(("a" * 64 + "\n").encode())
    elif failure == "wrong-expected":
        expected = "a" * 64
    elif failure == "extra-object":
        (root / "objects" / ("a" * 64 + ".bin")).write_bytes(b"unknown")
    elif failure == "object-dir":
        path = next((root / "objects").iterdir())
        path.unlink()
        path.mkdir()
    elif failure == "missing-object":
        next((root / "objects").iterdir()).unlink()
    elif failure == "nested-dir":
        (root / "objects/unknown").mkdir()
    else:
        (root / ".downforce").mkdir()
    before = _file_snapshot(root)
    with pytest.raises(StorageIntegrityError):
        module.BundledReleaseReader(root, expected_sha256=expected)
    assert before == _file_snapshot(root)


@pytest.mark.parametrize("bound", [-1, True, 1.0, "1000000", None, 0])
@pytest.mark.parametrize("name", ["release.json", ".downforce/analytics/manifest.json"])
def test_bundle_reader_rejects_bad_and_insufficient_bounds(
    tmp_path: Path, bound: object, name: str
) -> None:
    module = _module()
    root = module.write_bundle(_release(tmp_path), tmp_path / "bundle")
    reader = module.BundledReleaseReader(root)
    with pytest.raises(StorageIntegrityError):
        b"".join(reader.iter_bytes(name, max_bytes=bound))


@pytest.mark.parametrize(
    "name",
    [
        "../release.json",
        "objects/a.bin",
        "/release.json",
        "C:/secret",
        ".downforce/analytics/missing.json",
        "release.sha256",
    ],
)
def test_bundle_reader_reads_only_indexed_paths(tmp_path: Path, name: str) -> None:
    module = _module()
    root = module.write_bundle(_release(tmp_path), tmp_path / "bundle")
    with pytest.raises(StorageIntegrityError):
        b"".join(module.BundledReleaseReader(root).iter_bytes(name, max_bytes=1000000))


def test_bundle_reader_detects_post_constructor_manifest_changes(tmp_path: Path) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    reader = module.BundledReleaseReader(root)
    (root / "release.json").write_bytes(release.body + b" ")
    with pytest.raises(StorageIntegrityError):
        b"".join(reader.iter_bytes("release.json", max_bytes=len(release.body) + 1))


@pytest.mark.parametrize("name", ["release.json", "release.sha256", "object"])
def test_bundle_rejects_hardlinks_without_touching_files(tmp_path: Path, name: str) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    target = next((root / "objects").iterdir()) if name == "object" else root / name
    outside = tmp_path / "outside.bin"
    os.link(target, outside)
    before = _file_snapshot(tmp_path)
    with pytest.raises(StorageIntegrityError):
        module.BundledReleaseReader(root)
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, root)
    assert before == _file_snapshot(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_bundle_rejects_linked_ancestry_before_writing(tmp_path: Path, kind: str) -> None:
    module = _module()
    if kind == "junction" and os.name != "nt":
        pytest.skip("Windows junction")
    release = _release(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "user.txt").write_bytes(b"user-owned")
    link = tmp_path / "linked"
    if kind == "junction":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("Windows symlink privilege unavailable")
            raise
    before = _file_snapshot(outside)
    try:
        with pytest.raises(StorageIntegrityError):
            module.write_bundle(release, link / "product-data")
        with pytest.raises(StorageIntegrityError):
            module.BundledReleaseReader(link)
        assert before == _file_snapshot(outside)
        assert not (outside / "product-data").exists()
    finally:
        if kind == "junction":
            os.rmdir(link)
        else:
            link.unlink()


def test_bundle_rejects_lexical_parent_traversal_before_writing(tmp_path: Path) -> None:
    module = _module()
    release = _release(tmp_path)
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, tmp_path / "missing/../bundle")
    assert not (tmp_path / "missing").exists()


def test_bundle_rejects_inconsistent_manifest_before_writing(tmp_path: Path) -> None:
    module = _module()
    release = replace(_release(tmp_path), body=b"unauthenticated")
    output = tmp_path / "bundle"
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, output)
    assert not output.exists()


def test_bundle_reader_does_not_read_payloads_at_startup_and_closes_on_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    original = os.fdopen
    opened: list[BinaryIO] = []

    def record(descriptor: int, mode: str) -> BinaryIO:
        handle = original(descriptor, mode)
        opened.append(handle)
        return handle

    monkeypatch.setattr(module.os, "fdopen", record)
    reader = module.BundledReleaseReader(root)
    # Only pin and index are read. Object content is authenticated on demand.
    assert len(opened) == 2
    assert all(handle.closed for handle in opened)
    name = ".downforce/analytics/manifest.json"
    size = release.manifest.entry(name).size_bytes
    iterator = reader.iter_bytes(name, max_bytes=size)
    assert len(next(iterator)) == 65536
    assert not opened[-1].closed
    iterator.close()
    assert opened[-1].closed
    before = len(opened)
    with pytest.raises(StorageIntegrityError):
        b"".join(reader.iter_bytes(name, max_bytes=size - 1))
    assert len(opened) == before


@pytest.mark.parametrize(
    "chunk", [b"x" * 65537, b"invalid digest", "not bytes"], ids=["oversized", "digest", "nonbytes"]
)
def test_bundle_closes_rejected_source_stream_and_never_publishes_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: object
) -> None:
    from downforce_core.runtime_blob.export import ReleaseObjectReader

    module = _module()
    release = _release(tmp_path)
    closed: list[str] = []

    def malformed(self: ReleaseObjectReader, name: str, *, max_bytes: int) -> Iterator[object]:
        try:
            yield chunk
        finally:
            closed.append(name)

    monkeypatch.setattr(ReleaseObjectReader, "iter_bytes", malformed)
    root = tmp_path / "bundle"
    with pytest.raises(StorageIntegrityError):
        module.write_bundle(release, root)
    assert len(closed) == 1
    assert not (root / "release.sha256").exists()


def test_bundle_reader_checks_objects_replaced_with_hardlinks_after_startup(tmp_path: Path) -> None:
    module = _module()
    release = _release(tmp_path)
    root = module.write_bundle(release, tmp_path / "bundle")
    reader = module.BundledReleaseReader(root)
    name, entry = next(iter(release.manifest.files.items()))
    obj = root / "objects" / (entry.sha256 + ".bin")
    os.link(obj, tmp_path / "linked.bin")
    with pytest.raises(StorageIntegrityError):
        b"".join(reader.iter_bytes(name, max_bytes=entry.size_bytes))


@pytest.mark.parametrize("profile", ["full", "visitor"])
@pytest.mark.parametrize("copy", [False, True])
def test_export_cli_reports_actual_profile_and_explicit_bundle_only(
    tmp_path: Path, profile: str, copy: bool
) -> None:
    source, _, _, _ = fixture(tmp_path)
    output = tmp_path / "product-data"
    script = Path(__file__).parents[4] / "scripts/export_runtime_release.py"
    arguments = [
        sys.executable,
        str(script),
        "--project-root",
        str(source),
        "--profile",
        profile,
        "--write-metadata",
    ]
    if copy:
        arguments.extend(["--bundle-output", str(output)])
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    expected = prepare_runtime_release(source, include_detailed=profile == "full")
    assert report["profile"] == profile
    assert report["release_sha256"] == expected.sha256
    assert report["payload_bytes"] == sum(
        entry.size_bytes for entry in expected.manifest.files.values()
    )
    assert report["index_bytes"] == len(expected.body)
    assert report["source_payloads_copied"] is copy
    assert report["uploaded"] is False
    assert Path(report["metadata_directory"]).is_dir()
    assert report["bundle_directory"] == (str(output) if copy else None)
    assert output.exists() is copy


def test_export_cli_keeps_full_profile_as_default(tmp_path: Path) -> None:
    source, _, _, _ = fixture(tmp_path)
    script = Path(__file__).parents[4] / "scripts/export_runtime_release.py"
    result = subprocess.run(
        [sys.executable, str(script), "--project-root", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["profile"] == "full"
    assert report["bundle_directory"] is None
    assert report["source_payloads_copied"] is False
