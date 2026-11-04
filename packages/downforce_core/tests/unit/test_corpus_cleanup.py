"""Only proven, explicitly owned corpus duplicates are eligible for release."""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import source_manifest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.parquet import file_sha256

fixtures = importlib.import_module("test_corpus_ingest")


def cleanup() -> Any:
    name = "downforce_core.detailed.corpus_cleanup"
    assert importlib.util.find_spec(name) is not None, "safe owned corpus release is missing"
    return importlib.import_module(name)


def completed(
    tmp_path: Path, *, frozen: bool = False
) -> tuple[DetailedPaths, dict[str, Any], Path | None]:
    paths = fixtures.ready(tmp_path, additional=0 if frozen else 1)
    working: list[Path] = []

    def load(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        if offline:
            working.append(cache)
        return fixtures.loader(entry, cache, offline)

    identity = "2018-01-practice-1" if frozen else "2018-02-practice-1"
    result = fixtures.module().run_corpus(
        tmp_path, detailed_root=paths.root, session_ids={identity}, load_session=load
    )
    assert result["failed"] == 0
    return paths, result["sessions"][0], working[0] if working else None


def test_release_preserves_lossless_artifacts_and_provenance_but_frees_known_duplicates(
    tmp_path: Path,
) -> None:
    release = cleanup()
    paths, row, working = completed(tmp_path)
    identity = row["session_id"]
    directory = paths.root / f"corpus/staging/{identity}"
    native_path = directory / "native.json"
    native = json.loads(native_path.read_text())
    current = Path(native["source_directory"])
    unknown = directory / ("attempt-" + "f" * 32)
    unknown.mkdir()
    (unknown / "retain.bin").write_bytes(b"failed attempt, not eligible")
    offline_unknown = directory / ("offline-" + "f" * 32)
    offline_unknown.mkdir()
    (offline_unknown / "retain.bin").write_bytes(b"unregistered offline attempt")
    frozen_before = source_manifest(paths.root / "preflight")
    preserved = {
        path: file_sha256(path)
        for path in [
            native_path,
            paths.root / f"corpus/jobs/{identity}.json",
            directory / f"provider-restore-{row['raw_identity']}.json",
            paths.raw / f"{row['raw_identity']}.zip",
            paths.canonical / identity / "active.json",
        ]
    }
    result = release.release_owned_session(tmp_path, paths, identity)
    assert result["state"] == "RELEASED" and result["released_bytes"] > 0
    assert not current.exists() and working is not None and not working.exists()
    assert not (directory / f"restore-{row['raw_identity']}").exists()
    assert unknown.is_dir() and offline_unknown.is_dir()
    assert preserved == {path: file_sha256(path) for path in preserved}
    assert frozen_before == source_manifest(paths.root / "preflight")
    again = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("released COMPLETE must not download"),
    )
    assert again["failed"] == 0 and again["attempted"] == 0 and again["reused"] == 1
    assert again["sessions"][0]["canonical_identity"] == row["canonical_identity"]


def test_frozen_reuse_release_never_deletes_or_changes_original_study_source(
    tmp_path: Path,
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path, frozen=True)
    original = source_manifest(paths.root / "preflight")
    study = source_manifest(paths.root / "study")
    result = release.release_owned_session(tmp_path, paths, row["session_id"])
    assert result["state"] == "RELEASED"
    assert original == source_manifest(paths.root / "preflight")
    assert study == source_manifest(paths.root / "study")


@pytest.mark.parametrize(
    "problem", ["raw", "canonical", "checkpoint", "provider-proof", "extra-file", "broad-path"]
)
def test_any_unverified_artifact_or_target_prevents_all_deletion(
    tmp_path: Path, problem: str
) -> None:
    release = cleanup()
    paths, row, working = completed(tmp_path)
    identity = row["session_id"]
    directory = paths.root / f"corpus/staging/{identity}"
    native_path = directory / "native.json"
    native = json.loads(native_path.read_text())
    current = Path(native["source_directory"])
    if problem == "raw":
        (paths.raw / f"{row['raw_identity']}.zip").write_bytes(b"corrupt")
    elif problem == "canonical":
        (
            paths.canonical / identity / "revisions" / row["canonical_identity"] / "laps.parquet"
        ).write_bytes(b"corrupt")
    elif problem == "checkpoint":
        job = paths.root / f"corpus/jobs/{identity}.json"
        value = json.loads(job.read_text())
        value["state"] = "FAILED"
        fixtures.save(job, value)
    elif problem == "provider-proof":
        (directory / f"provider-restore-{row['raw_identity']}.json").write_text("{}")
    elif problem == "extra-file":
        (current / "user-notes.txt").write_bytes(b"must not be deleted")
    else:
        native["source_directory"] = str(paths.root / "corpus")
        fixtures.save(native_path, native)
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, identity)
    assert current.exists() and working is not None and working.exists()
    assert (directory / f"restore-{row['raw_identity']}").exists()


