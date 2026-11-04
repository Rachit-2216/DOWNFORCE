"""Proof-bound release of recoverable corpus-owned duplicates, never source studies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import fastf1  # type: ignore[import-untyped]

from downforce_core.detailed.corpus_publication import canonical_payload
from downforce_core.detailed.evidence import _typed_finite_json_equal as equal
from downforce_core.detailed.lifecycle import Lifecycle
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.status import guard_operation
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path, _read, ready_inventory
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import write_text_atomic
from downforce_core.storage.parquet import file_sha256

VERSION = "corpus-owned-release-v1"


class CleanupStop(ValueError):
    """Unverified release intent or altered recoverable evidence stops the operation."""


def _save(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    guard_operation(
        path,
        next_peak_bytes=2 * len(encoded.encode()) + 4096,
        operation="corpus release provenance",
    )
    write_text_atomic(path, encoded)


def _safe_tree(path: Path, root: Path) -> None:
    _path(root, path.relative_to(root).as_posix())
    if not path.exists():
        return
    if not path.is_dir():
        raise CleanupStop("release target must be a directory")
    for directory, names, files in os.walk(path, followlinks=False):
        for name in names + files:
            child = Path(directory) / name
            if child.is_symlink() or child.is_junction():
                raise CleanupStop("linked entry in owned release target")
            if child.is_file() and child.stat().st_nlink != 1:
                raise CleanupStop("hardlinked file in owned release target")
            _path(root, child.relative_to(root).as_posix())


def _snapshot(path: Path, owned: DetailedPaths) -> dict[str, Any]:
    _safe_tree(path, owned.root)
    manifest = source_manifest(path)

    def object_identity(member: Path) -> dict[str, int]:
        observed = member.stat()
        return {"device": observed.st_dev, "inode": observed.st_ino, "mode": observed.st_mode}

    root = {**object_identity(path), "mtime_ns": path.stat().st_mtime_ns}
    return {
        **manifest,
        "root": root,
        "objects": {
            record["path"]: object_identity(path / record["path"])
            for record in [*manifest["files"], *manifest["directories"]]
        },
    }


def _targets_digest(targets: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(targets, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _target(owned: DetailedPaths, identity: str, relative: str, revision: str, bundle: str) -> Path:
    token = Path(relative)
    parts = token.parts
    valid = (
        len(parts) == 3
        and parts[:2] == ("staging", identity)
        and (
            re.fullmatch(r"(?:attempt|offline)-[0-9a-f]{32}", parts[2]) is not None
            or parts[2] == f"restore-{bundle}"
        )
    ) or parts == ("candidates", "canonical", identity, "revisions", revision)
    if token.is_absolute() or ".." in parts or not valid:
        raise CleanupStop("release target is not an exact owned session duplicate")
    path = _path(owned.root, token.as_posix())
    _safe_tree(path, owned.root)
    return path


def _close_owned_cache(owned: DetailedPaths, targets: list[Path]) -> None:
    configured = fastf1.Cache._CACHE_DIR
    cached = fastf1.Cache._requests_session_cached
    if configured is None or cached is None:
        return
    path = Path(configured).absolute()
    if path.is_relative_to(owned.root) and any(
        path == target or path.is_relative_to(target) for target in targets
    ):
        _path(owned.root, path.relative_to(owned.root).as_posix())
        cached.close()


def register_verified_offline(
    paths: DetailedPaths, identity: str, bundle: str, working: Path
) -> str:
    """Called only after genuine offline native equivalence, before its durable receipt."""
    validate_safe_identifier(identity)
    owned = DetailedPaths(paths.root.parent, override=paths.root / "corpus")
    path = _target(owned, identity, working.relative_to(owned.root).as_posix(), "", bundle)
    if not re.fullmatch(r"offline-[0-9a-f]{32}", path.name):
        raise CleanupStop("offline ownership registration requires an exact offline attempt")
    _close_owned_cache(owned, [path])
    receipt_path = owned.root / f"staging/{identity}/offline-owner-{bundle}.json"
    _save(
        receipt_path,
        {
            "raw_identity": bundle,
            "path": path.relative_to(owned.root).as_posix(),
            "snapshot": _snapshot(path, owned),
        },
    )
    return file_sha256(receipt_path)


def _artifacts(project: Path, paths: DetailedPaths, identity: str) -> dict[str, Any]:
    validate_safe_identifier(identity)
    owned = DetailedPaths(project, override=paths.root / "corpus")
    directory = _path(owned.root, f"staging/{identity}")
    native_path = _path(owned.root, f"staging/{identity}/native.json")
    native = _read(native_path)
    job = Lifecycle(owned, identity).read()
    if job["state"] != "COMPLETE" or job["checkpoint"] != "COMPLETE":
        raise CleanupStop("only completed validated corpus jobs can release owned duplicates")
    catalog, _ = ready_inventory(paths.root)
    entries = [
        row
        for row in catalog["sessions"]
        if row["session_id"] == identity and row["state"] == "completed"
    ]
    if len(entries) != 1 or not equal(native["session"], entries[0]):
        raise CleanupStop("release native identity differs from current completed inventory")
    bundle, revision = job["raw_identity"], job["canonical_identity"]
    raw = RawEvidenceStore(paths.raw).validate(bundle)
    if (
        raw["session_id"] != identity
        or raw["provider"] != "fastf1"
        or raw["provider_version"] != native["provider_version"]
    ):
        raise CleanupStop("release raw identity differs from native provenance")
    store = DetailedStore(project, detailed_root=paths.root)
    canonical = store.validate(identity, revision=revision)
    if store.manifest(identity)["revision"] != revision:
        raise CleanupStop("release canonical revision is not the production activation")
    original, normalized = {}, {}
    for name, descriptor in native["tables"].items():
        key = name.replace("/", "-").replace("_", "-")
        table = store.read_table(identity, key, revision=revision)
        mapping = canonical["metadata"]["channels"][key]["native_fields"]
        columns = descriptor["columns"]
        if set(columns) != set(mapping):
            raise CleanupStop("canonical native field inventory differs from durable provenance")
        restored = (
            table.select([mapping[column] for column in columns])
            .rename_columns(columns)
            .replace_schema_metadata(table.schema.metadata)
        )
        if restored.num_rows != descriptor["rows"] or str(restored.schema) != descriptor["schema"]:
            raise CleanupStop("canonical native schema differs from durable provenance")
        original[name], normalized[key] = restored, table
    expected, metadata = canonical_payload(entries[0], original, native, bundle)
    if set(expected) != set(canonical["tables"]) or not equal(metadata, canonical["metadata"]):
        raise CleanupStop("canonical lossless reconstruction metadata differs")
    if any(
        not table.equals(normalized[name], check_metadata=True) for name, table in expected.items()
    ):
        raise CleanupStop("canonical lossless reconstruction values differ")
    if "frozen_bundle" in native:
        proof = _path(paths.root, f"study/provider-restore/{identity}.json")
        provider = _read(proof)
        if (
            native["frozen_bundle"] != bundle
            or file_sha256(proof) != native["restore_receipt_sha256"]
            or provider.get("offline") is not True
            or provider.get("native_tables_exact") is not True
            or provider.get("bundle_sha256") != bundle
            or set(provider.get("tables_checked", [])) != set(native["tables"]) - {"corners"}
        ):
            raise CleanupStop("frozen exact restoration proof differs")
    else:
        proof = _path(owned.root, f"staging/{identity}/provider-restore-{bundle}.json")
        owner_path = _path(owned.root, f"staging/{identity}/offline-owner-{bundle}.json")
        expected_proof = {
            "bundle_sha256": bundle,
            "tables": native["tables"],
            "provider_version": native["provider_version"],
            "native_tables_exact": True,
            "offline_owner_sha256": file_sha256(owner_path),
        }
        if not equal(_read(proof), expected_proof):
            raise CleanupStop("owned exact provider restoration proof differs")
    return {
        "owned": owned,
        "directory": directory,
        "native": native,
        "raw": raw,
        "canonical": canonical,
        "original": original,
        "store": store,
        "bindings": {
            "schema_version": VERSION,
            "session_id": identity,
            "raw_identity": bundle,
            "canonical_identity": revision,
            "native_sha256": file_sha256(native_path),
            "provider_receipt_sha256": file_sha256(proof),
        },
    }


def _prepare(context: dict[str, Any], paths: DetailedPaths) -> list[dict[str, Any]]:
    from downforce_core.detailed.corpus_ingest import _tables

    owned, native = context["owned"], context["native"]
    identity = native["session"]["session_id"]
    bundle, revision = (
        context["bindings"]["raw_identity"],
        context["bindings"]["canonical_identity"],
    )
    source = _path(paths.root, Path(native["source_directory"]).relative_to(paths.root).as_posix())
    if "frozen_bundle" in native:
        if source.relative_to(paths.root).parts[:2] != ("preflight", identity):
            raise CleanupStop("frozen source is not an immutable preflight source")
        targets = []
    else:
        source = _target(
            owned, identity, source.relative_to(owned.root).as_posix(), revision, bundle
        )
        if (
            re.fullmatch(r"attempt-[0-9a-f]{32}", source.name) is None
            or Path(native["raw_source"]) != source / "raw-cache"
        ):
            raise CleanupStop("current native source is not an exact owned attempt")
        targets = [source]
    _close_owned_cache(owned, targets)
    captured = _tables(source, native["tables"])
    if any(
        not table.equals(context["original"][name], check_metadata=True)
        for name, table in captured.items()
    ):
        raise CleanupStop("source/native canonical values differ before release")
    if source_manifest(Path(native["raw_source"])) != {
        key: context["raw"][key] for key in ("files", "directories")
    }:
        raise CleanupStop("retained raw source differs from lossless archive")
    snapshots = {}
    if targets:
        snapshot = _snapshot(source, owned)
        expected_files = {f"{name}.parquet" for name in native["tables"]} | {
            "raw-cache/" + record["path"] for record in context["raw"]["files"]
        }
        if {record["path"] for record in snapshot["files"]} != expected_files:
            raise CleanupStop("unregistered files in current native attempt")
        expected_directories = {
            "raw-cache",
            *("raw-cache/" + record["path"] for record in context["raw"]["directories"]),
        }
        expected_directories.update(
            str(parent).replace("\\", "/")
            for name in expected_files
            for parent in Path(name).parents
            if str(parent) != "."
        )
        if {record["path"] for record in snapshot["directories"]} != expected_directories:
            raise CleanupStop("unregistered directories in current native attempt")
        for record in snapshot["files"]:
            if record["path"].startswith("raw-cache/"):
                raw_record = {**record, "path": record["path"].removeprefix("raw-cache/")}
                if raw_record not in context["raw"]["files"]:
                    raise CleanupStop("raw source changed during release validation")
            else:
                descriptor = native["tables"][record["path"].removesuffix(".parquet")]
                if (
                    record["sha256"] != descriptor["sha256"]
                    or record["bytes"] != descriptor["bytes"]
                ):
                    raise CleanupStop("native table changed during release validation")
        snapshots[source] = snapshot
    restored = _target(owned, identity, f"staging/{identity}/restore-{bundle}", revision, bundle)
    restored_snapshot = _snapshot(restored, owned)
    if {key: restored_snapshot[key] for key in ("files", "directories")} != {
        key: context["raw"][key] for key in ("files", "directories")
    }:
        raise CleanupStop("exact retained raw restoration is unavailable")
    targets.append(restored)
    snapshots[restored] = restored_snapshot
    if "frozen_bundle" not in native:
        ownership = _read(_path(owned.root, f"staging/{identity}/offline-owner-{bundle}.json"))
        working = _target(owned, identity, ownership["path"], revision, bundle)
        _close_owned_cache(owned, [working])
        if ownership.get("raw_identity") != bundle or not equal(
            _snapshot(working, owned), ownership["snapshot"]
        ):
            raise CleanupStop("registered successful offline source changed")
        targets.append(working)
        snapshots[working] = ownership["snapshot"]
    candidate = _target(
        owned, identity, f"candidates/canonical/{identity}/revisions/{revision}", revision, bundle
    )
    if candidate.exists():
        duplicate = DetailedStore(paths.root, detailed_root=owned.root / "candidates").validate(
            identity, revision=revision
        )
        if not equal(duplicate, context["canonical"]):
            raise CleanupStop("owned canonical candidate is not the production duplicate")
        candidate_snapshot = _snapshot(candidate, owned)
        if candidate_snapshot["directories"] or {
            record["path"] for record in candidate_snapshot["files"]
        } != {"manifest.json", *(f"{name}.parquet" for name in duplicate["tables"])}:
            raise CleanupStop("unregistered files in owned canonical duplicate")
        for record in candidate_snapshot["files"]:
            production = context["store"].root / identity / "revisions" / revision / record["path"]
            if record["sha256"] != file_sha256(production):
                raise CleanupStop("candidate changed during release validation")
        targets.append(candidate)
        snapshots[candidate] = candidate_snapshot
    return [
        {"path": target.relative_to(owned.root).as_posix(), "snapshot": snapshots[target]}
        for target in targets
    ]


def _remaining(context: dict[str, Any], receipt: dict[str, Any]) -> list[Path]:
    owned = context["owned"]
    if receipt.get("state") not in {"PREPARED", "RELEASED"} or not equal(
        {key: receipt.get(key) for key in context["bindings"]}, context["bindings"]
    ):
        raise CleanupStop("owned release receipt binding differs")
    if receipt.get("targets_sha256") != _targets_digest(receipt["targets"]):
        raise CleanupStop("owned release target snapshot changed")
    identity = receipt["session_id"]
    native = context["native"]
    required = {f"staging/{identity}/restore-{receipt['raw_identity']}"}
    if "frozen_bundle" not in native:
        source = Path(native["source_directory"]).relative_to(owned.root).as_posix()
        if re.fullmatch(r"attempt-[0-9a-f]{32}", Path(source).name) is None:
            raise CleanupStop("registered native source is not a current owned attempt")
        ownership = _read(
            _path(owned.root, f"staging/{identity}/offline-owner-{receipt['raw_identity']}.json")
        )
        if (
            ownership["raw_identity"] != receipt["raw_identity"]
            or re.fullmatch(r"offline-[0-9a-f]{32}", Path(ownership["path"]).name) is None
        ):
            raise CleanupStop("offline registration differs from release identity")
        required.update((source, ownership["path"]))
    candidate = f"candidates/canonical/{identity}/revisions/{receipt['canonical_identity']}"
    recorded = [target["path"] for target in receipt["targets"]]
    if len(recorded) != len(set(recorded)) or set(recorded) not in (
        required,
        required | {candidate},
    ):
        raise CleanupStop("release targets differ from registered successful sources")
    if type(receipt.get("released_bytes")) is not int or receipt["released_bytes"] != sum(
        record["bytes"] for target in receipt["targets"] for record in target["snapshot"]["files"]
    ):
        raise CleanupStop("release byte accounting changed")
    targets = []
    for target in receipt["targets"]:
        path = _target(
            owned,
            receipt["session_id"],
            target["path"],
            receipt["canonical_identity"],
            receipt["raw_identity"],
        )
        if path.exists():
            actual = _snapshot(path, owned)
            expected = {record["path"]: record for record in target["snapshot"]["files"]}
            if any(record != expected.get(record["path"]) for record in actual["files"]):
                raise CleanupStop("remaining release target bytes or observations changed")
            directories = {record["path"]: record for record in target["snapshot"]["directories"]}
            missing = set(target["snapshot"]["objects"]) - set(actual["objects"])
            if (
                not equal(
                    {key: value for key, value in actual["root"].items() if key != "mtime_ns"},
                    {
                        key: value
                        for key, value in target["snapshot"]["root"].items()
                        if key != "mtime_ns"
                    },
                )
                or (
                    actual["root"]["mtime_ns"] != target["snapshot"]["root"]["mtime_ns"]
                    and not any(Path(name).parent.as_posix() == "." for name in missing)
                )
                or any(
                    record["path"] not in directories
                    or record["mode"] != directories[record["path"]]["mode"]
                    or (
                        record["mtime_ns"] != directories[record["path"]]["mtime_ns"]
                        and not any(
                            Path(name).parent.as_posix() == record["path"] for name in missing
                        )
                    )
                    for record in actual["directories"]
                )
                or any(
                    not equal(observed, target["snapshot"]["objects"].get(name))
                    for name, observed in actual["objects"].items()
                )
            ):
                raise CleanupStop("remaining release target directory or object identity changed")
            targets.append(path)
    return targets


def release_owned_session(project: Path, paths: DetailedPaths, identity: str) -> dict[str, Any]:
    """Call under the existing corpus worker lock. Never recursively remove a root."""
    try:
        context = _artifacts(project, paths, identity)
        receipt_path = context["directory"] / "release.json"
        if receipt_path.exists():
            receipt = _read(receipt_path)
        else:
            targets = _prepare(context, paths)
            receipt = {
                **context["bindings"],
                "state": "PREPARED",
                "targets": targets,
                "targets_sha256": _targets_digest(targets),
                "released_bytes": sum(
                    record["bytes"] for target in targets for record in target["snapshot"]["files"]
                ),
            }
            _save(receipt_path, receipt)
        remaining = _remaining(context, receipt)
        if receipt["state"] == "RELEASED" and remaining:
            raise CleanupStop("released target was recreated; it is not eligible for deletion")
        for target in remaining:
            context = _artifacts(project, paths, identity)
            _remaining(context, receipt)  # Immediate exact containment/tree/byte recheck.
            shutil.rmtree(target)
        context = _artifacts(project, paths, identity)
        if _remaining(context, receipt):
            raise CleanupStop("owned release targets remain after removal")
        receipt["state"] = "RELEASED"
        _save(receipt_path, receipt)
        return receipt
    except (OSError, StorageIntegrityError, KeyError, TypeError) as exc:
        if isinstance(exc, PermissionError):
            raise
        raise CleanupStop("owned release proof or path is unavailable") from exc


def resume_released(project: Path, paths: DetailedPaths, identity: str) -> dict[str, Any] | None:
    receipt_path = _path(paths.root, f"corpus/staging/{identity}/release.json")
    if not receipt_path.exists():
        return None
    context = _artifacts(project, paths, identity)
    receipt = _read(receipt_path)
    remaining = _remaining(context, receipt)
    if receipt["state"] == "RELEASED" and remaining:
        raise CleanupStop("released owned target was recreated")
    metadata = context["canonical"]["metadata"]
    return {
        "session_id": identity,
        "state": "complete",
        "raw_identity": receipt["raw_identity"],
        "canonical_identity": receipt["canonical_identity"],
        "quality": metadata["quality"],
        "availability": metadata["channel_availability"],
        "cache_release_state": receipt["state"],
    }
