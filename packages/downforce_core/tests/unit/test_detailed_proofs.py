import copy
import json
import zipfile
from pathlib import Path
from typing import Any

import fastf1
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from downforce_core.detailed.audit import write_sample_table
from downforce_core.detailed.cli import project_study
from downforce_core.detailed.measurement import measure_candidate
from downforce_core.detailed.openf1 import measure_enrichment
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.proofs import validated_study_measurements
from downforce_core.detailed.raw_evidence import RawEvidenceStore
from downforce_core.detailed.study_io import project_measured_study
from downforce_core.detailed.study_runner import ready_inventory
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.parquet import file_sha256
from test_detailed_independent_finalization import bundle as bundle
from test_detailed_independent_integration import finalize_ready, proof_session
from test_detailed_independent_integration import integration as integration
from test_detailed_study_runner import run


@pytest.fixture(autouse=True)
def no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fastf1.Cache,
        "requests_get",
        lambda *_args, **_kwargs: pytest.fail("synthetic projection tests forbid HTTP"),
    )


def save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def study(
    tmp_path: Path,
    *,
    modern: bool = False,
    stored_archive: bool = False,
    native_damage: str | None = None,
) -> tuple[DetailedPaths, dict[str, Any]]:
    root = tmp_path / "detailed"
    identity = "2024-01-race" if modern else "2018-01-practice-1"
    session = {
        "session_id": identity,
        "season": 2024 if modern else 2018,
        "session_type": "race" if modern else "practice-1",
        "event_name": "Test Grand Prix",
        "round_number": 1,
        "fastf1_slot": 5 if modern else 1,
        "scheduled_at": "2024-01-01T00:00:00+00:00" if modern else "2018-01-01T00:00:00+00:00",
        "completion": "past-unverified",
        "fastf1_supported": True,
    }
    save(root / "inventory.json", {"complete_inventory": True, "sessions": [session]})
    provider = {
        "session_key": 123,
        "date_start": "2024-01-01T00:00:00+00:00",
        "date_end": "2024-01-01T02:00:00+00:00",
        "year": 2024,
        "session_name": "Race",
        "session_type": "Race",
    }
    save(root / "openf1-inventory.json", {"sessions": [provider] if modern else [], "failures": []})
    save(
        root / "completion" / f"{identity}.json",
        {
            "session_id": identity,
            "state": "completed",
            "statuses": ["Started", "Finalised"],
            "times": ["0:00:01", "0:00:02"],
            "timing_path": f"{session['season']}/test/session/",
            "error": None,
        },
    )
    row = {
        **session,
        "state": "completed",
        "statuses": ["Started", "Finalised"],
        "classification_version": "formal-completion-v2",
        "openf1_session_key": 123 if modern else None,
    }
    save(
        root / "reconciled-inventory.json",
        {
            "complete": True,
            "classification_version": "formal-completion-v2",
            "sessions": [row],
            "provider_entries": [
                {**provider, "canonical_session_id": identity, "reconciliation_state": "completed"}
            ]
            if modern
            else [],
            "unmatched_unverified": [],
            "discovery_sha256": file_sha256(root / "inventory.json"),
            "completion_evidence_sha256": {
                identity: file_sha256(root / "completion" / f"{identity}.json")
            },
            "openf1_inventory_sha256": file_sha256(root / "openf1-inventory.json"),
        },
    )
    save(
        root / "study" / "sample-plan.json",
        {
            "schema_version": "detailed-sample-plan-v1",
            "selected_before_download": True,
            "inventory_sha256": file_sha256(root / "reconciled-inventory.json"),
            "selected_sessions": [row],
        },
    )
    attempt = root / "preflight" / identity / "attempt"
    cache = attempt / "raw-cache"
    cache.mkdir(parents=True)
    (cache / "evidence.bin").write_bytes(b"original provider evidence" * 100)
    frames = {
        "laps": pd.DataFrame({"Speed": [100.0, None]}),
        "results": pd.DataFrame({"DriverNumber": ["44"]}),
        "session_status": pd.DataFrame(
            {"Status": ["Started", "Finalised"], "Time": pd.to_timedelta([1, 2], unit="s")}
        ),
        "track_status": pd.DataFrame({"Status": ["1"]}),
        "car_data/44": pd.DataFrame({"Speed": [100.0]}),
    }
    if native_damage == "completion":
        frames["session_status"]["Status"] = ["Started", "Aborted"]
    elif native_damage in frames:
        frames.pop(native_damage)
    elif native_damage == "car_data":
        frames["car_data/44"] = frames["car_data/44"].iloc[:0]
    source = {
        "session": session,
        "tables": {
            name: write_sample_table(frame, attempt / f"{name}.parquet")
            for name, frame in frames.items()
        },
        "fastf1_version": "test",
        "completion_confirmed": True,
        "session_statuses": frames.get("session_status", pd.DataFrame())
        .get("Status", pd.Series(dtype=str))
        .tolist(),
        "failures": [],
    }
    save(attempt / "measurement.json", source)
    save(attempt.parent / "measurement.json", source)
    return candidate_fixture(root, source, attempt, stored_archive=stored_archive)


