"""Read-only data-corpus assessment uses artifacts, not saved completion flags."""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from downforce_core.cli.corpus import _inspect
from downforce_core.detailed import corpus_openf1, study_runner
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.parquet import file_sha256

fixtures = importlib.import_module("test_corpus_openf1")


def save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def native_jobs(project: Path, paths: DetailedPaths) -> None:
    store = DetailedStore(project, detailed_root=paths.root)
    catalog = json.loads((paths.root / "reconciled-inventory.json").read_text())
    for entry in catalog["sessions"]:
        manifest = store.manifest(entry["session_id"])
        metadata = manifest["metadata"]
        metadata["quality"] = "partial"
        tables = {name: store.read_table(entry["session_id"], name) for name in manifest["tables"]}
        revision = store.publish(entry["session_id"], tables, metadata)
        save(
            paths.root / f"corpus/jobs/{entry['session_id']}.json",
            {
                "session_id": entry["session_id"],
                "state": "COMPLETE",
                "checkpoint": "COMPLETE",
                "canonical_identity": revision,
                "raw_identity": metadata["raw_bundle_sha256"],
            },
        )


@pytest.mark.parametrize("validate", [False, True])
def test_empty_assessment_is_read_only_and_never_complete(tmp_path: Path, validate: bool) -> None:
    paths = DetailedPaths(tmp_path)
    result = _inspect(tmp_path, paths, validate=validate)
    assert result["completion_scope"] == "data-corpus-only"
    assert result["expected_completed"] == result["missing_jobs"] == 0
    assert result["corpus_complete"] is False
    assert result["enrichment_complete"] is False
    assert not (tmp_path / ".downforce").exists()


def test_failed_jobs_are_not_unattempted_and_status_does_not_prove_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = DetailedPaths(tmp_path)
    entries = [
        {"session_id": f"2024-{number:04d}-race", "state": "completed"} for number in range(943)
    ]
    save(paths.root / "reconciled-inventory.json", {"sessions": entries})
    for entry in entries[:42]:
        save(paths.root / f"corpus/jobs/{entry['session_id']}.json", {"state": "COMPLETE"})
    failed = paths.root / f"corpus/jobs/{entries[42]['session_id']}.json"
    save(failed, {"state": "FAILED", "error": "PermissionError: HTTP 403"})
    before = failed.read_bytes()
    monkeypatch.setattr(
        study_runner,
        "ready_inventory",
        lambda _root: pytest.fail("cheap status cannot replay proof"),
    )
    result = _inspect(tmp_path, paths, validate=False)
    assert result["expected_completed"] == 943
    assert result["complete_jobs"] == 42
    assert result["failed_jobs"] == 1
    assert result["missing_jobs"] == result["unattempted_jobs"] == 900
    assert result["published_sessions"] == 0
    assert result["authority"]["verified"] is False
    assert result["corpus_complete"] is result["verified"] is False
    assert result["jobs"][42]["error"] == "PermissionError: HTTP 403"
    assert failed.read_bytes() == before


def test_native_artifacts_survive_stale_source_authority(tmp_path: Path) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    (paths.root / "inventory.json").write_text("{}")
    result = _inspect(tmp_path, paths, validate=True)
    assert result["native_corpus_complete"] is True
    assert result["authority"]["verified"] is False
    assert result["corpus_complete"] is result["complete"] is False


@pytest.mark.parametrize("broken", ["malformed", "duplicate"])
def test_bad_inventory_is_reported_without_false_completion(tmp_path: Path, broken: str) -> None:
    paths = DetailedPaths(tmp_path)
    target = paths.root / "reconciled-inventory.json"
    save(target, {"sessions": [{"session_id": "2024-01-race", "state": "completed"}] * 2})
    if broken == "malformed":
        target.write_text("{")
    result = _inspect(tmp_path, paths, validate=True)
    assert result["authority"]["failures"]
    assert result["corpus_complete"] is False


def test_verified_native_and_real_openf1_artifacts_complete_data_corpus_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    calls, _ = fixtures.transport(monkeypatch, [])
    corpus_openf1.run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    before = {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}
    result = _inspect(tmp_path, paths, validate=True)
    assert result["native_corpus_complete"] is True
    assert result["authority"]["verified"] is True
    assert result["openf1_assessment"]["target_sessions"] == 1
    assert result["openf1_assessment"]["completed_sessions"] == 1
    assert result["enrichment_complete"] is True
    assert result["corpus_complete"] is result["complete"] is True
    assert result["completion_scope"] == "data-corpus-only"
    assert len(calls) == 12
    assert before == {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}
    cheap = _inspect(tmp_path, paths, validate=False)
    assert cheap["complete"] is cheap["enrichment_complete"] is False


