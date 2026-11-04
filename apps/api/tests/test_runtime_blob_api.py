"""Offline runtime integration against genuine published product fixtures."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from app.core.config import Settings
from app.main import create_app
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.manifest import ManifestLimits
from downforce_core.runtime_blob.transport import PrivateBlobReader
from fastapi.testclient import TestClient
from test_runtime_blob_config import cloud_values, isolated_settings
from test_telemetry_api import publish as publish_telemetry

_cache_directories: dict[Path, TemporaryDirectory[str]] = {}


def runtime_cache_root(tmp_path: Path) -> Path:
    """Keep mutable caches in genuine OS temp, independently of source-fixture drive."""
    if tmp_path not in _cache_directories:
        _cache_directories[tmp_path] = TemporaryDirectory(prefix="downforce-blob-api-test-")
    return Path(_cache_directories[tmp_path].name) / "runtime"


@pytest.fixture(autouse=True)
def cleanup_runtime_cache_directories() -> Iterator[None]:
    before = set(_cache_directories)
    try:
        yield
    finally:
        for key in set(_cache_directories) - before:
            _cache_directories.pop(key).cleanup()


def publish(source: Path) -> Any:
    source.mkdir(parents=True, exist_ok=True)
    return publish_telemetry(source)


class OfflineReader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[str] = []
        self.closed: list[str] = []

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        self.calls.append(relative_path)
        try:
            yield self.objects[relative_path]
        finally:
            self.closed.append(relative_path)


def release(source: Path, *, exclude: tuple[str, ...] = ()) -> tuple[bytes, OfflineReader]:
    prefixes = (
        ".downforce/detailed/canonical/",
        ".downforce/normalized/",
        ".downforce/aliases/",
        ".downforce/archive/sessions/",
        "artifacts/ml/",
    )
    exact = {
        ".downforce/archive/catalog/historical-catalog.json",
        ".downforce/analytics/manifest.json",
        ".downforce/analytics/driver-race.parquet",
        ".downforce/analytics/circuit-identities.json",
    }
    objects = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".parquet"}
        and path.relative_to(source).as_posix() not in exclude
        and (
            path.relative_to(source).as_posix().startswith(prefixes)
            or path.relative_to(source).as_posix() in exact
        )
    }
    body = json.dumps(
        {
            "schema_version": 1,
            "files": {
                path: {"size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for path, value in objects.items()
            },
        }
    ).encode()
    return body, OfflineReader({**objects, "release.json": body})


def settings_for(tmp_path: Path, body: bytes) -> Settings:
    return isolated_settings(
        environment="test",
        log_level="CRITICAL",
        project_root=tmp_path / "readonly-checkout",
        blob_cache_root=runtime_cache_root(tmp_path),
        **{**cloud_values(), "blob_release_sha256": hashlib.sha256(body).hexdigest()},
    )


def build(tmp_path: Path, body: bytes, reader: OfflineReader, **kwargs: Any) -> Any:
    return create_app(settings_for(tmp_path, body), object_reader=reader, **kwargs)


def test_factory_dispatches_before_local_writers_and_health_is_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("local constructor reached in Blob mode")

    for name in (
        "DownforceRepository",
        "DetailedStore",
        "HistoricalArchiveStore",
        "AnalyticsEngine",
    ):
        monkeypatch.setattr(f"app.main.{name}", forbidden)
    app = build(tmp_path, body, reader)
    assert reader.calls == ["release.json"]
    assert not app.state.settings.project_root.exists()
    assert not app.state.repository.layout.project_root.exists()
    assert TestClient(app).get("/health").status_code == 200
    assert reader.closed == ["release.json"]


def test_catalog_enumerates_index_only_and_fetches_no_sensor_tables(tmp_path: Path) -> None:
    local, identity, revision = publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    # An unindexed genuine leftover is neither advertised nor removed.
    leftover = app.state.telemetry_service.store.root / "unindexed" / "active.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes((local.root / identity / "active.json").read_bytes())
    api = TestClient(app)
    cold = api.get("/api/v1/telemetry/sessions")
    assert cold.status_code == 200, cold.text
    assert cold.json()["total"] == 1
    assert cold.json()["items"][0]["revision"] == revision
    assert cold.json()["items"][0]["drivers"] == ["1", "44"]
    assert all("car-data" not in path and "pos-data" not in path for path in reader.calls)
    assert leftover.exists()
    calls = list(reader.calls)
    assert api.get("/api/v1/telemetry/sessions").json() == cold.json()
    assert reader.calls == calls


def test_catalog_orders_session_identities_not_object_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    store, identity, revision = publish(source)
    original = store.manifest(identity, revision=revision)
    tables = {
        name: store.read_table(identity, name, revision=revision) for name in original["tables"]
    }
    # Object-path sorting puts '-' before '/', reversing these real session types.
    for session_type in ("sprint", "sprint-shootout", "sprint-qualifying"):
        native_id = f"2024-01-{session_type}"
        store.publish(
            native_id,
            tables,
            {
                "session": {
                    **original["metadata"]["session"],
                    "session_id": native_id,
                    "session_type": session_type,
                }
            },
        )
    body, reader = release(source)
    api = TestClient(build(tmp_path, body, reader))
    local = TestClient(create_app(isolated_settings(project_root=source)))
    expected = local.get("/api/v1/telemetry/sessions").json()
    assert [row["session_id"] for row in expected["items"]] == [
        "2024-01-race",
        "2024-01-sprint",
        "2024-01-sprint-qualifying",
        "2024-01-sprint-shootout",
    ]
    for _ in range(2):
        actual = api.get("/api/v1/telemetry/sessions")
        assert actual.status_code == 200, actual.text
        assert actual.json() == expected
        for offset in range(4):
            query = f"/api/v1/telemetry/sessions?offset={offset}&limit=1"
            assert api.get(query).json() == local.get(query).json()


def test_telemetry_pins_before_probes_and_preserves_trace_math(tmp_path: Path) -> None:
    _, identity, revision = publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    api = TestClient(build(tmp_path, body, reader))
    path = f"/api/v1/telemetry/sessions/{identity}"
    laps = api.get(path + "/laps", params={"driver": "44", "revision": revision})
    assert laps.status_code == 200, laps.text
    assert [row["lap_number"] for row in laps.json()["items"]] == [1, 2]
    params: dict[str, str | int] = {"driver": "44", "lap": 1, "channels": "speed_kmh,gear,drs_raw"}
    trace = api.get(path + "/trace", params=params)
    assert trace.status_code == 200, trace.text
    local_api = TestClient(create_app(isolated_settings(project_root=tmp_path / "source")))
    assert trace.json() == local_api.get(path + "/trace", params=params).json()
    calls = list(reader.calls)
    assert api.get(path + "/trace", params=params).json() == trace.json()
    assert reader.calls == calls
    assert all("car-data-1.parquet" not in item for item in reader.calls)


@pytest.mark.parametrize(
    "identity,revision,status",
    [
        ("bad!", None, 422),
        ("2024-01-race", "bad", 422),
        ("missing-race", None, 404),
        ("2024-01-race", "f" * 64, 404),
    ],
)
def test_invalid_or_unindexed_telemetry_pin_has_no_io(
    tmp_path: Path,
    identity: str,
    revision: str | None,
    status: int,
) -> None:
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    api = TestClient(build(tmp_path, body, reader))
    params = {"driver": "44"}
    if revision is not None:
        params["revision"] = revision
    response = api.get(f"/api/v1/telemetry/sessions/{identity}/laps", params=params)
    assert response.status_code == status
    assert reader.calls == ["release.json"]


@pytest.mark.parametrize("failure", ["pin", "oversize", "chunk", "malformed", "reader-error"])
def test_bootstrap_rejects_unauthenticated_or_bad_stream_before_cache(
    tmp_path: Path,
    failure: str,
) -> None:
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    settings = settings_for(tmp_path, body)
    if failure == "malformed":
        settings = settings.model_copy(
            update={"blob_release_sha256": hashlib.sha256(b"{}").hexdigest()}
        )
    closed: list[bool] = []

    class BadReader:
        def iter_bytes(self, _path: str, *, max_bytes: int) -> Iterator[bytes]:
            assert max_bytes == ManifestLimits().max_manifest_bytes
            try:
                if failure == "reader-error":
                    raise RuntimeError("offline-test-secret")
                yield {
                    "pin": b"tampered",
                    "oversize": b"x" * (max_bytes + 1),
                    "chunk": "not-bytes",
                    "malformed": b"{}",
                }[failure]  # type: ignore[misc]
            finally:
                closed.append(True)

    with pytest.raises(StorageIntegrityError) as caught:
        create_app(settings, object_reader=BadReader())
    assert "offline-test-secret" not in str(caught.value)
    assert closed == [True]
    assert not settings.blob_cache_root.exists()
    assert not settings.project_root.exists()


def test_raw_manifest_injection_still_authenticates(tmp_path: Path) -> None:
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    assert (
        TestClient(build(tmp_path, body, reader, manifest_body=body)).get("/health").status_code
        == 200
    )
    assert reader.calls == []
    with pytest.raises(StorageIntegrityError):
        build(tmp_path, body, reader, manifest_body=b"bad")


def test_optional_models_fail_closed_without_hydration_or_affecting_telemetry(
    tmp_path: Path,
) -> None:
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    api = TestClient(build(tmp_path, body, reader))
    strategy = api.get("/api/v1/strategy/status")
    assert strategy.status_code == 200, strategy.text
    assert strategy.json()["reason"] == "missing_or_corrupt_model"
    ml = api.get("/api/v1/ml/status")
    assert ml.status_code == 200, ml.text
    assert ml.json()["availability"] == "unavailable"
    assert reader.calls == ["release.json"]
    assert api.get("/health").status_code == 200
    assert api.get("/api/v1/telemetry/sessions").json()["total"] == 1


def test_runtime_module_exists() -> None:
    assert importlib.import_module("app.core.runtime_blob")


def analytics_fixture(source: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    unit = Path(__file__).resolve().parents[3] / "packages/downforce_core/tests/unit"
    monkeypatch.syspath_prepend(str(unit))
    _fixture_store = importlib.import_module("test_analytics")._fixture_store
    from downforce_core.analytics import AnalyticsEngine
    from downforce_core.analytics.circuits import CircuitIdentityResolver

    store = _fixture_store(source)
    catalog = store.load_catalog()
    identities = {(event.season, event.round_number): "fixture-circuit" for event in catalog.events}
    engine = AnalyticsEngine(store, circuit_resolver=CircuitIdentityResolver(identities))
    engine.rebuild_manifest()
    (source / ".downforce/analytics/circuit-identities.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identities": [
                    {"season": key[0], "round_number": key[1], "circuit_id": value}
                    for key, value in sorted(identities.items())
                ],
            }
        ),
        encoding="utf-8",
    )
    return engine


def test_precomputed_analytics_and_catalog_match_local_without_archive_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = analytics_fixture(tmp_path / "source", monkeypatch)
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    assert reader.calls == ["release.json"]
    local_app = create_app(isolated_settings(project_root=tmp_path / "source"))
    local = TestClient(local_app)
    # The fixture resolver represents the compact published provider identities.
    local_app.state.analytics_service.engine = engine
    api = TestClient(app)
    year = engine.snapshot().catalog.events[0].season
    for path in (
        "/api/v1/catalog/seasons",
        "/api/v1/analytics/status",
        f"/api/v1/analytics/seasons/{year}",
        "/api/v1/analytics/drivers",
        "/api/v1/analytics/circuits",
    ):
        result = api.get(path)
        assert result.status_code == 200, result.text
        assert result.json() == local.get(path).json()
    calls = list(reader.calls)
    assert (
        api.get("/api/v1/analytics/status").json() == local.get("/api/v1/analytics/status").json()
    )
    assert calls == reader.calls
    assert not any("/archive/sessions/" in path for path in reader.calls)
    assert app.state.analytics_service.engine.circuit_resolver.provider_identity_count() == len(
        engine.circuit_resolver.identities
    )


@pytest.mark.parametrize(
    "missing", ["manifest.json", "driver-race.parquet", "circuit-identities.json"]
)
def test_missing_analytics_does_not_rebuild_or_stop_other_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    analytics_fixture(tmp_path / "source", monkeypatch)
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source", exclude=(f".downforce/analytics/{missing}",))
    api = TestClient(build(tmp_path, body, reader))
    assert api.get("/health").status_code == 200
    assert api.get("/api/v1/analytics/status").status_code == 409
    assert not any("/archive/sessions/" in path for path in reader.calls)
    assert api.get("/api/v1/telemetry/sessions").json()["total"] == 1
    assert api.get("/api/v1/strategy/status").json()["reason"] == "missing_or_corrupt_model"


def test_analytics_rejects_compact_mapping_count_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analytics_fixture(tmp_path / "source", monkeypatch)
    mapping = tmp_path / "source/.downforce/analytics/circuit-identities.json"
    payload = json.loads(mapping.read_text("utf-8"))
    payload["identities"].pop()
    mapping.write_text(json.dumps(payload), encoding="utf-8")
    body, reader = release(tmp_path / "source")
    api = TestClient(build(tmp_path, body, reader))
    assert api.get("/api/v1/analytics/status").status_code == 409
    assert api.get("/health").status_code == 200


def test_runtime_analytics_forbids_force_build_and_publication_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analytics_fixture(tmp_path / "source", monkeypatch)
    body, reader = release(tmp_path / "source")
    engine = build(tmp_path, body, reader).state.analytics_service.engine
    for operation in (
        lambda: engine.snapshot(force=True),
        engine.rebuild_manifest,
        lambda: engine._build_snapshot(None),
    ):
        with pytest.raises(StorageIntegrityError, match="read-only"):
            operation()
    assert reader.calls == ["release.json"]


def test_genuine_model_loading_matches_local_and_strategy_constructor_has_live_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downforce_core.ml.artifacts import ArtifactStore
    from downforce_core.strategy.engine import StrategyEngine

    publish(tmp_path / "source")
    # Re-publish an existing compatible frozen JSON bundle only; no training.
    root = Path(__file__).resolve().parents[3]
    ArtifactStore(tmp_path / "source").publish(ArtifactStore(root).load())
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    cache = app.state.runtime_blob_cache
    constructed: list[bool] = []

    class CheckedEngine(StrategyEngine):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert cache._state.depth > 0
            assert len(cache._state.leased) == 2
            super().__init__(*args, **kwargs)
            constructed.append(True)

    module = importlib.import_module("app.services.runtime_blob_services")
    monkeypatch.setattr(module, "StrategyEngine", CheckedEngine, raising=False)
    api = TestClient(app)
    local = TestClient(create_app(isolated_settings(project_root=tmp_path / "source")))
    for path in ("/api/v1/strategy/status", "/api/v1/ml/status"):
        actual = api.get(path)
        assert actual.status_code == 200, actual.text
        assert actual.json() == local.get(path).json()
        assert actual.json()["availability"] == "available"
    assert constructed == [True]
    assert cache._state.depth == 0 and not cache._state.leased
    calls = list(reader.calls)
    assert api.get("/api/v1/strategy/status").json() == local.get("/api/v1/strategy/status").json()
    assert reader.calls == calls
    assert len([path for path in calls if path.startswith("artifacts/ml/")]) == 2


def test_bootstrap_authorization_denial_is_latched_and_not_retried(tmp_path: Path) -> None:
    publish(tmp_path / "source")
    body, _ = release(tmp_path / "source")
    calls: list[str] = []
    closed: list[bool] = []

    class Denial:
        status_code = 403

        def close(self) -> None:
            closed.append(True)

        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            pytest.fail("denial body must not be read")
            yield b""

    def request(url: str, **_kwargs: Any) -> Denial:
        calls.append(url)
        return Denial()

    reader = PrivateBlobReader(
        cloud_values()["blob_origin"], "releases/frozen", "offline-test-secret", request=request
    )
    for _ in range(2):
        with pytest.raises(StorageIntegrityError) as caught:
            create_app(settings_for(tmp_path, body), object_reader=reader)
        assert "offline-test-secret" not in str(caught.value)
    assert len(calls) == 1 and closed == [True]
    assert not runtime_cache_root(tmp_path).exists()


def test_root_entrypoint_and_module_app_import_do_not_write_checkout(tmp_path: Path) -> None:
    publish(tmp_path / "source")
    body, _ = release(tmp_path / "source")
    root = Path(__file__).resolve().parents[3]
    checkout = tmp_path / "readonly-checkout"
    checkout.mkdir()
    script = f"""