def candidate_fixture(
    root: Path, source: dict[str, Any], attempt: Path, *, stored_archive: bool = False
) -> tuple[DetailedPaths, dict[str, Any]]:
    """Publish only synthetic lossless candidate/compression artifacts for a source."""
    identity = source["session"]["session_id"]
    cache = attempt / "raw-cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "evidence.bin").write_bytes(b"original provider evidence" * 100)
    raw = RawEvidenceStore(root / "study" / "raw-bundles")
    bundle = raw.pack(
        cache, session_id=identity, provider="fastf1", provider_version=source["fastf1_version"]
    )
    if stored_archive:
        replacement = raw.root / "uncompressed.zip"
        with (
            zipfile.ZipFile(raw.root / f"{bundle}.zip") as original,
            zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_STORED) as uncompressed,
        ):
            for member in original.infolist():
                uncompressed.writestr(member.filename, original.read(member))
        bundle = file_sha256(replacement)
        replacement.rename(raw.root / f"{bundle}.zip")
    expanded_bytes = sum(path.stat().st_size for path in cache.rglob("*") if path.is_file())
    archive_bytes = (raw.root / f"{bundle}.zip").stat().st_size
    compression = {
        "session_id": identity,
        "round_trip_exact": True,
        "bundle_sha256": bundle,
        "observed_peak_temporary_bytes": expanded_bytes + archive_bytes,
        "codec": "ZIP-DEFLATE",
        "level": 6,
        "original_bytes": expanded_bytes,
        "compressed_bytes": archive_bytes,
        "ratio": archive_bytes / expanded_bytes,
        "peak_temporary_bytes_upper_bound": expanded_bytes + archive_bytes,
        "sampling_interval_ms": 20,
        "tool_version": "detailed-storage-study-v2",
        "pack_seconds_including_validation": 0.01,
        "restore_seconds_including_validation": 0.01,
    }
    save(root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json", compression)
    measured = measure_candidate(root.parent, root, source, compression)
    return DetailedPaths(root.parent, override=root), measured


@pytest.mark.parametrize(
    "damage", ["completion", "laps", "results", "session_status", "track_status", "car_data"]
)
def test_projection_reuses_native_completion_core_and_car_validation(
    tmp_path: Path, damage: str
) -> None:
    paths, _ = study(tmp_path, native_damage=damage)
    assert ready_inventory(paths.root)[0]["complete"]
    with pytest.raises(ValueError, match="native|completion|required"):
        project_study(paths)
    assert not (paths.root / "stratified-projection.json").exists()


@pytest.mark.parametrize("tables", [[1], {"laps": [1]}, {"laps": None}])
@pytest.mark.parametrize("gate", ["proofs", "public", "cli"])
def test_projection_rejects_malformed_native_table_objects(
    tmp_path: Path, tables: Any, gate: str
) -> None:
    paths, measured = study(tmp_path)
    samples = validated_study_measurements(paths)
    directory = paths.root / "preflight" / measured["session_id"]
    primary_path = directory / "measurement.json"
    primary = json.loads(primary_path.read_text())
    primary["tables"] = tables
    save(primary_path, primary)
    save(directory / "attempt" / "measurement.json", primary)
    assert ready_inventory(paths.root)[0]["complete"]
    with pytest.raises(ValueError, match="authoritative source table.*object"):
        if gate == "proofs":
            validated_study_measurements(paths)
        elif gate == "public":
            project_measured_study(paths.root, samples, free_bytes=10**12)
        else:
            project_study(paths)
    assert not (paths.root / "stratified-projection.json").exists()


@pytest.fixture
def independent_study(integration):
    root, entry, proof = integration
    finalize_ready(integration)
    assert run(root, lambda *_: proof_session(proof))["new_writes"] == 1
    directory = root / "preflight" / entry["session_id"]
    source = json.loads((directory / "measurement.json").read_text())
    attempt = next(directory.glob("*/measurement.json")).parent
    paths, _ = candidate_fixture(root, source, attempt)
    return paths, proof


def test_projection_preserves_independent_finality_semantics(independent_study) -> None:
    paths, proof = independent_study
    result = project_study(paths)
    assert result["safe_to_sync"]
    assert result["measurement_count"] == 1
    assert ready_inventory(paths.root)[0]["sessions"][0]["chronology_valid"] is False
    assert proof["statuses"][-1] == "Started"


@pytest.mark.parametrize("gate", ["proofs", "public", "cli"])
@pytest.mark.parametrize("dependency", ["original", "derived", "independent", "capture"])
def test_projection_replays_independent_and_derived_dependencies(
    independent_study, dependency: str, gate: str
) -> None:
    paths, proof = independent_study
    samples = validated_study_measurements(paths)
    relative = {
        "original": f"completion/{proof['session_id']}.json",
        "derived": f"completion-derived/{proof['session_id']}.json",
        "independent": f"completion-finalization/{proof['session_id']}.json",
        "capture": "evidence-requests/fia/capture.json",
    }[dependency]
    path = paths.root / relative
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        if gate == "proofs":
            validated_study_measurements(paths)
        elif gate == "public":
            project_measured_study(paths.root, samples, free_bytes=10**12)
        else:
            project_study(paths)
    assert not (paths.root / "stratified-projection.json").exists()


def test_project_checks_real_artifacts(tmp_path: Path) -> None:
    paths, measured = study(tmp_path)
    result = project_study(paths)
    assert result["measurement_count"] == 1
    assert result["measurements"][0]["canonical_bytes"] == measured["canonical_bytes"]


@pytest.mark.parametrize("gate", ["proofs", "public", "cli"])
@pytest.mark.parametrize(
    "damage",
    [
        "same-byte-completion",
        "missing-discovery",
        "missing-source-map",
        "missing-completion",
        "stale-provider",
        "identity-conflict",
        "past-future-session",
        "past-future-provider",
        "missing-index-proof",
    ],
)
def test_projection_replays_readiness_before_output(tmp_path: Path, damage: str, gate: str) -> None:
    paths, _ = study(tmp_path)
    root = paths.root
    samples = validated_study_measurements(paths)
    assert project_measured_study(root, samples, free_bytes=10**12)["safe_to_sync"]
    catalog_path = root / "reconciled-inventory.json"
    catalog = json.loads(catalog_path.read_text())
    identity = catalog["sessions"][0]["session_id"]
    completion = root / "completion" / f"{identity}.json"
    if damage == "same-byte-completion":
        original = completion.read_bytes()
        changed = original.replace(b"Finalised", b"Xinalised")
        assert changed != original and len(changed) == len(original)
        completion.write_bytes(changed)
    elif damage == "missing-discovery":
        (root / "inventory.json").unlink()
    elif damage == "missing-source-map":
        catalog.pop("completion_evidence_sha256")
    elif damage == "missing-completion":
        completion.unlink()
    elif damage == "stale-provider":
        provider = root / "openf1-inventory.json"
        provider.write_text(provider.read_text() + " ")
    elif damage == "identity-conflict":
        catalog["sessions"][0]["round_number"] = 2
    elif damage == "past-future-session":
        discovery = json.loads((root / "inventory.json").read_text())
        future = {**discovery["sessions"][0], "session_id": "2018-02-practice-1"}
        discovery["sessions"].append(future)
        save(root / "inventory.json", discovery)
        future_source = {"session_id": future["session_id"], "state": "future"}
        future_path = root / "completion" / f"{future['session_id']}.json"
        save(future_path, future_source)
        catalog["sessions"].append({**future, "state": "future"})
        catalog["discovery_sha256"] = file_sha256(root / "inventory.json")
        catalog["completion_evidence_sha256"][future["session_id"]] = file_sha256(future_path)
    elif damage == "past-future-provider":
        catalog["provider_entries"].append(
            {
                "session_key": 999,
                "canonical_session_id": None,
                "reconciliation_state": "future",
                "date_start": "2018-01-01T00:00:00+00:00",
            }
        )
    else:
        catalog["index_failures"] = [{"year": 2018, "error": "unavailable"}]
    save(catalog_path, catalog)
    plan_path = root / "study" / "sample-plan.json"
    plan = json.loads(plan_path.read_text())
    plan["inventory_sha256"] = file_sha256(catalog_path)
    plan["selected_sessions"] = [catalog["sessions"][0]]
    save(plan_path, plan)
    with pytest.raises((ValueError, OSError)):
        ready_inventory(root)
    with pytest.raises((ValueError, StorageIntegrityError)):
        if gate == "proofs":
            validated_study_measurements(paths)
        elif gate == "public":
            project_measured_study(root, samples, free_bytes=10**12)
        else:
            project_study(paths)
    assert not (root / "stratified-projection.json").exists()


@pytest.mark.parametrize(
    "damage", ["zero-sizes", "lossless-flag", "duplicate", "omitted", "nested-bool", "nested-float"]
)
def test_public_projection_requires_actual_typed_measurements(tmp_path: Path, damage: str) -> None:
    paths, measured = study(tmp_path)
    if damage.startswith("nested"):
        measured["notes"] = {"flags": [{"count": 1, "exact": True}]}
        save(
            paths.root / "study" / "session-measurements" / f"{measured['session_id']}.json",
            measured,
        )
    samples = validated_study_measurements(paths)
    assert ready_inventory(paths.root)[0]["complete"]
    if damage == "zero-sizes":
        for key in samples[0]:
            if key.endswith("_bytes"):
                samples[0][key] = 0
    elif damage == "lossless-flag":
        samples[0]["round_trip_exact"] = 1
    elif damage == "duplicate":
        samples.append(copy.deepcopy(samples[0]))
    elif damage == "nested-bool":
        samples[0]["notes"]["flags"][0]["exact"] = 1
    elif damage == "nested-float":
        samples[0]["notes"]["flags"][0]["count"] = 1.0
    else:
        samples.clear()
    with pytest.raises(ValueError):
        project_measured_study(paths.root, samples, free_bytes=10**12)
    assert not (paths.root / "stratified-projection.json").exists()


@pytest.mark.parametrize("gate", ["public", "cli"])
@pytest.mark.parametrize("damage", ["stale-binding", "changed-selection", "changed-row"])
def test_final_projection_gate_rechecks_frozen_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, gate: str
) -> None:
    from downforce_core.detailed import proofs

    paths, _ = study(tmp_path)
    samples = validated_study_measurements(paths)
    validate = proofs.validated_study_measurements

    def change_after_validation(current_paths: DetailedPaths) -> list[dict[str, Any]]:
        actual = validate(current_paths)
        path = paths.root / "study" / "sample-plan.json"
        plan = json.loads(path.read_text())
        if damage == "stale-binding":
            plan["inventory_sha256"] = "stale"
        elif damage == "changed-selection":
            plan["selected_sessions"] = []
        else:
            plan["selected_sessions"][0]["round_number"] = 2
        save(path, plan)
        return actual

    monkeypatch.setattr(proofs, "validated_study_measurements", change_after_validation)
    with pytest.raises(ValueError, match="plan|selection"):
        if gate == "public":
            project_measured_study(paths.root, samples, free_bytes=10**12)
        else:
            project_study(paths)
    assert not (paths.root / "stratified-projection.json").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-plan",
        "stale-plan",
        "extra-measurement",
        "missing-measurement",
        "raw-corrupt",
        "canonical-corrupt",
        "pointer-stale",
        "source-corrupt",
        "raw-size",
        "canonical-size",
        "metadata-size",
        "raw-count",
        "persistent-count",
        "missing-compression",
    ],
)
def test_project_fails_closed_on_unproven_measurements(tmp_path: Path, damage: str) -> None:
    paths, measured = study(tmp_path)
    root, identity = paths.root, measured["session_id"]
    measurement_path = root / "study" / "session-measurements" / f"{identity}.json"
    revision = root / "study" / "candidates" / "canonical" / identity / "revisions"
    if damage == "missing-plan":
        (root / "study" / "sample-plan.json").unlink()
    elif damage == "stale-plan":
        (root / "reconciled-inventory.json").write_text(
            (root / "reconciled-inventory.json").read_text() + "\n"
        )
    elif damage == "extra-measurement":
        save(measurement_path.with_name("extra.json"), measured)
    elif damage == "missing-measurement":
        measurement_path.unlink()
    elif damage == "raw-corrupt":
        (root / "study" / "raw-bundles" / f"{measured['raw_bundle_sha256']}.zip").write_bytes(
            b"bad"
        )
    elif damage == "canonical-corrupt":
        (revision / measured["candidate_revision"] / "laps.parquet").write_bytes(b"bad")
    elif damage == "pointer-stale":
        save(revision.parent / "active.json", {"revision": "0" * 64})
    elif damage == "source-corrupt":
        (root / "preflight" / identity / "attempt" / "laps.parquet").write_bytes(b"bad")
    elif damage == "missing-compression":
        (root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json").unlink()
    else:
        key = {
            "raw-size": "raw_evidence_bytes",
            "canonical-size": "canonical_bytes",
            "metadata-size": "metadata_bytes",
            "raw-count": "raw_file_count",
            "persistent-count": "persistent_file_count",
        }[damage]
        measured[key] += 1
        save(measurement_path, measured)
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)
    assert not (root / "stratified-projection.json").exists()