def test_opt_in_releases_only_completed_sessions_and_remains_idempotent(tmp_path: Path) -> None:
    cleanup()
    paths = fixtures.ready(tmp_path, additional=1)
    identity = "2018-02-practice-1"
    first = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=fixtures.loader,
        release_cache=True,
    )
    assert first["failed"] == 0
    second = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("idempotent released completion must not load"),
        release_cache=True,
    )
    assert second["failed"] == 0 and second["attempted"] == 0
    assert first["sessions"][0]["canonical_identity"] == second["sessions"][0]["canonical_identity"]


def prepare_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, DetailedPaths, dict[str, Any], Path]:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"

    def crash(target: Path) -> None:
        raise RuntimeError("interrupted before removal")

    with monkeypatch.context() as patch:
        patch.setattr(release.shutil, "rmtree", crash)
        with pytest.raises(RuntimeError, match="interrupted"):
            release.release_owned_session(tmp_path, paths, row["session_id"])
    assert json.loads((directory / "release.json").read_text())["state"] == "PREPARED"
    return release, paths, row, directory


@pytest.mark.parametrize(
    "problem",
    [
        "new-directory",
        "hardlink",
        "wrong-attempt",
        "new-file",
        "changed-file",
        "recreated-root",
        "changed-directory-time",
    ],
)
def test_prepared_resume_rejects_changed_or_unregistered_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    release, paths, row, directory = prepare_interrupted(tmp_path, monkeypatch)
    receipt_path = directory / "release.json"
    receipt = json.loads(receipt_path.read_text())
    current = paths.root / "corpus" / receipt["targets"][0]["path"]
    if problem == "new-directory":
        (current / "unregistered-empty").mkdir()
    elif problem == "hardlink":
        os.link(current / "results.parquet", tmp_path / "outside-owned.parquet")
    elif problem == "wrong-attempt":
        unrelated = directory / ("attempt-" + "e" * 32)
        unrelated.mkdir()
        receipt["targets"][0] = {
            "path": unrelated.relative_to(paths.root / "corpus").as_posix(),
            "snapshot": source_manifest(unrelated),
        }
        receipt["targets_sha256"] = release._targets_digest(receipt["targets"])
        fixtures.save(receipt_path, receipt)
    elif problem == "new-file":
        (current / "notes").write_bytes(b"user bytes")
    elif problem == "changed-file":
        (current / "results.parquet").write_bytes(b"changed")
    elif problem == "changed-directory-time":
        observed = (current / "car_data").stat()
        os.utime(
            current / "car_data", ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000_000)
        )
    else:
        current.rename(directory / "retained-original")
        current.mkdir()
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists()
    assert (directory / f"restore-{row['raw_identity']}").exists()


def test_binding_is_rechecked_immediately_before_any_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, paths, row, directory = prepare_interrupted(tmp_path, monkeypatch)
    original = release._artifacts
    calls = 0

    def mutate(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            native_path = directory / "native.json"
            value = json.loads(native_path.read_text())
            value["unregistered_observation"] = True
            fixtures.save(native_path, value)
        return original(*args)

    monkeypatch.setattr(release, "_artifacts", mutate)
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert (directory / f"restore-{row['raw_identity']}").exists()


@pytest.mark.parametrize("missing_nested_member", [False, True])
def test_prepared_root_timestamp_tamper_preserves_all_remaining_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_nested_member: bool
) -> None:
    release, paths, row, directory = prepare_interrupted(tmp_path, monkeypatch)
    receipt = json.loads((directory / "release.json").read_text())
    current = paths.root / "corpus" / receipt["targets"][0]["path"]
    if missing_nested_member:
        (current / "car_data/44.parquet").unlink()
    children = source_manifest(current)
    observed = current.stat()
    os.utime(current, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000_000))
    assert children == source_manifest(current)
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists()
    assert (directory / f"restore-{row['raw_identity']}").exists()


