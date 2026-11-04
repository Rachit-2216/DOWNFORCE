"""Product-only release preparation is deterministic, authentic and read-only."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pyarrow as pa
import pytest
from downforce_core.analytics.engine import AnalyticsEngine
from downforce_core.detailed.storage import DetailedStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactStore
from downforce_core.providers import SessionRef
from downforce_core.runtime_blob.cache import RuntimeBlobCache
from downforce_core.runtime_blob.stores_analytics import BlobCircuitIdentityResolver
from downforce_core.runtime_blob.stores_detailed import BlobDetailedStore
from test_analytics import _fixture_store
from test_runtime_blob_stores_repository import repository_fixture


def fixture(tmp_path: Path):
    repository, _, manifest = repository_fixture(tmp_path)
    source = repository.layout.project_root
    archive = _fixture_store(source)
    raw = archive.raw_root / "jolpica/jolpica-csv-sha256-fixture.zip"
    raw.parent.mkdir(parents=True)
    with ZipFile(raw, "w") as zipped:
        zipped.writestr("formula_one_season.csv", "id,year\n1,2000\n")
        zipped.writestr("formula_one_round.csv", "season_id,circuit_id,number\n1,1,1\n1,2,2\n")
        zipped.writestr("formula_one_circuit.csv", "id,reference\n1,circuit-one\n2,circuit-two\n")
    AnalyticsEngine(archive).rebuild_manifest()
    models = ArtifactStore(source)
    models.publish({"dataset_digest": "inactive"})
    models.publish({"dataset_digest": "frozen"})
    detailed = DetailedStore(source, detailed_root=source / ".downforce/detailed")
    retained = detailed.publish("2024-01-race", {"laps": pa.table({"lap": [1]})}, {})
    detailed.publish("2024-01-race", {"laps": pa.table({"lap": [2]})}, {})
    return source, manifest, detailed, retained


def export(source: Path):
    module = importlib.import_module("downforce_core.runtime_blob.export")
    return module.prepare_runtime_release(source)


def test_export_feature_exists() -> None:
    assert importlib.util.find_spec("downforce_core.runtime_blob.export") is not None


def test_visitor_export_skips_detailed_entirely_but_keeps_original_product(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.export import prepare_runtime_release

    source, _, _, retained = fixture(tmp_path)
    full = prepare_runtime_release(source)
    assert any(retained in path for path in full.manifest.files)
    detailed_paths = {
        path for path in full.manifest.files if path.startswith(".downforce/detailed/")
    }
    before = _file_snapshot(source)
    visitor = prepare_runtime_release(source, include_detailed=False)
    assert set(visitor.manifest.files) == set(full.manifest.files) - detailed_paths
    assert before == _file_snapshot(source)
    # An invalid native table must not participate in visitor enumeration or validation.
    next(source.glob(".downforce/detailed/canonical/*/revisions/*/laps.parquet")).write_bytes(
        b"corrupt retained Step 9 data"
    )
    corrupt_before = _file_snapshot(source)
    assert prepare_runtime_release(source, include_detailed=False).body == visitor.body
    assert corrupt_before == _file_snapshot(source)
    with pytest.raises(StorageIntegrityError):
        prepare_runtime_release(source)


@pytest.mark.parametrize("failure", ["normalized", "alias", "archive", "analytics", "model"])
def test_visitor_export_still_validates_original_core(tmp_path: Path, failure: str) -> None:
    from downforce_core.runtime_blob.export import prepare_runtime_release

    source, _, _, _ = fixture(tmp_path)
    patterns = {
        "normalized": ".downforce/normalized/*/*/tables/*.parquet",
        "alias": ".downforce/aliases/*.json",
        "archive": ".downforce/archive/sessions/*/active.json",
        "analytics": ".downforce/analytics/manifest.json",
        "model": "artifacts/ml/registry.json",
    }
    next(source.glob(patterns[failure])).write_bytes(b"corrupt original artifact")
    with pytest.raises(StorageIntegrityError):
        prepare_runtime_release(source, include_detailed=False)


def test_export_deterministic_whitelist_immutable_and_retained_revision(tmp_path: Path) -> None:
    source, manifest, detailed, retained = fixture(tmp_path)
    requested = str(SessionRef(2024, 1, "R").session_id)
    canonical = f".downforce/normalized/{manifest.session_id}/{manifest.dataset_id}"
    expected_sources = {
        f".downforce/aliases/{requested}.json",
        f".downforce/normalized/{manifest.session_id}/active.json",
        f"{canonical}/manifest.json",
        ".downforce/archive/catalog/historical-catalog.json",
        ".downforce/analytics/manifest.json",
        ".downforce/analytics/driver-race.parquet",
        "artifacts/ml/registry.json",
    }
    expected_sources.update(
        f"{canonical}/tables/{name}.parquet"
        for name, declaration in manifest.tables.items()
        if declaration.materialized
    )
    detailed_session = ".downforce/detailed/canonical/2024-01-race"
    expected_sources.add(f"{detailed_session}/active.json")
    for revision in (retained, detailed.manifest("2024-01-race")["revision"]):
        expected_sources.update(
            f"{detailed_session}/revisions/{revision}/{name}"
            for name in ("manifest.json", "laps.parquet")
        )
    for round_number in (1, 2):
        session = f".downforce/archive/sessions/archive-2000-round-{round_number:02d}-race"
        revision = json.loads((source / session / "active.json").read_bytes())["data_revision"]
        expected_sources.add(f"{session}/active.json")
        expected_sources.update(
            f"{session}/revisions/{revision}/{name}"
            for name in ("manifest.json", "results.parquet", "laps.parquet", "pit_stops.parquet")
        )
    registry = json.loads((source / "artifacts/ml/registry.json").read_bytes())
    expected_sources.add(f"artifacts/ml/bundles/{registry['active_bundle_id']}.json")
    expected_generated = {".downforce/analytics/circuit-identities.json"}
    excluded = {
        ".downforce/archive/raw/provider/private.json",
        ".downforce/providers/raw-payload.json",
        ".downforce/jobs/job.json",
        ".downforce/completion/status.json",
        ".downforce/studies/study.json",
        ".downforce/agents/context.json",
        ".downforce/prompts/prompt.txt",
        ".downforce/reports/report.json",
        ".downforce/cache/cached.json",
        ".downforce/workflows/workflow.json",
        ".downforce/deployment/private.json",
        "tests/private-fixture.json",
        "artifacts/private.json",
        "artifacts/ml/report.json",
        f"{canonical}/tables/private.parquet",
        f"{detailed_session}/revisions/.staging-private/report.json",
    }
    for name in excluded:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private bytes must not be published")
    before = _file_snapshot(source)
    first, second = export(source), export(source)
    assert first.body == second.body and first.sha256 == second.sha256
    assert first.sha256 == hashlib.sha256(first.body).hexdigest()
    assert set(first.manifest.files) == expected_sources | expected_generated
    assert set(first.source_files) == expected_sources
    assert set(first.generated_files) == expected_generated
    assert not excluded.intersection(first.manifest.files)
    assert first.source_files == {name: source / name for name in expected_sources}
    for name in expected_sources:
        original, digest, _ = before[source / name]
        assert b"".join(first.reader().iter_bytes(name, max_bytes=len(original))) == original
        assert first.manifest.entry(name).sha256 == digest
    catalog = ".downforce/archive/catalog/historical-catalog.json"
    assert b"https://example.invalid" in before[source / catalog][0]
    assert source.as_posix().encode() not in first.body
    assert not (source / ".downforce/analytics/circuit-identities.json").exists()
    assert before == _file_snapshot(source)
    with pytest.raises(TypeError):
        first.source_files["x"] = source
    cache = RuntimeBlobCache(tmp_path / "runtime", first.manifest, first.reader())
    assert (
        BlobDetailedStore(cache)
        .read_table("2024-01-race", "laps", revision=retained)
        .equals(detailed.read_table("2024-01-race", "laps", revision=retained))
    )
    assert BlobCircuitIdentityResolver.from_cache(cache).identities == {
        (2000, 1): "circuit-one",
        (2000, 2): "circuit-two",
    }
    assert any(manifest.dataset_id in p for p in first.source_files)


def _file_snapshot(root: Path) -> dict[Path, tuple[bytes, str, int]]:
    snapshot = {}
    for path in root.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            snapshot[path] = (body, hashlib.sha256(body).hexdigest(), path.stat().st_mtime_ns)
    return snapshot


@pytest.mark.parametrize("failure", ["missing", "stale", "alias", "archive-pointer", "identities"])
def test_export_rejects_invalid_published_data(tmp_path: Path, failure: str) -> None:
    source, manifest, _, _ = fixture(tmp_path)
    if failure == "missing":
        next(source.glob(".downforce/detailed/canonical/*/revisions/*/laps.parquet")).unlink()
    elif failure == "stale":
        path = source / f".downforce/normalized/{manifest.session_id}/active.json"
        path.write_text(json.dumps({"session_id": "wrong", "dataset_id": manifest.dataset_id}))
    elif failure == "alias":
        next(source.glob(".downforce/aliases/*.json")).write_text('{"session_id":"absent"}')
    elif failure == "archive-pointer":
        next(source.glob(".downforce/archive/sessions/*/active.json")).write_text(
            json.dumps({"data_revision": "archive-revision-sha256-" + "a" * 64})
        )
    else:
        path = source / ".downforce/analytics/manifest.json"
        value = json.loads(path.read_text())
        value["provider_circuit_identities"] += 1
        path.write_text(json.dumps(value))
    with pytest.raises(StorageIntegrityError):
        export(source)


@pytest.mark.parametrize("failure", ["requested-filename", "unpublished-target"])
def test_export_rejects_structurally_valid_alias_conflicts(tmp_path: Path, failure: str) -> None:
    from downforce_core.storage.repository import DownforceRepository

    source, manifest, _, _ = fixture(tmp_path)
    requested = str(SessionRef(2024, 1, "R").session_id)
    path = source / f".downforce/aliases/{requested}.json"
    alias = {
        "requested_session_id": requested,
        "canonical_session_id": manifest.session_id,
    }
    if failure == "requested-filename":
        alias["requested_session_id"] = "another-safe-request"
        diagnostic = "session alias does not match its filename"
    else:
        alias["canonical_session_id"] = "safe-unpublished-session"
        diagnostic = "release alias target is not included"
        assert not (source / ".downforce/normalized/safe-unpublished-session").exists()
    path.write_text(json.dumps(alias))
    before = _file_snapshot(source)
    repository = DownforceRepository(source)
    if failure == "requested-filename":
        with pytest.raises(StorageIntegrityError, match=diagnostic):
            repository.resolve_session_id(requested)
    else:
        assert repository.resolve_session_id(requested) == "safe-unpublished-session"
    with pytest.raises(StorageIntegrityError, match=diagnostic):
        export(source)
    assert before == _file_snapshot(source)


def test_export_reader_rejects_post_index_mutations(tmp_path: Path) -> None:
    source, _, _, _ = fixture(tmp_path)
    release = export(source)
    path, original = next(iter(release.source_files.items()))
    original.write_bytes(original.read_bytes() + b" ")
    with pytest.raises(StorageIntegrityError):
        b"".join(release.reader().iter_bytes(path, max_bytes=512 * 1024**2))


def test_export_rejects_hardlinked_source(tmp_path: Path) -> None:
    source, _, _, _ = fixture(tmp_path)
    target = source / ".downforce/analytics/driver-race.parquet"
    os.link(target, tmp_path / "linked.parquet")
    with pytest.raises(StorageIntegrityError, match="unsafe"):
        export(source)


def test_metadata_output_reuses_identical_and_refuses_unknown(tmp_path: Path) -> None:
    source, _, _, _ = fixture(tmp_path)
    release = export(source)
    output = release.write_metadata()
    assert output == source / ".downforce/deployment/releases" / release.sha256
    assert (output / "release.json").read_bytes() == release.body
    before = _file_snapshot(output)
    assert release.write_metadata() == output
    assert before == _file_snapshot(output)
    (output / "unknown.json").write_text("owned by user")
    before = _file_snapshot(output)
    with pytest.raises(StorageIntegrityError, match="output"):
        release.write_metadata()
    assert (output / "unknown.json").read_text() == "owned by user"
    assert before == _file_snapshot(output)


@pytest.mark.parametrize("failure", ["incomplete", "different"])
def test_metadata_output_refuses_existing_incomplete_or_different_bytes(
    tmp_path: Path, failure: str
) -> None:
    source, _, _, _ = fixture(tmp_path)
    release = export(source)
    output = source / ".downforce/deployment/releases" / release.sha256
    output.mkdir(parents=True)
    (output / "release.json").write_bytes(release.body)
    if failure == "different":
        (output / "circuit-identities.json").write_bytes(b"different existing bytes")
    before = _file_snapshot(source)
    diagnostic = "output.*incomplete" if failure == "incomplete" else "output differs"
    with pytest.raises(StorageIntegrityError, match=diagnostic):
        release.write_metadata()
    assert before == _file_snapshot(source)


@pytest.mark.parametrize("name", ["release.json", "circuit-identities.json"])
def test_metadata_output_rejects_hardlinked_known_file_without_touching_links(
    tmp_path: Path, name: str
) -> None:
    source, _, _, _ = fixture(tmp_path)
    release = export(source)
    output = release.write_metadata()
    outside = tmp_path / "outside-known.json"
    os.link(output / name, outside)
    unknown = output.parent / "unknown.json"
    unknown.write_bytes(b"unknown sibling must survive")
    before = _file_snapshot(tmp_path)
    with pytest.raises(StorageIntegrityError, match="output file is unsafe"):
        release.write_metadata()
    assert before == _file_snapshot(tmp_path)
    assert (output / name).stat().st_nlink == outside.stat().st_nlink == 2
    assert os.path.samefile(output / name, outside)


@pytest.mark.parametrize("link_kind", ["junction", "symlink"])
@pytest.mark.parametrize(
    "ancestor", [".downforce", ".downforce/deployment", ".downforce/deployment/releases"]
)
def test_metadata_output_rejects_real_linked_ancestry_before_writing(
    tmp_path: Path, link_kind: str, ancestor: str
) -> None:
    if link_kind == "junction" and os.name != "nt":
        pytest.skip("Windows junction-specific check")
    source, _, _, _ = fixture(tmp_path)
    project = tmp_path / "output-project"
    project.mkdir()
    release = replace(export(source), project_root=project)
    outside = tmp_path / "outside-deployment"
    outside.mkdir()
    (outside / "unknown.json").write_bytes(b"outside bytes must survive")
    link = project / ancestor
    link.parent.mkdir(parents=True, exist_ok=True)
    if link_kind == "junction":
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("Windows symlink creation requires privileges")
            raise
    before = _file_snapshot(outside)
    directories_before = {path for path in outside.rglob("*") if path.is_dir()}
    try:
        with pytest.raises(StorageIntegrityError, match="output path is unsafe"):
            release.write_metadata()
        assert before == _file_snapshot(outside)
        assert directories_before == {path for path in outside.rglob("*") if path.is_dir()}
        assert list(project.rglob("release.json")) == []
        assert os.path.samefile(link, outside)
    finally:
        if link_kind == "junction":
            os.rmdir(link)
        else:
            link.unlink()
    assert outside.is_dir()


def test_export_rejects_linked_project_ancestor(tmp_path: Path, monkeypatch) -> None:
    from downforce_core.runtime_blob.export import _safe_path

    root = tmp_path / "project"
    root.mkdir()
    original = Path.lstat

    def linked(path: Path):
        if path == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original(path)

    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(StorageIntegrityError, match="unsafe"):
        _safe_path(root, root)


def test_offline_reader_obeys_default_cache_chunk_limit_for_large_objects(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.export import RuntimeRelease
    from downforce_core.runtime_blob.manifest import RuntimeManifest

    generated_path = ".downforce/analytics/circuit-identities.json"
    source_path = ".downforce/analytics/driver-race.parquet"
    content = b"x" * 70000
    source = tmp_path / source_path
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    body = json.dumps(
        {
            "schema_version": 1,
            "files": {
                path: {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for path in (generated_path, source_path)
            },
        }
    ).encode()
    digest = hashlib.sha256(body).hexdigest()
    release = RuntimeRelease(
        tmp_path,
        body,
        digest,
        RuntimeManifest.from_bytes(body, digest),
        {source_path: source},
        {generated_path: content},
    )
    for path in (generated_path, source_path):
        chunks = tuple(release.reader().iter_bytes(path, max_bytes=len(content)))
        assert max(map(len, chunks)) <= 65536
        assert b"".join(chunks) == content
    cache = RuntimeBlobCache(tmp_path / "cache", release.manifest, release.reader())
    with cache.lease((generated_path, source_path)) as files:
        assert all(path.read_bytes() == content for path in files.values())


def test_export_source_repository_rejects_publication(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.export import _SourceRepository

    repository, normalized, _ = repository_fixture(tmp_path)
    readonly = _SourceRepository(repository.layout.project_root)
    with pytest.raises(StorageIntegrityError, match="read-only"):
        readonly.write_session(normalized, "fixture")


def test_source_guard_rejects_out_of_root_path(tmp_path: Path) -> None:
    from downforce_core.runtime_blob.export import _safe_path

    with pytest.raises(StorageIntegrityError, match="unsafe"):
        _safe_path(tmp_path, tmp_path.parent)