class OfflineClient:
    def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
        rows = [
            {"session_key": session_key, "interval": "1L", "native_extra": {"nested": [1, 2]}},
            {"session_key": session_key, "interval": None, "later_field": "retained"},
        ]
        return json.dumps(
            rows
        ).encode(), f"https://api.openf1.org/v1/{endpoint}?session_key={session_key}"


def enrichment(paths: DetailedPaths) -> dict[str, Any]:
    session = json.loads((paths.root / "reconciled-inventory.json").read_text())["sessions"][0]
    return measure_enrichment(paths.root, session, OfflineClient())  # type: ignore[arg-type]


def test_project_joins_independent_enrichment_and_counts_its_manifest(tmp_path: Path) -> None:
    paths, measured = study(tmp_path, modern=True)
    report = enrichment(paths)
    result = project_study(paths)
    actual = result["measurements"][0]
    directory = paths.root / "study" / "openf1" / measured["session_id"]
    assert actual["openf1_measured"] is True
    assert (
        actual["openf1_raw_bytes"]
        == report["openf1_raw_bytes"] + (directory / "measurement.json").stat().st_size
    )
    assert actual["openf1_canonical_bytes"] == report["openf1_canonical_bytes"]


@pytest.mark.parametrize(
    "damage", ["missing", "wire", "native", "provider-key", "size", "source-proof"]
)
def test_project_rejects_forged_enrichment_flags(tmp_path: Path, damage: str) -> None:
    paths, measured = study(tmp_path, modern=True)
    measured.update(openf1_measured=True, openf1_raw_bytes=1, openf1_canonical_bytes=1)
    save(paths.root / "study" / "session-measurements" / f"{measured['session_id']}.json", measured)
    if damage != "missing":
        report = enrichment(paths)
        directory = paths.root / "study" / "openf1" / measured["session_id"]
        report = copy.deepcopy(report)
        record = report["endpoints"]["drivers"]
        if damage == "wire":
            (directory / record["raw_file"]).write_bytes(b"bad")
        elif damage == "native":
            canonical = directory / record["canonical_file"]
            pq.write_table(pa.table({"invented": [1]}), canonical)
            record["canonical_file_sha256"] = file_sha256(canonical)
            record["canonical_bytes"] = canonical.stat().st_size
            save(directory / "drivers.json", record)
        elif damage == "provider-key":
            report["session_key"] = 999
        elif damage == "size":
            report["openf1_canonical_bytes"] += 1
        elif damage == "source-proof":
            (directory / "drivers-source.json").unlink()
        save(directory / "measurement.json", report)
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)