def test_registration_is_bound_to_genuine_offline_receipt(tmp_path: Path) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    ownership_path = directory / f"offline-owner-{row['raw_identity']}.json"
    ownership = json.loads(ownership_path.read_text())
    unrelated = directory / ("offline-" + "e" * 32)
    unrelated.mkdir()
    ownership["path"] = unrelated.relative_to(paths.root / "corpus").as_posix()
    ownership["snapshot"] = source_manifest(unrelated)
    fixtures.save(ownership_path, ownership)
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert unrelated.is_dir()


@pytest.mark.parametrize("location", ["attempt", "candidate"])
def test_unregistered_empty_directory_stops_initial_release(tmp_path: Path, location: str) -> None:
    release = cleanup()
    paths, row, working = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    native = json.loads((directory / "native.json").read_text())
    current = Path(native["source_directory"])
    target = (
        current
        if location == "attempt"
        else paths.root
        / f"corpus/candidates/canonical/{row['session_id']}/revisions/{row['canonical_identity']}"
    )
    (target / "user-empty-directory").mkdir()
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists() and working is not None and working.exists()


def test_raw_bytes_are_rechecked_when_prepared_snapshot_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    current = Path(json.loads((directory / "native.json").read_text())["source_directory"])
    original = release._snapshot
    changed = False

    def mutate(path: Path, owned: DetailedPaths) -> Any:
        nonlocal changed
        if path == current and not changed:
            changed = True
            (current / "raw-cache/captured-provider.bin").write_bytes(b"changed during validation")
        return original(path, owned)

    monkeypatch.setattr(release, "_snapshot", mutate)
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists()


@pytest.mark.parametrize("kind", ["symlink", "junction", "hardlink"])
def test_initial_release_rejects_links_and_preserves_external_data(
    tmp_path: Path, kind: str
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    current = Path(json.loads((directory / "native.json").read_text())["source_directory"])
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "retain.bin"
    sentinel.write_bytes(b"outside corpus ownership")
    if kind == "hardlink":
        os.link(current / "results.parquet", external / "linked.parquet")
    elif kind == "junction":
        if sys.platform != "win32":
            pytest.skip("Windows junction regression")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(current / "junction"), str(external)],
            check=True,
            capture_output=True,
        )
    else:
        try:
            (current / "symlink").symlink_to(sentinel)
        except OSError as exc:
            pytest.skip(f"OS does not permit symlink creation: {exc}")
    with pytest.raises(ValueError, match="link"):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists() and sentinel.read_bytes() == b"outside corpus ownership"


