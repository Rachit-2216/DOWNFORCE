"""Credential-free visitor serving uses genuine authenticated published artifacts."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from app.main import create_app
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.bundle import write_bundle
from downforce_core.runtime_blob.export import prepare_runtime_release
from fastapi.testclient import TestClient
from test_runtime_blob_api import canonical_model_fixture
from test_runtime_blob_config import isolated_settings


def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Any]:
    unit = Path(__file__).resolve().parents[3] / "packages/downforce_core/tests/unit"
    # Core fixture modules import siblings; API-only test collection must work too.
    monkeypatch.syspath_prepend(str(unit))
    from downforce_core.analytics import AnalyticsEngine

    source = tmp_path / "source"
    archive = importlib.import_module("test_analytics")._fixture_store(source)
    raw = archive.raw_root / "jolpica/jolpica-csv-sha256-fixture.zip"
    raw.parent.mkdir(parents=True)
    with ZipFile(raw, "w") as zipped:
        zipped.writestr("formula_one_season.csv", "id,year\n1,2000\n")
        zipped.writestr("formula_one_round.csv", "season_id,circuit_id,number\n1,1,1\n1,2,2\n")
        zipped.writestr("formula_one_circuit.csv", "id,reference\n1,circuit-one\n2,circuit-two\n")
    AnalyticsEngine(archive).rebuild_manifest()
    session = canonical_model_fixture(source, monkeypatch)
    release = prepare_runtime_release(source, include_detailed=False)
    bundle = write_bundle(release, tmp_path / "product-data")
    return source, bundle, session


def bundled_settings(tmp_path: Path, bundle: Path, **kwargs: Any) -> Any:
    return isolated_settings(
        storage_backend="bundled",
        environment="test",
        log_level="CRITICAL",
        project_root=tmp_path / "never-created-checkout",
        bundle_root=bundle,
        bundle_cache_root=tmp_path / "runtime",
        **kwargs,
    )


def test_bundle_factory_dispatches_before_local_writers_without_http_and_health_is_lazy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("local writer or cloud client constructed in bundled mode")

    for name in (
        "DownforceRepository",
        "DetailedStore",
        "HistoricalArchiveStore",
        "AnalyticsEngine",
    ):
        monkeypatch.setattr(f"app.main.{name}", forbidden)
    monkeypatch.setattr("app.core.runtime_blob.PrivateBlobReader", forbidden)
    app = create_app(bundled_settings(tmp_path, bundle))
    cache = app.state.runtime_blob_cache
    assert app.state.runtime_bundle_reader.manifest == cache.manifest
    assert not cache.release_root.exists()
    assert not app.state.settings.project_root.exists()
    assert TestClient(app).get("/health").status_code == 200
    assert not cache.release_root.exists()


def test_genuine_bundle_cold_warm_matches_original_routes_and_has_no_native_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, bundle, session = published(tmp_path, monkeypatch)
    app = create_app(bundled_settings(tmp_path, bundle))
    local = TestClient(create_app(isolated_settings(project_root=source, log_level="CRITICAL")))
    visitor = TestClient(app)
    identity = str(session.metadata.session_id)
    driver = str(session.drivers[0].driver_id)
    for path in (
        "/api/v1/sessions",
        f"/api/v1/sessions/{identity}",
        f"/api/v1/sessions/{identity}/state?time_ms=360000",
        f"/api/v1/sessions/{identity}/drivers/{driver}/intelligence?time_ms=360000",
        "/api/v1/catalog/seasons",
        "/api/v1/catalog/events?limit=100",
        "/api/v1/catalog/sessions/archive-2000-round-01-race/results",
        "/api/v1/analytics/status",
        "/api/v1/analytics/drivers",
        "/api/v1/analytics/circuits",
        "/api/v1/ml/status",
        "/api/v1/strategy/status",
    ):
        expected = local.get(path)
        assert expected.status_code == 200, (path, expected.text)
        for _ in range(2):
            actual = visitor.get(path)
            assert actual.status_code == 200, (path, actual.text)
            assert actual.json() == expected.json(), path
    path = f"/api/v1/sessions/{identity}/strategy/simulate"
    request = {
        "cursor_time_ms": 360_000,
        "driver_id": driver,
        "strategy": {"strategy_id": "stay", "label": "Stay", "actions": []},
        "scenario": {"scheduled_total_laps": 8},
        "simulation_count": 100,
        "seed": 2216,
    }
    expected = local.post(path, json=request)
    assert expected.status_code == 200, expected.text
    assert expected.json()["status"] == "available", expected.json()
    for _ in range(2):
        assert visitor.post(path, json=request).json() == expected.json()
    native = visitor.get("/api/v1/telemetry/sessions")
    assert native.status_code == 200
    assert native.json()["items"] == [] and native.json()["total"] == 0
    assert not app.state.settings.project_root.exists()


@pytest.mark.parametrize("failure", ["missing-root", "missing-pin", "missing-object", "bad-index"])
def test_bundle_startup_fails_closed_without_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)
    if failure == "missing-root":
        bundle = tmp_path / "absent"
    elif failure == "missing-pin":
        (bundle / "release.sha256").unlink()
    elif failure == "missing-object":
        next((bundle / "objects").iterdir()).unlink()
    else:
        (bundle / "release.json").write_bytes(b"{}")
    with pytest.raises(StorageIntegrityError):
        create_app(bundled_settings(tmp_path, bundle))
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "never-created-checkout").exists()


def test_bundle_optional_expected_pin_must_match_packaged_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)
    with pytest.raises(StorageIntegrityError):
        create_app(bundled_settings(tmp_path, bundle, bundle_release_sha256="a" * 64))
    assert not (tmp_path / "runtime").exists()


def test_bundle_object_checksum_mutation_is_lazy_integrity_error_not_empty_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)
    app = create_app(bundled_settings(tmp_path, bundle))
    manifest = app.state.runtime_bundle_reader.manifest
    name = manifest.paths(".downforce/normalized", suffix="/active.json")[0]
    entry = manifest.entry(name)
    obj = bundle / "objects" / f"{entry.sha256}.bin"
    obj.write_bytes(b"z" * entry.size_bytes)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    result = client.get("/api/v1/sessions")
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "canonical_data_incompatible"
    assert not app.state.settings.project_root.exists()


def test_vercel_entrypoint_defaults_to_bundle_without_credentials_or_checkout_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[3]
    checkout = tmp_path / "empty-checkout"
    checkout.mkdir()
    script = f"""