def test_project_handles_truncated_wire_even_with_matching_stored_checksums(tmp_path: Path) -> None:
    paths, measured = study(tmp_path, modern=True)
    report = enrichment(paths)
    directory = paths.root / "study" / "openf1" / measured["session_id"]
    record = report["endpoints"]["drivers"]
    raw = directory / record["raw_file"]
    raw.write_bytes(raw.read_bytes()[:-8])
    record["raw_file_sha256"] = file_sha256(raw)
    record["raw_bytes"] = raw.stat().st_size
    source = json.loads((directory / "drivers-source.json").read_text())
    source["raw_file_sha256"] = record["raw_file_sha256"]
    save(directory / "drivers-source.json", source)
    save(directory / "drivers.json", record)
    save(directory / "measurement.json", report)
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)


def test_missing_candidate_proof_does_not_create_an_empty_store(tmp_path: Path) -> None:
    paths, _ = study(tmp_path)
    canonical = paths.root / "study" / "candidates" / "canonical"
    canonical.rename(canonical.with_name("saved-canonical"))
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)
    assert not canonical.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_bytes", 0),
        ("compressed_bytes", 0),
        ("codec", "invented"),
        ("level", 9),
        ("ratio", 0.0),
        ("peak_temporary_bytes_upper_bound", 0),
        ("sampling_interval_ms", 0),
        ("tool_version", "invented"),
        ("pack_seconds_including_validation", float("nan")),
        ("restore_seconds_including_validation", -1.0),
    ],
)
def test_project_checks_actual_compression_proof(tmp_path: Path, field: str, value: Any) -> None:
    paths, measured = study(tmp_path)
    identity = measured["session_id"]
    path = paths.root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json"
    compression = json.loads(path.read_text())
    compression[field] = value
    save(path, compression)
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)