import json, runpy
from pathlib import Path
from downforce_core.runtime_blob.transport import PrivateBlobReader
from downforce_core.storage.layout import StorageLayout
from fastapi.testclient import TestClient
def forbidden(*args, **kwargs):
    raise AssertionError("original local writer constructed")
StorageLayout.__post_init__ = forbidden
PrivateBlobReader.iter_bytes = lambda self, path, max_bytes: iter([{body!r}])
result = runpy.run_path({str(root / "apps/api/index.py")!r})
assert result["app"].state.settings.storage_backend == "vercel_blob"
assert TestClient(result["app"]).get("/health").status_code == 200
assert list(Path.cwd().iterdir()) == []
print("verified")
"""
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "VERCEL": "1",
        "DOWNFORCE_STORAGE_BACKEND": "vercel_blob",
        "DOWNFORCE_BLOB_ORIGIN": str(cloud_values()["blob_origin"]),
        "DOWNFORCE_BLOB_RELEASE_PREFIX": "releases/frozen",
        "DOWNFORCE_BLOB_RELEASE_SHA256": hashlib.sha256(body).hexdigest(),
        "DOWNFORCE_BLOB_TOKEN": "offline-test-secret",
        "DOWNFORCE_BLOB_CACHE_ROOT": str(runtime_cache_root(tmp_path)),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified"


def canonical_model_fixture(source: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    unit = Path(__file__).resolve().parents[3] / "packages/downforce_core/tests/unit"
    monkeypatch.syspath_prepend(str(unit))
    _eligible_session = importlib.import_module("test_ml")._eligible_session
    from dataclasses import replace

    from downforce_core.ml.artifacts import ArtifactStore
    from downforce_core.replay import build_timeline
    from downforce_core.storage import DownforceRepository

    root = Path(__file__).resolve().parents[3]
    bundle = ArtifactStore(root).load()
    pit = bundle["pit_loss"]
    assert isinstance(pit, dict)
    supported_circuit = pit["supported_circuits"][0]
    session = _eligible_session()
    rival_id = session.drivers[1].driver_id
    rival_lap = next(lap for lap in session.laps if lap.driver_id == rival_id)
    rival_position = next(row for row in session.race_positions if row.driver_id == rival_id)
    background_laps = tuple(
        replace(
            rival_lap,
            lap_number=number,
            lap_start_time_ms=91_000 + (number - 2) * 90_000,
            lap_end_time_ms=179_000 + (number - 2) * 90_000,
            lap_time_ms=90_000,
            tyre_life_laps=float(number + 2),
            is_accurate=True,
            is_generated=False,
            is_deleted=False,
            raw_track_status="1",
        )
        for number in range(2, 7)
    )
    background_positions = tuple(
        replace(rival_position, session_time_ms=lap.lap_end_time_ms, lap_number=lap.lap_number)
        for lap in background_laps
    )
    session = replace(
        session,
        metadata=replace(session.metadata, circuit_name=supported_circuit),
        laps=session.laps + background_laps,
        race_positions=session.race_positions + background_positions,
    )
    repository = DownforceRepository(source)
    repository.write_session(session, "fixture", events=build_timeline(session).events)
    ArtifactStore(source).publish(bundle)
    return session


def test_real_replay_inference_and_strategy_parity_compute_outside_cache_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downforce_core.ml.inference import MLInferenceEngine
    from downforce_core.strategy.composition import ModelComposition

    session = canonical_model_fixture(tmp_path / "source", monkeypatch)
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    cache = app.state.runtime_blob_cache
    api = TestClient(app)
    local = TestClient(
        create_app(isolated_settings(project_root=tmp_path / "source", log_level="CRITICAL"))
    )
    identity = str(session.metadata.session_id)
    driver = str(session.drivers[0].driver_id)
    compute: list[str] = []
    lap_prediction = ModelComposition.lap_prediction
    predict = MLInferenceEngine.predict

    def checked_lap(self: Any, **kwargs: Any) -> Any:
        assert cache._state.depth == 0 and not cache._state.leased
        compute.append("strategy")
        return lap_prediction(self, **kwargs)

    def checked_predict(self: Any, *args: Any, **kwargs: Any) -> Any:
        assert cache._state.depth == 0 and not cache._state.leased
        compute.append("ml")
        return predict(self, *args, **kwargs)

    monkeypatch.setattr(ModelComposition, "lap_prediction", checked_lap)
    monkeypatch.setattr(MLInferenceEngine, "predict", checked_predict)
    for path in (
        "/api/v1/sessions",
        f"/api/v1/sessions/{identity}",
        f"/api/v1/sessions/{identity}/state?time_ms=360000",
        f"/api/v1/sessions/{identity}/drivers/{driver}/intelligence?time_ms=360000",
    ):
        actual = api.get(path)
        assert actual.status_code == 200, actual.text
        assert actual.json() == local.get(path).json()
        calls = list(reader.calls)
        assert api.get(path).json() == actual.json()
        assert reader.calls == calls
    request = {
        "cursor_time_ms": 360_000,
        "driver_id": driver,
        "strategy": {"strategy_id": "stay", "label": "Stay", "actions": []},
        "scenario": {"scheduled_total_laps": 8},
        "simulation_count": 100,
        "seed": 2216,
    }
    path = f"/api/v1/sessions/{identity}/strategy/simulate"
    result = api.post(path, json=request)
    assert result.status_code == 200, result.text
    assert result.json() == local.post(path, json=request).json()
    assert result.json()["status"] == "available", result.json()
    assert "ml" in compute and "strategy" in compute
    assert not cache._state.leased and cache._state.depth == 0


@pytest.mark.parametrize("part", ["registry", "bundle", "mapping", "derived"])
def test_optional_corruption_is_safe_and_unrelated_families_remain_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    part: str,
) -> None:
    analytics_fixture(tmp_path / "source", monkeypatch)
    canonical_model_fixture(tmp_path / "source", monkeypatch)
    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    selected = {
        "registry": "artifacts/ml/registry.json",
        "bundle": next(path for path in reader.objects if path.startswith("artifacts/ml/bundles/")),
        "mapping": ".downforce/analytics/circuit-identities.json",
        "derived": ".downforce/analytics/driver-race.parquet",
    }[part]
    reader.objects[selected] = b"offline-test-secret"
    api = TestClient(build(tmp_path, body, reader))
    assert api.get("/health").status_code == 200
    assert api.get("/api/v1/telemetry/sessions").json()["total"] == 1
    assert api.get("/api/v1/catalog/seasons").status_code == 200
    if part in {"registry", "bundle"}:
        assert api.get("/api/v1/strategy/status").json()["reason"] == "missing_or_corrupt_model"
        ml = api.get("/api/v1/ml/status")
        assert ml.json()["availability"] == "unavailable"
        assert "offline-test-secret" not in ml.text
        assert api.get("/api/v1/analytics/status").status_code == 200
    else:
        analytics = api.get("/api/v1/analytics/status")
        assert analytics.status_code == 409
        assert "offline-test-secret" not in analytics.text
        assert api.get("/api/v1/strategy/status").json()["availability"] == "available"
    assert not any("/raw/" in path or "Index.json" in path for path in reader.calls)


@pytest.mark.parametrize("metadata", [[], {"session": []}])
def test_telemetry_catalog_malformed_mapper_metadata_has_safe_integrity_error(
    tmp_path: Path,
    metadata: Any,
) -> None:
    local, identity, revision = publish(tmp_path / "source")
    path = local.root / identity / "revisions" / revision / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["metadata"] = metadata
    semantic = {key: value for key, value in manifest.items() if key != "revision"}
    new_revision = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["revision"] = new_revision
    path.write_text(json.dumps(manifest), encoding="utf-8")
    path.parent.rename(path.parent.with_name(new_revision))
    (local.root / identity / "active.json").write_text(
        json.dumps({"revision": new_revision}), encoding="utf-8"
    )
    body, reader = release(tmp_path / "source")
    api = TestClient(build(tmp_path, body, reader))
    response = api.get("/api/v1/telemetry/sessions")
    assert response.status_code == 409
    assert "AttributeError" not in response.text
    assert api.get("/health").status_code == 200


def test_bootstrap_accepts_plain_iterator_without_close(tmp_path: Path) -> None:
    publish(tmp_path / "source")
    body, _ = release(tmp_path / "source")

    class Reader:
        def iter_bytes(self, path: str, *, max_bytes: int) -> Iterator[bytes]:
            return iter([body])

    api = TestClient(create_app(settings_for(tmp_path, body), object_reader=Reader()))
    assert api.get("/health").status_code == 200


def test_inherited_telemetry_pin_files_are_live_leased(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.telemetry_service import TelemetryService

    _, identity, revision = publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    cache = app.state.runtime_blob_cache
    original = TelemetryService._pin
    probed: list[bool] = []

    def checked(self: Any, session_id: str, pinned: str | None) -> str:
        assert cache._state.depth > 0
        assert len(cache._state.leased) == (2 if pinned is None else 1)
        probed.append(True)
        return original(self, session_id, pinned)

    monkeypatch.setattr(TelemetryService, "_pin", checked)
    assert app.state.telemetry_service._pin(identity, None) == revision
    assert app.state.telemetry_service._pin(identity, revision) == revision
    assert probed == [True, True]
    assert not cache._state.leased and cache._state.depth == 0


def test_invalid_route_queries_and_direct_pin_do_not_hydrate(tmp_path: Path) -> None:
    from fastapi import HTTPException

    publish(tmp_path / "source")
    body, reader = release(tmp_path / "source")
    app = build(tmp_path, body, reader)
    api = TestClient(app)
    for path in (
        "/api/v1/telemetry/sessions/2024-01-race/laps?driver=0",
        "/api/v1/telemetry/sessions/2024-01-race/trace?driver=44&lap=0",
        "/api/v1/telemetry/sessions/2024-01-race/trace?driver=44&lap=1&channels=invalid",
        "/api/v1/analytics/drivers?start_season=2024&end_season=2023",
        "/api/v1/sessions/bad!/state?time_ms=10",
    ):
        assert api.get(path).status_code == 422
    for identity, revision in (("bad!", None), ("2024-01-race", "invalid")):
        with pytest.raises(HTTPException) as caught:
            app.state.telemetry_service._pin(identity, revision)
        assert caught.value.status_code == 422
    assert reader.calls == ["release.json"]