@pytest.mark.parametrize("broken", ["missing", "partial", "raw", "binding", "manifest"])
def test_enrichment_must_match_current_native_and_original_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    if broken != "missing":
        replies = [fixtures.Reply(500, b'"failed"')] if broken == "partial" else []
        calls, _ = fixtures.transport(monkeypatch, replies)
        corpus_openf1.run_openf1_corpus(tmp_path, detailed_root=paths.root)
        leaf = paths.root / "openf1/2024-01-race"
        if broken == "raw":
            record = json.loads((leaf / "drivers.json").read_text())
            raw = leaf / record["raw_file"]
            raw.write_bytes(raw.read_bytes() + b"tamper")
        elif broken in {"binding", "manifest"}:
            target = leaf / ("binding.json" if broken == "binding" else "manifest.json")
            value = json.loads(target.read_text())
            value["native_revision" if broken == "binding" else "state"] = "changed"
            save(target, value)
        assert len(calls) == 12
    result = _inspect(tmp_path, paths, validate=True)
    assert result["native_corpus_complete"] is True
    assert result["enrichment_complete"] is result["corpus_complete"] is False
    enrichment = result["openf1_assessment"]
    assert enrichment["completed_sessions"] == 0
    assert enrichment["missing_sessions" if broken == "missing" else "partial_sessions"] == (
        1 if broken in {"missing", "partial"} else 0
    )
    if broken not in {"missing", "partial"}:
        assert enrichment["failures"]


def test_no_completed_targets_or_missing_mapping_authority_cannot_complete(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path)
    save(paths.root / "reconciled-inventory.json", {"sessions": []})
    result = _inspect(tmp_path, paths, validate=True)
    assert result["openf1_assessment"]["target_sessions"] == 0
    assert result["authority"]["verified"] is False
    assert result["enrichment_complete"] is result["corpus_complete"] is False


@pytest.mark.parametrize("broken", ["raw", "canonical", "job", "checkpoint"])
def test_native_corruption_is_not_a_valid_publication(tmp_path: Path, broken: str) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    manifest = DetailedStore(tmp_path, detailed_root=paths.root).manifest("2024-01-race")
    if broken == "raw":
        target = paths.raw / f"{manifest['metadata']['raw_bundle_sha256']}.zip"
        target.write_bytes(b"corrupt")
    elif broken == "canonical":
        target = paths.canonical / f"2024-01-race/revisions/{manifest['revision']}/laps.parquet"
        target.write_bytes(b"corrupt")
    else:
        target = paths.root / "corpus/jobs/2024-01-race.json"
        if broken == "job":
            target.write_text("{")
        else:
            value = json.loads(target.read_text())
            value["checkpoint"] = "invalid"
            save(target, value)
    result = _inspect(tmp_path, paths, validate=True)
    assert result["native_corpus_complete"] is result["verified"] is False
    assert result["published_sessions"] == 0
    assert result["failures"]
    assert result["invalid_jobs"] == int(broken in {"job", "checkpoint"})


def test_provider_stop_is_reported_and_never_retried_or_reclassified_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    calls, _ = fixtures.transport(monkeypatch, [fixtures.Reply(403, b'"denied"')])
    with pytest.raises(PermissionError):
        corpus_openf1.run_openf1_corpus(tmp_path, detailed_root=paths.root)
    before = {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}
    for validate in (False, True):
        result = _inspect(tmp_path, paths, validate=validate)
        assert result["openf1_assessment"]["provider_stop"]["session_id"] == "2024-01-race"
        assert "403" in result["openf1_assessment"]["provider_stop"]["error"]
        assert result["enrichment_complete"] is False
        assert result["openf1"] != "unavailable"
    assert len(calls) == 1
    assert before == {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}


def test_enrichment_target_requires_source_mapping_even_when_public_proof_is_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    catalog = json.loads((paths.root / "reconciled-inventory.json").read_text())
    catalog.pop("provider_entries")
    save(paths.root / "reconciled-inventory.json", catalog)
    monkeypatch.setattr(study_runner, "ready_inventory", lambda _root: (catalog, {}))
    result = _inspect(tmp_path, paths, validate=True)
    assert result["openf1_assessment"]["target_sessions"] == 0
    assert result["openf1_assessment"]["verified"] is False
    assert result["enrichment_complete"] is result["corpus_complete"] is False


def test_data_completion_does_not_require_new_ingestion_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    fixtures.transport(monkeypatch, [])
    corpus_openf1.run_openf1_corpus(tmp_path, detailed_root=paths.root)
    gate = importlib.import_module("downforce_core.detailed.corpus_gate")
    monkeypatch.setattr(gate, "guard_operation", lambda *_a, **_k: pytest.fail("read-only"))
    monkeypatch.setattr(gate, "storage_status", lambda *_a: pytest.fail("read-only"))
    assert _inspect(tmp_path, paths, validate=True)["corpus_complete"] is True