@pytest.mark.parametrize(
    "point", ["partial-file", "partial-nested-file", "between-targets", "before-released-receipt"]
)
def test_interrupted_release_resumes_only_prevalidated_targets_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    identity = row["session_id"]
    directory = paths.root / f"corpus/staging/{identity}"
    native_path = directory / "native.json"
    current = Path(json.loads(native_path.read_text())["source_directory"])
    job_path = paths.root / f"corpus/jobs/{identity}.json"
    preserved = {
        path: file_sha256(path)
        for path in (native_path, job_path, paths.canonical / identity / "active.json")
    }
    remove = release.shutil.rmtree
    save = release._save
    calls = 0

    def crash_removal(target: Path) -> None:
        nonlocal calls
        calls += 1
        if point in {"partial-file", "partial-nested-file"} and calls == 1:
            (
                target / ("results.parquet" if point == "partial-file" else "car_data/44.parquet")
            ).unlink()
            raise RuntimeError("simulated crash")
        if point == "between-targets" and calls == 2:
            raise RuntimeError("simulated crash")
        remove(target)

    def crash_receipt(path: Path, value: dict[str, Any]) -> None:
        if point == "before-released-receipt" and value["state"] == "RELEASED":
            raise RuntimeError("simulated crash")
        save(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(release.shutil, "rmtree", crash_removal)
        patch.setattr(release, "_save", crash_receipt)
        with pytest.raises(RuntimeError, match="simulated crash"):
            release.release_owned_session(tmp_path, paths, identity)
    receipt_path = directory / "release.json"
    prepared = json.loads(receipt_path.read_text())
    assert prepared["state"] == "PREPARED"
    before = source_manifest(directory)
    reuse = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("prepared COMPLETE must not load"),
    )
    assert reuse["failed"] == 0 and reuse["attempted"] == 0
    assert source_manifest(directory) == before  # Default false never resumes deletion.
    unknown = directory / ("attempt-" + "f" * 32)
    unknown.mkdir()
    (unknown / "retain.bin").write_bytes(b"unrelated failed attempt")
    resumed = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("release resume must not load"),
        release_cache=True,
    )
    assert resumed["failed"] == 0 and resumed["attempted"] == 0
    assert resumed["sessions"][0]["canonical_identity"] == row["canonical_identity"]
    released = json.loads(receipt_path.read_text())
    assert released["state"] == "RELEASED" and released["targets"] == prepared["targets"]
    assert not current.exists() and unknown.exists()
    assert preserved == {path: file_sha256(path) for path in preserved}


@pytest.mark.parametrize("problem", ["provider", "snapshot", "released-recreated"])
def test_released_complete_revalidates_proof_and_never_deletes_recreated_target(
    tmp_path: Path, problem: str
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    identity = row["session_id"]
    directory = paths.root / f"corpus/staging/{identity}"
    receipt = release.release_owned_session(tmp_path, paths, identity)
    target = paths.root / "corpus" / receipt["targets"][0]["path"]
    if problem == "provider":
        proof = directory / f"provider-restore-{row['raw_identity']}.json"
        value = json.loads(proof.read_text())
        value["native_tables_exact"] = False
        fixtures.save(proof, value)
    elif problem == "snapshot":
        receipt["targets"][0]["snapshot"]["files"][0]["sha256"] = "0" * 64
        fixtures.save(directory / "release.json", receipt)
    else:
        target.mkdir()
        (target / "user.bin").write_bytes(b"recreated target is not owned")
    with pytest.raises(ValueError):
        fixtures.module().run_corpus(
            tmp_path,
            detailed_root=paths.root,
            session_ids={identity},
            load_session=lambda *_: pytest.fail("released COMPLETE must not load"),
            release_cache=True,
        )
    if problem == "released-recreated":
        assert (target / "user.bin").read_bytes() == b"recreated target is not owned"


def test_legacy_unbound_offline_receipt_stays_usable_but_is_not_release_authority(
    tmp_path: Path,
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    proof = directory / f"provider-restore-{row['raw_identity']}.json"
    value = json.loads(proof.read_text())
    value.pop("offline_owner_sha256")
    fixtures.save(proof, value)
    reuse = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={row["session_id"]},
        load_session=lambda *_: pytest.fail("legacy COMPLETE must not load"),
    )
    assert reuse["failed"] == 0 and reuse["attempted"] == 0
    with pytest.raises(ValueError):
        release.release_owned_session(tmp_path, paths, row["session_id"])


def test_symlink_guard_without_os_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path)
    directory = paths.root / f"corpus/staging/{row['session_id']}"
    current = Path(json.loads((directory / "native.json").read_text())["source_directory"])
    original = Path.is_symlink

    def linked(path: Path) -> bool:
        return path == current / "results.parquet" or original(path)

    monkeypatch.setattr(Path, "is_symlink", linked)
    with pytest.raises(ValueError, match="link"):
        release.release_owned_session(tmp_path, paths, row["session_id"])
    assert current.exists()


