"""Opt-in offline real-corpus parity; no SDK, provider requests, writes or ingestion.

Run with DOWNFORCE_OFFLINE_RELEASE_PROOF=1 and pytest -s on this file only.
The runtime cache is an owned ordinary OS temporary child, never the checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from app.main import create_app
from downforce_core.analytics.circuits import CircuitIdentityResolver
from downforce_core.analytics.storage import AnalyticsDerivedStore
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.runtime_blob.export import prepare_runtime_release
from downforce_core.runtime_blob.stores_analytics import BlobCircuitIdentityResolver
from downforce_core.storage.parquet import file_sha256
from downforce_core.telemetry.reader import TelemetryReader
from fastapi.testclient import TestClient
from test_runtime_blob_config import isolated_settings
from test_telemetry_api import publish


def _representative_lap(
    reader: TelemetryReader,
    identity: str,
    revision: str,
    drivers: list[str],
    *,
    supports: Callable[[str, int], bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    for driver in drivers:
        _, laps = reader.laps(identity, driver, revision=revision)
        for lap in laps:
            if (
                lap["reliable"]
                and not lap["deleted"]
                and not lap["provider_generated"]
                and not lap["pit_in"]
                and not lap["pit_out"]
                and lap["car_available"]
                and lap["timing_available"]
            ):
                if supports is None or supports(driver, lap["lap_number"]):
                    return driver, lap
    raise ValueError("No eligible native telemetry lap in the supplied drivers")


def test_representative_lap_searches_drivers_without_weakening_quality(tmp_path: Path) -> None:
    import pyarrow as pa  # type: ignore[import-untyped]

    store, identity, revision = publish(tmp_path)
    manifest = store.manifest(identity, revision=revision)
    tables = {
        name: store.read_table(identity, name, revision=revision) for name in manifest["tables"]
    }
    laps = tables["laps"]
    tables["laps"] = laps.set_column(
        laps.schema.get_field_index("is_accurate"),
        "is_accurate",
        pa.array([number == "44" for number in laps["driver_number"].to_pylist()]),
    )
    revision = store.publish(identity, tables, manifest["metadata"])
    driver, selected = _representative_lap(TelemetryReader(store), identity, revision, ["1", "44"])
    assert driver == "44" and selected["lap_number"] == 1
    assert selected["reliable"] and selected["timing_available"]
    with pytest.raises(ValueError, match="No eligible native telemetry lap"):
        _representative_lap(TelemetryReader(store), identity, revision, ["1"])


def test_representative_lap_requires_operation_support(tmp_path: Path) -> None:
    store, identity, revision = publish(tmp_path)
    driver, lap = _representative_lap(
        TelemetryReader(store),
        identity,
        revision,
        ["44"],
        supports=lambda _driver, number: number == 2,
    )
    assert driver == "44" and lap["lap_number"] == 2
    with pytest.raises(ValueError, match="No eligible native telemetry lap"):
        _representative_lap(
            TelemetryReader(store),
            identity,
            revision,
            ["44"],
            supports=lambda _driver, _number: False,
        )


@pytest.mark.skipif(
    os.getenv("DOWNFORCE_OFFLINE_RELEASE_PROOF") != "1", reason="explicit real-corpus proof"
)
def test_actual_product_release_cold_warm_semantic_parity() -> None:
    root = Path(__file__).resolve().parents[3]
    protected = [root / "README.md", root / "data/README.md"]
    protected.extend((root / "apps/api/app").rglob("*.py"))
    protected.extend((root / "packages/downforce_core/src").rglob("*.py"))
    protected.extend((root / "artifacts/ml").rglob("*.json"))
    before = {path: (file_sha256(path), path.stat().st_mtime_ns) for path in protected}
    release = prepare_runtime_release(root)
    archive = HistoricalArchiveStore(root)
    catalog = archive.load_catalog()
    snapshot = AnalyticsDerivedStore(root).load(catalog.source_revision)
    assert snapshot is not None and len(snapshot.observations) == 10814
    original_identities = CircuitIdentityResolver.from_store(archive).identities
    assert len(original_identities) == 1172
    compact = json.loads(release.generated_files[".downforce/analytics/circuit-identities.json"])
    assert {
        (row["season"], row["round_number"]): row["circuit_id"] for row in compact["identities"]
    } == original_identities
    assert len(release.manifest.paths(".downforce/archive/sessions", suffix="/active.json")) == 515
    assert len(release.manifest.paths(".downforce/normalized", suffix="/active.json")) == 13
    assert len(release.manifest.paths(".downforce/aliases")) == 13
    assert len(release.manifest.paths(".downforce/detailed/canonical", suffix="/active.json")) == 42
    source_before = {
        path: (entry.sha256, release.source_files[path].stat().st_mtime_ns)
        for path, entry in release.manifest.files.items()
        if path in release.source_files
    }
    checked: list[str] = []
    peak = 0
    intrinsic_timing_fields: set[str] = set()

    def semantic(value: Any) -> Any:
        if isinstance(value, dict):
            # Only intrinsic execution timing, not timestamps, bounds, models or values.
            return {
                key: semantic(item)
                for key, item in value.items()
                if key not in intrinsic_timing_fields
            }
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return value

    with TemporaryDirectory(prefix="downforce-offline-parity-") as temporary:
        local_app = create_app(
            isolated_settings(project_root=root, environment="test", log_level="CRITICAL")
        )
        cloud_app = create_app(
            isolated_settings(
                project_root=root,
                environment="test",
                log_level="CRITICAL",
                storage_backend="vercel_blob",
                blob_origin="https://offline-test.private.blob.vercel-storage.com",
                blob_release_prefix=f"releases/{release.sha256}",
                blob_release_sha256=release.sha256,
                blob_token="offline-placeholder-not-a-credential",
                blob_cache_root=Path(temporary) / "cache",
            ),
            object_reader=release.reader(),
            manifest_body=release.body,
        )
        cache = cloud_app.state.runtime_blob_cache
        assert not cache.root.is_relative_to(root)
        assert BlobCircuitIdentityResolver.from_cache(cache).identities == original_identities
        local, blob = TestClient(local_app), TestClient(cloud_app)

        def compare(
            path: str, *, body: dict[str, Any] | None = None, expected_status: int = 200
        ) -> dict[str, Any]:
            nonlocal peak
            expected = local.get(path) if body is None else local.post(path, json=body)
            assert expected.status_code == expected_status, (
                path,
                expected.status_code,
                expected.text,
            )
            for temperature in ("cold", "warm"):
                actual = blob.get(path) if body is None else blob.post(path, json=body)
                assert actual.status_code == expected.status_code, (
                    path,
                    actual.status_code,
                    actual.text,
                )
                assert semantic(actual.json()) == semantic(expected.json()), (path, temperature)
                peak = max(
                    peak, sum(p.stat().st_size for p in cache.root.rglob("*") if p.is_file())
                )
                assert peak <= 512 * 1024**2
            checked.append(path)
            print("PARITY", path, flush=True)
            return cast(dict[str, Any], expected.json())

        for path in (
            "/health",
            "/api/v1/catalog/seasons",
            "/api/v1/catalog/events?limit=100",
            "/api/v1/analytics/status",
            "/api/v1/analytics/drivers?limit=100",
            "/api/v1/analytics/constructors?limit=100",
            "/api/v1/analytics/seasons/2024",
            "/api/v1/ml/status",
            "/api/v1/strategy/status",
        ):
            compare(path)
        archive_identity = next(
            session.session_id
            for event in catalog.events
            for session in event.sessions
            if session.data_revision is not None
        )
        for endpoint in ("capabilities", "results", "laps", "pit-stops"):
            compare(f"/api/v1/catalog/sessions/{archive_identity}/{endpoint}")
        replay = (
            "session-2023-event-hungarian-grand-prix-sha256-"
            "43bbd9213155f6f4dbc0adde0498e7b9493c27d1a2694b0393a201ad4ab91b8c-type-race"
        )
        compare(f"/api/v1/sessions/{replay}")
        compare(f"/api/v1/sessions/{replay}/state?lap=5")
        drivers = compare(f"/api/v1/sessions/{replay}/drivers")["items"]
        driver = next(
            (row["driver_id"] for row in drivers if row["racing_number"] == 1),
            drivers[0]["driver_id"],
        )
        laps = compare(f"/api/v1/sessions/{replay}/laps?driver_id={driver}&from_lap=5&to_lap=5")[
            "items"
        ]
        cursor = laps[0]["lap_end_time_ms"] + 1
        inference = compare(
            f"/api/v1/sessions/{replay}/drivers/{driver}/intelligence?time_ms={cursor}"
        )
        assert inference["availability"] == "available", inference
        simulation = compare(
            f"/api/v1/sessions/{replay}/strategy/simulate",
            body={
                "cursor_time_ms": cursor,
                "driver_id": driver,
                "strategy": {"strategy_id": "stay", "label": "Stay", "actions": []},
                "scenario": {},
                "simulation_count": 10,
                "seed": 2216,
            },
        )
        assert simulation["status"] == "available", simulation
        # Choose eligible observed samples locally before the expensive whole-catalog
        # check, so a bad sample assumption fails before repeating that check.
        native = local_app.state.telemetry_service.reader.sessions()
        assert len(native) == 42
        for category in ("practice", "qualifying", "sprint", "race"):
            row = next(
                row
                for row in native
                if (row["session_type"] or "").casefold().startswith(category)
                and (category != "sprint" or row["session_type"].casefold() == "sprint")
            )
            identity, revision = row["session_id"], row["revision"]
            base = f"/api/v1/telemetry/sessions/{identity}"

            def trace_path(
                driver: str,
                number: int,
                *,
                session_base: str = base,
                pinned_revision: str = revision,
            ) -> str:
                return (
                    session_base + f"/trace?driver={driver}&lap={number}"
                    f"&revision={pinned_revision}"
                    "&channels=speed_kmh,gear,drs_raw"
                )

            def supported(driver: str, number: int) -> bool:
                path = trace_path(driver, number)
                response = local.get(path)
                assert response.status_code in {200, 409}, (path, response.text)
                if response.status_code == 409:
                    # Unsupported alignment is an honest data outcome, not a reason
                    # to change observations or weaken the production trace rules.
                    compare(path, expected_status=409)
                    print("NATIVE_REJECTED", path, flush=True)
                    return False
                return True

            driver_number, lap = _representative_lap(
                local_app.state.telemetry_service.reader,
                identity,
                revision,
                row["drivers"],
                supports=supported,
            )
            compare(base + f"/laps?driver={driver_number}&revision={revision}")
            trace = compare(trace_path(driver_number, lap["lap_number"]))
            assert trace["revision"] == revision
            print("NATIVE", category, identity, revision, flush=True)
        assert len(compare("/api/v1/telemetry/sessions?limit=100")["items"]) == 42
        # Storage accounting intentionally reports product release, not private raw evidence.
        local_storage = local.get("/api/v1/catalog/storage").json()
        blob_storage = blob.get("/api/v1/catalog/storage").json()
        print(
            "OPERATIONAL_STORAGE",
            json.dumps({"local": local_storage, "blob": blob_storage}, sort_keys=True),
            flush=True,
        )
    assert before == {path: (file_sha256(path), path.stat().st_mtime_ns) for path in protected}
    assert source_before == {
        path: (file_sha256(source), source.stat().st_mtime_ns)
        for path, source in release.source_files.items()
    }
    assert hashlib.sha256(release.body).hexdigest() == release.sha256
    print(
        "RELEASE_PROOF",
        json.dumps(
            {
                "sha256": release.sha256,
                "files": len(release.manifest.files),
                "index_bytes": len(release.body),
                "payload_bytes": sum(entry.size_bytes for entry in release.manifest.files.values()),
                "max_object_bytes": max(
                    entry.size_bytes for entry in release.manifest.files.values()
                ),
                "cache_disk_peak_bytes": peak,
                "native_sessions": 42,
                "legacy_datasets": 13,
                "aliases": 13,
                "archive_sessions": 515,
                "analytics_rows": 10814,
                "circuit_identities": 1172,
                "checked_routes": len(checked),
                "ignored_intrinsic_timing_fields": sorted(intrinsic_timing_fields),
                "original_hashes_and_mtimes_unchanged": True,
                "native_full_corpus_denominator": 943,
            },
            sort_keys=True,
        ),
        flush=True,
    )