def test_native_metadata_must_match_current_inventory_for_verified_authority(
    tmp_path: Path,
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    store = DetailedStore(tmp_path, detailed_root=paths.root)
    identity = "2024-01-race"
    manifest = store.manifest(identity)
    metadata = manifest["metadata"]
    metadata["session"]["event_name"] = "different event"
    revision = store.publish(
        identity, {name: store.read_table(identity, name) for name in manifest["tables"]}, metadata
    )
    job = paths.root / f"corpus/jobs/{identity}.json"
    value = json.loads(job.read_text())
    value["canonical_identity"] = revision
    save(job, value)
    result = _inspect(tmp_path, paths, validate=True)
    assert result["native_corpus_complete"] is True  # Backward-compatible artifact-set check.
    assert result["authority"]["verified"] is False
    assert result["corpus_complete"] is False


@pytest.mark.parametrize("key", [None, True, 0, "123"])
def test_completed_modern_entry_cannot_silently_shrink_enrichment_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: Any
) -> None:
    paths = DetailedPaths(tmp_path)
    row = {
        "session_id": "2024-01-race",
        "season": 2024,
        "state": "completed",
        "openf1_session_key": key,
    }
    catalog = {"sessions": [row], "provider_entries": []}
    save(paths.root / "reconciled-inventory.json", catalog)
    save(paths.root / "openf1-inventory.json", {"sessions": []})
    monkeypatch.setattr(study_runner, "ready_inventory", lambda _root: (catalog, {}))
    result = _inspect(tmp_path, paths, validate=True)
    assert result["openf1_assessment"]["verified"] is False
    assert result["openf1_assessment"]["failures"]
    assert result["corpus_complete"] is False


@pytest.mark.parametrize("broken", ["missing", "malformed", "duplicate"])
def test_bad_inventory_retains_independently_valid_publications_and_failed_jobs(
    tmp_path: Path, broken: str
) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    save(
        paths.root / "corpus/jobs/2024-02-race.json",
        {
            "session_id": "2024-02-race",
            "state": "FAILED",
            "checkpoint": "DOWNLOADING",
            "error": "PermissionError: HTTP 403",
        },
    )
    inventory = paths.root / "reconciled-inventory.json"
    if broken == "missing":
        inventory.unlink()
    elif broken == "malformed":
        inventory.write_text("{")
    else:
        value = json.loads(inventory.read_text())
        value["sessions"] *= 2
        save(inventory, value)
    before = {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}
    result = _inspect(tmp_path, paths, validate=True)
    assert result["published_sessions"] == 1
    assert result["sessions"][0]["session_id"] == "2024-01-race"
    assert result["failures"] == []
    assert result["authority"]["verified"] is False
    assert result["native_corpus_complete"] is result["corpus_complete"] is False
    assert result["expected_completed"] == result["complete_jobs"] == result["failed_jobs"] == 0
    jobs = {row["session_id"]: row for row in result["jobs"]}
    assert set(jobs) == {"2024-01-race", "2024-02-race"}
    assert all(row["expected"] is False for row in jobs.values())
    assert jobs["2024-02-race"]["error"] == "PermissionError: HTTP 403"
    assert before == {p: file_sha256(p) for p in paths.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("broken", ["identity", "checkpoint", "state", "raw", "canonical"])
def test_unknown_inventory_does_not_weaken_native_job_checks(tmp_path: Path, broken: str) -> None:
    paths = fixtures.ready(tmp_path)
    native_jobs(tmp_path, paths)
    job = paths.root / "corpus/jobs/2024-01-race.json"
    value = json.loads(job.read_text())
    name = {"identity": "session_id", "raw": "raw_identity", "canonical": "canonical_identity"}.get(
        broken, broken
    )
    value[name] = "wrong"
    save(job, value)
    (paths.root / "reconciled-inventory.json").unlink()
    result = _inspect(tmp_path, paths, validate=True)
    assert result["published_sessions"] == 0
    assert result["failures"]
    assert result["native_corpus_complete"] is result["corpus_complete"] is False
    assert result["expected_completed"] == result["invalid_jobs"] == 0
    assert result["jobs"][0]["expected"] is False


@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("broken", ["regular-file", "permission"])
def test_canonical_enumeration_failure_preserves_job_and_provider_stop_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validate: bool, broken: str
) -> None:
    paths = DetailedPaths(tmp_path)
    stop = {"session_id": "2024-01-race", "error": "PermissionError: HTTP 403"}
    save(
        paths.root / "corpus/jobs/2024-01-race.json",
        {**stop, "state": "FAILED", "checkpoint": "DOWNLOADING"},
    )
    save(paths.root / "runtime/corpus-openf1-progress.json", {"stopped": stop})
    if broken == "regular-file":
        paths.canonical.write_text("invalid directory")
    else:
        paths.canonical.mkdir()
        original = Path.iterdir

        def denied(path: Path) -> Any:
            if path == paths.canonical:
                raise PermissionError("canonical enumeration denied")
            return original(path)

        monkeypatch.setattr(Path, "iterdir", denied)
    result = _inspect(tmp_path, paths, validate=validate)
    assert result["published_sessions"] == 0
    assert result["failures"][0]["session_id"] == "canonical"
    assert result["jobs"][0]["state"] == "FAILED"
    assert result["jobs"][0]["error"] == stop["error"]
    assert result["openf1_assessment"]["provider_stop"] == stop
    assert result["native_corpus_complete"] is result["corpus_complete"] is False
