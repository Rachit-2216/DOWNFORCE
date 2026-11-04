"""Synthetic index-size regressions; no source objects or provider observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from app.main import create_app
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.manifest import ManifestLimits, RuntimeManifest
from fastapi.testclient import TestClient
from test_runtime_blob_api import OfflineReader, build, settings_for

LIMIT = 16 * 1024 * 1024
FILE_COUNT = 52_000


@pytest.fixture(scope="module")
def large_release() -> bytes:
    # Only metadata is synthetic. No indexed object is fabricated or persisted.
    digest = "a" * 64
    metadata = b':{"size_bytes":1,"sha256":"' + digest.encode() + b'"}'
    with BytesIO() as output:
        output.write(b'{"schema_version":1,"files":{')
        for index in range(FILE_COUNT):
            path = (
                f".downforce/detailed/canonical/2024-{index // 52:04d}-race/"
                f"revisions/{digest}/car-data-{index % 52:05d}.parquet"
            )
            if index:
                output.write(b",")
            output.write(json.dumps(path).encode())
            output.write(metadata)
        output.write(b"}}")
        body = output.getvalue()
    assert 11_775_260 < len(body) <= LIMIT
    return body


class ReleaseReader(OfflineReader):
    def __init__(self, body: bytes, *, closing: bool) -> None:
        super().__init__({"release.json": body})
        self.closing = closing
        self.bounds: list[int] = []

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        self.calls.append(relative_path)
        self.bounds.append(max_bytes)
        body = self.objects[relative_path]
        return self._chunks(relative_path, body) if self.closing else iter([body])

    def _chunks(self, relative_path: str, body: bytes) -> Iterator[bytes]:
        try:
            for offset in range(0, len(body), 64 * 1024):
                yield body[offset : offset + 64 * 1024]
        finally:
            self.closed.append(relative_path)


def test_authenticated_large_release_exceeds_old_bound_but_fits_fixed_default(
    large_release: bytes,
) -> None:
    pin = hashlib.sha256(large_release).hexdigest()
    with pytest.raises(StorageIntegrityError, match="body exceeds manifest limit"):
        RuntimeManifest.from_bytes(
            large_release, pin, limits=ManifestLimits(max_manifest_bytes=8 * 1024 * 1024)
        )
    manifest = RuntimeManifest.from_bytes(large_release, pin)
    assert manifest.sha256 == pin
    assert len(manifest.files) == FILE_COUNT
    assert all(
        entry.size_bytes == 1 and entry.sha256 == "a" * 64 for entry in manifest.files.values()
    )


@pytest.mark.parametrize("closing", [False, True], ids=["plain", "closing"])
def test_large_release_bootstraps_through_bounded_reader_and_health_stays_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    large_release: bytes,
    closing: bool,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("local writer constructed during Blob bootstrap")

    for name in (
        "DownforceRepository",
        "DetailedStore",
        "HistoricalArchiveStore",
        "AnalyticsEngine",
    ):
        monkeypatch.setattr(f"app.main.{name}", forbidden)
    reader = ReleaseReader(large_release, closing=closing)
    app = build(tmp_path, large_release, reader)
    manifest = app.state.runtime_blob_cache.manifest
    assert manifest.sha256 == hashlib.sha256(large_release).hexdigest()
    assert len(manifest.files) == FILE_COUNT
    assert TestClient(app).get("/health").status_code == 200
    assert reader.calls == ["release.json"]
    assert reader.bounds == [LIMIT]
    assert reader.closed == (["release.json"] if closing else [])
    assert not app.state.settings.project_root.exists()
    assert not app.state.repository.layout.project_root.exists()
    assert not any(app.state.runtime_blob_cache.release_root.rglob("*.parquet"))


def test_large_release_wrong_pin_fails_before_json_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    large_release: bytes,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("unauthenticated release reached JSON parsing")

    monkeypatch.setattr(json, "loads", forbidden)
    settings = settings_for(tmp_path, large_release).model_copy(
        update={"blob_release_sha256": "0" * 64}
    )
    reader = ReleaseReader(large_release, closing=True)
    with pytest.raises(StorageIntegrityError, match="checksum verification failed"):
        create_app(settings, object_reader=reader)
    assert reader.calls == reader.closed == ["release.json"]
    assert reader.bounds == [LIMIT]
    assert not settings.blob_cache_root.exists()
    assert not settings.project_root.exists()


def test_release_stream_over_sixteen_mib_stops_and_closes_before_json_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    large_release: bytes,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("oversized release stream reached JSON parsing")

    monkeypatch.setattr(json, "loads", forbidden)
    closed: list[bool] = []
    calls: list[tuple[str, int]] = []

    class OverBoundReader:
        def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
            calls.append((relative_path, max_bytes))
            chunk = b"x" * (64 * 1024)
            try:
                for _ in range(LIMIT // len(chunk)):
                    yield chunk
                yield b"x"
                pytest.fail("oversized release stream was consumed past the bound")
            finally:
                closed.append(True)

    settings = settings_for(tmp_path, large_release)
    with pytest.raises(StorageIntegrityError, match="release stream is unavailable or invalid"):
        create_app(settings, object_reader=OverBoundReader())
    assert calls == [("release.json", LIMIT)]
    assert closed == [True]
    assert not settings.blob_cache_root.exists()
    assert not settings.project_root.exists()
