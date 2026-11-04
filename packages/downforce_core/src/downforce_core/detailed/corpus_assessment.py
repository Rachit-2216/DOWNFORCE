"""Read-only, evidence-derived data-corpus coverage (not deployment or Step 9 QA)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downforce_core.detailed import corpus_openf1, study_runner
from downforce_core.detailed.corpus_openf1_authority import ProductionAuthority
from downforce_core.detailed.lifecycle import ORDER
from downforce_core.detailed.openf1 import _same
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import RawEvidenceStore
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path, _read
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import DownforceError
from downforce_core.storage.parquet import file_sha256

_ERRORS = (DownforceError, OSError, ValueError, KeyError, TypeError, AttributeError)


def _failure(identity: str, exc: Exception) -> dict[str, str]:
    return {"session_id": identity, "error": type(exc).__name__, "message": str(exc)}


def _entries(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = catalog["sessions"]
    if not isinstance(rows, list):
        raise ValueError("canonical session inventory must be an array")
    entries = {}
    for row in rows:
        identity = row["session_id"]
        validate_safe_identifier(identity)
        if identity in entries:
            raise ValueError("duplicate canonical session identities")
        entries[identity] = row
    return entries


def _job(root: Path, identity: str) -> dict[str, Any]:
    validate_safe_identifier(identity)
    job = _read(_path(root, f"corpus/jobs/{identity}.json"))
    if (
        job.get("state") not in (*ORDER, "FAILED")
        or job.get("session_id", identity) != identity
        or job.get("checkpoint", "DISCOVERED") not in ORDER
    ):
        raise ValueError("invalid detailed corpus checkpoint")
    return job


class _AssessmentGate:
    """Replay source authority and pin real historical bindings; never require free space."""

    def __init__(self, paths: DetailedPaths) -> None:
        self.paths = paths
        self.catalog, self.sources = study_runner.ready_inventory(paths.root)
        _entries(self.catalog)
        self.bindings = self._bindings()

    def _bindings(self) -> dict[str, str]:
        return {
            name: file_sha256(_path(self.paths.root, name))
            for name in (
                "reconciled-inventory.json",
                "study/sample-plan.json",
                "stratified-projection.json",
            )
        }

    def check(self, entry: dict[str, Any] | None = None, *, peak: int = 0) -> None:
        if self._bindings() != self.bindings:
            raise ValueError("current corpus source bindings changed during assessment")
        catalog, sources = study_runner.ready_inventory(self.paths.root)
        if not _same(catalog, self.catalog) or not _same(sources, self.sources):
            raise ValueError("current corpus source authority changed during assessment")
        if entry is not None and (
            [row for row in catalog["sessions"] if row["session_id"] == entry["session_id"]]
            != [entry]
            or entry.get("state") != "completed"
        ):
            raise ValueError("enrichment differs from current completed canonical entry")


def _targets(root: Path, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Count only unique completed mappings backed by an actual provider inventory."""
    providers = _read(_path(root, "openf1-inventory.json"))["sessions"]
    mappings = catalog["provider_entries"]
    if not isinstance(providers, list) or not isinstance(mappings, list):
        raise ValueError("OpenF1 inventory and mapping arrays are required")
    for rows in (providers, mappings):
        keys = [row["session_key"] for row in rows]
        if any(type(key) is not int or key <= 0 for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("duplicate or invalid OpenF1 provider keys")
    targets = []
    for entry in _entries(catalog).values():
        key = entry.get("openf1_session_key")
        if (
            entry.get("state") != "completed"
            or type(entry.get("season")) is not int
            or entry["season"] < 2023
        ):
            continue
        if type(key) is not int or key <= 0:
            raise ValueError("completed modern session lacks a valid OpenF1 mapping")
        matching = [row for row in mappings if row["session_key"] == key]
        originals = [row for row in providers if row["session_key"] == key]
        if (
            len(matching) != 1
            or len(originals) != 1
            or matching[0].get("canonical_session_id") != entry["session_id"]
            or matching[0].get("reconciliation_state") != "completed"
        ):
            raise ValueError("completed OpenF1 key lacks unique source-backed canonical mapping")
        targets.append(entry)
    return targets


def assess_corpus(project: Path, paths: DetailedPaths, *, validate: bool) -> dict[str, Any]:
    """Status is explicitly unverified; validate replays original immutable evidence."""
    authority: dict[str, Any] = {"verified": False, "failures": []}
    catalog: dict[str, Any] = {}
    expected: set[str] = set()
    try:
        catalog = _read(_path(paths.root, "reconciled-inventory.json"))
        expected = {
            identity
            for identity, row in _entries(catalog).items()
            if row.get("state") == "completed"
        }
    except _ERRORS as exc:
        authority["failures"].append(_failure("inventory", exc))

    jobs: list[dict[str, Any]] = []
    counts = {name: 0 for name in ("complete", "failed", "missing", "incomplete", "invalid")}
    observed: set[str] = set()
    try:
        directory = _path(paths.root, "corpus/jobs")
        if directory.exists():
            observed = {path.stem for path in directory.iterdir() if path.suffix == ".json"}
    except _ERRORS as exc:
        authority["failures"].append(_failure("jobs", exc))
    for identity in sorted(expected | observed):
        in_scope = identity in expected
        try:
            path = _path(paths.root, f"corpus/jobs/{identity}.json")
            if not path.exists():
                counts["missing"] += int(in_scope)
                continue
            job = _job(paths.root, identity)
            counts[
                "complete"
                if job["state"] == "COMPLETE"
                else "failed"
                if job["state"] == "FAILED"
                else "incomplete"
            ] += int(in_scope)
            jobs.append({**job, "session_id": identity, "expected": in_scope})
        except _ERRORS as exc:
            counts["invalid"] += int(in_scope)
            jobs.append({**_failure(identity, exc), "state": "INVALID", "expected": in_scope})

    store = DetailedStore(project, detailed_root=paths.root)
    sessions: list[dict[str, Any]] = []
    native_entries: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    identities = (
        sorted(path.name for path in paths.canonical.iterdir()) if paths.canonical.exists() else []
    )
    for identity in identities:
        try:
            manifest = (store.validate if validate else store.manifest)(identity)
            metadata = manifest["metadata"]
            if validate:
                raw = RawEvidenceStore(paths.raw).validate(metadata["raw_bundle_sha256"])
                job = _job(paths.root, identity)
                if (
                    raw["session_id"] != identity
                    or job["state"] != "COMPLETE"
                    or job["canonical_identity"] != manifest["revision"]
                    or job["raw_identity"] != metadata["raw_bundle_sha256"]
                ):
                    raise ValueError("publication does not match its completed corpus checkpoint")
            sessions.append(
                {
                    "session_id": identity,
                    "revision": manifest["revision"],
                    "quality": metadata["quality"],
                }
            )
            native_entries[identity] = metadata.get("session")
        except _ERRORS as exc:
            failures.append(_failure(identity, exc))

    enrichment: dict[str, Any] = {
        "target_sessions": 0,
        "completed_sessions": 0,
        "missing_sessions": 0,
        "partial_sessions": 0,
        "unverified_sessions": 0,
        "provider_stop": None,
        "failures": [],
        "verified": False,
    }
    gate: _AssessmentGate | None = None
    try:
        progress = _path(paths.root, "runtime/corpus-openf1-progress.json")
        if progress.exists():
            stop = _read(progress).get("stopped")
            if stop is not None:
                if not isinstance(stop, dict) or not all(
                    isinstance(stop.get(name), str) for name in ("session_id", "error")
                ):
                    raise ValueError("invalid persisted OpenF1 stop record")
                # Historical stop is a diagnostic, never endpoint availability or proof.
                enrichment["provider_stop"] = stop
    except _ERRORS as exc:
        enrichment["failures"].append(_failure("openf1-progress", exc))
    if validate:
        try:
            verified_catalog, _sources = study_runner.ready_inventory(paths.root)
            _entries(verified_catalog)
            if not _same(verified_catalog, catalog):
                raise ValueError("inventory changed during assessment")
            current = _entries(verified_catalog)
            for identity, native_entry in native_entries.items():
                if not _same(native_entry, current.get(identity)):
                    raise ValueError("native publication differs from current canonical identity")
            authority["verified"] = not authority["failures"]
        except _ERRORS as exc:
            authority["failures"].append(_failure("inventory", exc))
    try:
        targets = _targets(paths.root, catalog)
        enrichment["target_sessions"] = len(targets)
        for entry in targets:
            identity = entry["session_id"]
            try:
                directory = _path(paths.root, f"openf1/{identity}")
                if not directory.exists():
                    enrichment["missing_sessions"] += 1
                elif not validate or not authority["verified"]:
                    enrichment["unverified_sessions"] += 1
                else:
                    if gate is None:
                        gate = _AssessmentGate(paths)
                    admission = ProductionAuthority(project, gate, entry)
                    records, _ = corpus_openf1._inventory(directory, admission)
                    if (directory / "manifest.json").exists():
                        corpus_openf1._complete_manifest(directory, admission, records)
                        enrichment["completed_sessions"] += 1
                    else:
                        enrichment["partial_sessions"] += 1
            except _ERRORS as exc:
                enrichment["failures"].append(_failure(identity, exc))
        if gate is not None:
            gate.check()
        enrichment["verified"] = validate and authority["verified"] and not enrichment["failures"]
    except _ERRORS as exc:
        enrichment["failures"].append(_failure("openf1-inventory", exc))
    native_complete = (
        validate
        and bool(expected)
        and not failures
        and {row["session_id"] for row in sessions} == expected
    )
    enrichment_complete = (
        bool(enrichment["target_sessions"])
        and enrichment["verified"]
        and enrichment["completed_sessions"] == enrichment["target_sessions"]
    )
    complete = bool(native_complete and authority["verified"] and enrichment_complete)
    return {
        "completion_scope": "data-corpus-only",
        "published_sessions": len(sessions),
        "catalog_completed": len(expected),
        "expected_completed": len(expected),
        **{f"{name}_jobs": value for name, value in counts.items()},
        "unattempted_jobs": counts["missing"],
        "jobs": jobs,
        "sessions": sessions,
        "failures": failures,
        "verified": validate and not failures,
        "authority": authority,
        "native_corpus_complete": native_complete,
        "openf1": "complete" if enrichment_complete else "incomplete" if validate else "unverified",
        "openf1_assessment": enrichment,
        "enrichment_complete": bool(enrichment_complete),
        "corpus_complete": complete,
        "complete": complete,
    }