def test_zeroed_paired_observed_peaks_cannot_reduce_working_peak(tmp_path: Path) -> None:
    paths, measured = study(tmp_path)
    identity = measured["session_id"]
    path = paths.root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json"
    compression = json.loads(path.read_text())
    upper = compression["peak_temporary_bytes_upper_bound"]
    compression["observed_peak_temporary_bytes"] = 0
    measured["observed_compression_temporary_peak_bytes"] = 0
    measured["temporary_peak_bytes"] = 2 * measured["canonical_bytes"]
    save(path, compression)
    save(paths.root / "study" / "session-measurements" / f"{identity}.json", measured)
    actual = project_study(paths)["measurements"][0]
    assert actual["historical_temporary_peak_bytes"] == upper + 2 * measured["canonical_bytes"]
    assert actual["temporary_peak_bytes"] == max(
        actual["historical_temporary_peak_bytes"], actual["conservative_operation_peak_bytes"]
    )


def test_project_includes_retained_proof_metadata_in_effective_totals(tmp_path: Path) -> None:
    paths, measured = study(tmp_path)
    identity = measured["session_id"]
    report = paths.root / "study" / "session-measurements" / f"{identity}.json"
    compression = paths.root / "study" / "compression" / f"{identity}-deflate-6-metrics-v2.json"
    primary = paths.root / "preflight" / identity / "measurement.json"
    proof_bytes = report.stat().st_size + compression.stat().st_size + primary.stat().st_size
    actual = project_study(paths)["measurements"][0]
    assert actual["metadata_bytes"] == measured["metadata_bytes"] + proof_bytes
    assert actual["reported_metadata_bytes"] == measured["metadata_bytes"]
    assert actual["study_proof_metadata_bytes"] == proof_bytes
    assert actual["persistent_file_count"] == 13
    assert actual["reported_persistent_file_count"] == measured["persistent_file_count"]


@pytest.mark.parametrize("filename", ["reconciled-inventory.json", "inventory.json"])
def test_project_rejects_linked_inventory_files_before_reading(
    tmp_path: Path, filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = study(tmp_path)
    inventory = paths.root / filename
    if filename == "inventory.json":
        save(inventory, {"complete_inventory": True})
    target = tmp_path / f"linked-{filename}"
    inventory.rename(target)
    try:
        inventory.symlink_to(target)
    except OSError:
        # Windows hosts without SeCreateSymbolicLinkPrivilege cannot create the
        # link. Exercise the same path guard using its actual OS detection seam.
        target.rename(inventory)
        original_detection = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path == inventory or original_detection(path)
        )
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)
    assert not (paths.root / "stratified-projection.json").exists()


def test_project_checks_real_zip_codec_not_just_report_label(tmp_path: Path) -> None:
    paths, _ = study(tmp_path, stored_archive=True)
    with pytest.raises((ValueError, StorageIntegrityError)):
        project_study(paths)