def test_offline_registration_crash_leaves_unknown_attempt_and_reverifies_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup()
    ingest = fixtures.module()
    paths = fixtures.ready(tmp_path, additional=1)
    identity = "2018-02-practice-1"
    directory = paths.root / f"corpus/staging/{identity}"
    original = ingest._save

    def crash(path: Path, value: dict[str, Any]) -> None:
        if path.name.startswith("provider-restore-"):
            assert "offline_owner_sha256" in value
            raise RuntimeError("crash after registered offline ownership")
        original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_save", crash)
        first = ingest.run_corpus(
            tmp_path, detailed_root=paths.root, session_ids={identity}, load_session=fixtures.loader
        )
    assert first["failed"] == 1
    old = list(directory.glob("offline-*"))
    attempts = [path for path in old if path.is_dir()]
    assert len(attempts) == 1
    assert list(directory.glob("offline-owner-*.json"))
    calls = []

    def offline_only(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        assert offline
        calls.append(offline)
        return fixtures.loader(entry, cache, offline)

    resumed = ingest.run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=offline_only,
        release_cache=True,
    )
    assert resumed["failed"] == 0 and resumed["attempted"] == 0 and calls == [True]
    assert attempts[0].exists()  # Interrupted, unreceipted attempts never become release targets.


def test_released_frozen_complete_rechecks_study_proof_read_only(tmp_path: Path) -> None:
    release = cleanup()
    paths, row, _ = completed(tmp_path, frozen=True)
    release.release_owned_session(tmp_path, paths, row["session_id"])
    before = source_manifest(paths.root / "preflight")
    proof = paths.root / f"study/provider-restore/{row['session_id']}.json"
    value = json.loads(proof.read_text())
    value["offline"] = False
    fixtures.save(proof, value)
    study_before = source_manifest(paths.root / "study")
    reuse = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={row["session_id"]},
        load_session=lambda *_: pytest.fail("frozen COMPLETE must not load"),
    )
    assert reuse["failed"] == 1 and reuse["attempted"] == 0
    assert before == source_manifest(paths.root / "preflight")
    assert study_before == source_manifest(paths.root / "study")


@pytest.mark.parametrize("variant", ["nanoseconds", "all-zero"])
def test_owned_release_preserves_original_native_values_types_and_quality(
    tmp_path: Path, variant: str
) -> None:
    import pandas as pd  # type: ignore[import-untyped]

    paths = fixtures.ready(tmp_path, additional=1)
    identity = "2018-02-practice-1"

    def load(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        session = fixtures.loader(entry, cache, offline)
        car = session.car_data["44"]
        car["SessionTime"] = pd.to_timedelta([1000001, 999999], unit="ns")
        car["Date"] = pd.to_datetime([1000000000001, 1000000000002], unit="ns")
        car["CustomProviderField"] = ["untouched", "second"]
        if variant == "all-zero":
            for channel in ("Speed", "RPM", "Throttle", "nGear"):
                car[channel] = [0, 0]
            session.laps["LapTime"] = pd.to_timedelta([82_000_000_001], unit="ns")
            session.laps["IsAccurate"] = [True]
        return session

    first = fixtures.module().run_corpus(
        tmp_path, detailed_root=paths.root, session_ids={identity}, load_session=load
    )
    assert first["failed"] == 0
    native_path = paths.root / f"corpus/staging/{identity}/native.json"
    native_hash = file_sha256(native_path)
    store = DetailedStore(tmp_path, detailed_root=paths.root)
    before = store.validate(identity)
    car_before = store.read_table(identity, "car-data-44")
    released = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("COMPLETE release must not load"),
        release_cache=True,
    )
    assert released["failed"] == 0 and released["attempted"] == 0
    reused = fixtures.module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        session_ids={identity},
        load_session=lambda *_: pytest.fail("released reuse must not load"),
    )
    assert reused["failed"] == 0 and reused["attempted"] == 0
    assert file_sha256(native_path) == native_hash and store.validate(identity) == before
    car_after = store.read_table(identity, "car-data-44")
    assert car_before.equals(car_after, check_metadata=True)
    assert car_after["session_time"].cast("int64").to_pylist() == [1000001, 999999]
    assert car_after["timestamp"].cast("int64").to_pylist() == [1000000000001, 1000000000002]
    assert car_after["CustomProviderField"].to_pylist() == ["untouched", "second"]
    if variant == "all-zero":
        assert car_after["speed"].to_pylist() == [0, 0]
        assert str(car_after["speed"].type) == "int64"
        assert reused["sessions"][0]["quality"] == "partial"
        assert before["metadata"]["channels"]["car-data-44"]["availability"]["usable"] is False