import runpy
from pathlib import Path
from downforce_core.runtime_blob.transport import PrivateBlobReader
from downforce_core.storage.layout import StorageLayout
from fastapi.testclient import TestClient
def forbidden(*args, **kwargs):
    raise AssertionError("local writer or cloud client constructed")
StorageLayout.__post_init__ = forbidden
PrivateBlobReader.__init__ = forbidden
app = runpy.run_path({str(root / "apps/api/index.py")!r})["app"]
assert app.state.settings.storage_backend == "bundled"
assert TestClient(app).get("/health").status_code == 200
assert list(Path.cwd().iterdir()) == []
print("verified")
"""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("DOWNFORCE_")
    }
    environment.update(
        VERCEL="1",
        PYTHONDONTWRITEBYTECODE="1",
        DOWNFORCE_BUNDLE_ROOT=str(bundle),
        DOWNFORCE_BUNDLE_CACHE_ROOT=str(tmp_path / "runtime"),
    )
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


def test_ingestion_sdk_remains_pinned_in_local_dev_and_optional_for_serving() -> None:
    root = Path(__file__).resolve().parents[3]
    core = tomllib.loads((root / "packages/downforce_core/pyproject.toml").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert not any(item.startswith("fastf1") for item in core["project"]["dependencies"])
    assert "requests>=2.31,<3" in core["project"]["dependencies"]
    assert core["project"]["optional-dependencies"]["ingestion"] == ["fastf1==3.8.3"]
    assert "fastf1==3.8.3" in project["dependency-groups"]["dev"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked_core = next(item for item in lock["package"] if item["name"] == "downforce-core")
    assert not any(item["name"] == "fastf1" for item in locked_core["dependencies"])
    assert locked_core["optional-dependencies"]["ingestion"] == [{"name": "fastf1"}]


def test_locked_production_export_excludes_sdk_only_dependencies() -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["uv", "export", "--locked", "--no-dev", "--format", "requirements-txt", "--no-hashes"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    requirements = {
        line.split("==")[0]
        for line in result.stdout.splitlines()
        if line and not line.startswith(("#", " ", "-"))
    }
    assert {
        "pandas",
        "numpy",
        "pyarrow",
        "fastapi",
        "pydantic-settings",
        "requests",
        "uvicorn",
    } <= requirements
    assert {"fastf1", "scipy", "matplotlib"}.isdisjoint(requirements)


def test_isolated_no_dev_environment_imports_the_actual_bundled_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, _ = published(tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.util
import runpy
for name in ("fastf1", "scipy", "matplotlib"):
    assert importlib.util.find_spec(name) is None, name
app = runpy.run_path({str(root / "apps/api/index.py")!r})["app"]
assert app.state.settings.storage_backend == "bundled"
assert app.state.runtime_bundle_reader.manifest.files
assert not app.state.runtime_blob_cache.release_root.exists()
print("isolated serving API imported without ingestion SDK")
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DOWNFORCE_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update(
        VERCEL="1",
        PYTHONDONTWRITEBYTECODE="1",
        DOWNFORCE_BUNDLE_ROOT=str(bundle),
        DOWNFORCE_BUNDLE_CACHE_ROOT=str(tmp_path / "isolated-runtime"),
    )
    result = subprocess.run(
        ["uv", "run", "--isolated", "--locked", "--no-dev", "python", "-c", script],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated serving API imported without ingestion SDK"


def test_runtime_factories_refuse_the_other_storage_mode() -> None:
    from app.core.runtime_blob import create_blob_app, create_bundled_app

    with pytest.raises(StorageIntegrityError, match="bundled"):
        create_bundled_app(isolated_settings(), create_app)
    with pytest.raises(StorageIntegrityError, match="cloud"):
        create_blob_app(isolated_settings(storage_backend="bundled"), create_app)
