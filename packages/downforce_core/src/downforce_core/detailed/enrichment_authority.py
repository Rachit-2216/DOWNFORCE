"""Private operation-local byte receipts for fully replayed historical authority.

The receipt is not readiness evidence itself. Its only owner admits through the
public validator and repeats that validator before a successful operation ends.
Intervening gates retain full byte hashing unless Windows kernel read leases
exclude writes/deletion for every present input throughout this one operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from downforce_core.detailed.enrichment_paths import (
    default_evidence_path,
    evidence_path,
)
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.domain.identifiers import validate_safe_identifier

if TYPE_CHECKING:
    from downforce_core.detailed.input_leases import _LeaseSet

_SOURCE_TREES = (
    "completion",
    "completion-derived",
    "completion-finalization",
    "completion-temporal",
    "archive-finalization",
    "evidence-requests",
)
_SIDECARS = ("", "-wal", "-shm", "-journal")


def _owned(root: Path, relative: str) -> Path:
    token = Path(relative)
    if ".." in token.parts or not relative or ":" in relative and not token.is_absolute():
        raise ValueError(f"enrichment authority dependency escaped dedicated root: {relative}")
    path = (root / token).absolute()
    if not path.is_relative_to(root):
        raise ValueError("enrichment authority dependency escaped dedicated root")
    return path


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@dataclass(frozen=True)
class _File:
    identity: tuple[int, ...] | None
    sha256: str | None


def _file(path: Path, *, ancestors: bool = True) -> _File:
    if ancestors:
        _unlinked_path(path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _File(None, None)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("enrichment authority input must be a regular unlinked file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        # Windows Python's fstat ctime can describe last-write time whereas path
        # stat ctime describes creation. Compare ctime using path stat below;
        # handle dev/inode/size/mtime must still identify the exact opened file.
        if _identity(os.fstat(handle.fileno()))[:-1] != _identity(before)[:-1]:
            raise ValueError("enrichment authority input changed while opening")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        if _identity(os.fstat(handle.fileno()))[:-1] != _identity(before)[:-1]:
            raise ValueError("enrichment authority input changed while hashing")
    if _identity(path.stat(follow_symlinks=False)) != _identity(before):
        raise ValueError("enrichment authority input changed while hashing")
    return _File(_identity(before), digest.hexdigest())


@dataclass(frozen=True)
class _Directory:
    identity: tuple[int, ...] | None
    members: tuple[str, ...]
    # Root/study/runtime are mutable containers, not authority bytes. Only their
    # relevant membership and device/inode/type are bound; source trees are exact.
    mutable: bool = False
    excluded: frozenset[str] = frozenset()
    prefix: str | None = None


def _directory(
    path: Path,
    *,
    mutable: bool = False,
    excluded: frozenset[str] = frozenset(),
    prefix: str | None = None,
    ancestors: bool = True,
) -> _Directory:
    if ancestors:
        _unlinked_path(path)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _Directory(None, (), mutable, excluded, prefix)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("enrichment authority directory is not a directory")
    if getattr(before, "st_file_attributes", 0) & 0x400:
        raise ValueError("linked enrichment authority directory")
    members = []
    for child in path.iterdir():
        if child.name in excluded or prefix is not None and not child.name.startswith(prefix):
            continue
        if ancestors and (child.is_symlink() or child.is_junction()):
            raise ValueError("linked enrichment authority member")
        members.append(child.name)
    after = path.stat(follow_symlinks=False)
    identity = (*_identity(before), getattr(before, "st_file_attributes", 0))
    if mutable:
        identity = identity[:3]
        changed = identity != _identity(after)[:3]
    else:
        changed = identity != (*_identity(after), getattr(after, "st_file_attributes", 0))
    if changed:
        raise ValueError("enrichment authority directory changed while enumerating")
    return _Directory(identity, tuple(sorted(members)), mutable, excluded, prefix)


class _Inputs:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, _File] = {}
        self.directories: dict[str, _Directory] = {}
        self.documents: dict[str, Any] = {}

    def directory(
        self,
        relative: str,
        *,
        mutable: bool = False,
        excluded: frozenset[str] = frozenset(),
        prefix: str | None = None,
    ) -> _Directory:
        path = self.root if relative == "." else _owned(self.root, relative)
        value = _directory(path, mutable=mutable, excluded=excluded, prefix=prefix)
        self.directories[relative] = value
        return value

    def file(self, relative: str, *, document: bool = False) -> None:
        if relative in self.files:
            return
        path = _owned(self.root, relative)
        value = _file(path)
        self.files[relative] = value
        if document and value.identity is not None:
            raw = path.read_bytes()
            if (
                hashlib.sha256(raw).hexdigest() != value.sha256
                or _identity(path.stat(follow_symlinks=False)) != value.identity
            ):
                raise ValueError("enrichment authority document changed while reading")
            self.documents[relative] = json.loads(raw)

    def tree(self, relative: str, *, documents: bool = False) -> None:
        if relative in self.directories:
            return
        members = self.directory(relative)
        for name in members.members:
            child = f"{relative}/{name}"
            path = _owned(self.root, child)
            if path.is_dir():
                self.tree(child, documents=documents)
            else:
                self.file(child, document=documents and path.suffix == ".json")

    def sqlite(self, relative: str) -> None:
        for suffix in _SIDECARS:
            self.file(relative + suffix)

    def references(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self.references(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"dependencies", "historical_403_records"} and isinstance(item, dict):
                    for relative in item:
                        self.file(relative, document=relative.endswith(".json"))
                elif (
                    key == "path"
                    or key.endswith("_path")
                    and key not in {"timing_path", "api_path"}
                ) and isinstance(item, str):
                    path = _owned(self.root, item)
                    item = path.relative_to(self.root).as_posix()
                    if path.is_dir():
                        self.tree(item, documents=True)
                    else:
                        self.file(item, document=path.suffix == ".json")
                        # Native corroboration and captured bodies have relevant
                        # sibling membership even when only one file is consumed.
                        parent = path.parent.relative_to(self.root).as_posix()
                        if parent not in self.directories:
                            self.directory(parent)
                    if path.name.endswith(".sqlite"):
                        self.sqlite(item)
                self.references(item)


def _collect(root: Path, identity: str, preservation_inputs: tuple[str, ...] = ()) -> _Inputs:
    inputs = _Inputs(root)
    inputs.directory(".", mutable=True, excluded=frozenset({"runtime", "study"}))
    inputs.directory("study", mutable=True, excluded=frozenset({"openf1"}))
    for relative in ("inventory.json", "openf1-inventory.json", "reconciled-inventory.json"):
        inputs.file(relative, document=True)
    for relative in _SOURCE_TREES:
        inputs.tree(relative, documents=True)
    for relative in ("discovery-cache", "reconciliation-cache"):
        inputs.directory(relative)
        inputs.sqlite(f"{relative}/fastf1_http_cache.sqlite")
    # Budgets, pending intents and locks retain their own transport validators.
    runtime = inputs.directory("runtime", mutable=True, prefix="official-index-retry-")
    for name in runtime.members:
        inputs.file(f"runtime/{name}", document=True)
    inputs.file("study/sample-plan.json", document=True)
    inputs.file("study/acquisition-journal.json", document=True)
    for relative in ("study/inventory-snapshots",):
        inputs.tree(relative, documents=True)
    for relative in ("study/frozen", "study/provider-restore"):
        inputs.directory(relative)
        inputs.file(f"{relative}/{identity}.json")
    inputs.file(f"study/session-measurements/{identity}.json", document=True)
    inputs.file(f"study/compression/{identity}-deflate-6-metrics-v2.json")
    # All possible fallback sample ownership/ambiguity inputs, but not the 39
    # sessions' unrelated native tables or expanded FastF1 pickle caches.
    preflight = inputs.directory("preflight")
    for name in preflight.members:
        session_path = f"preflight/{name}"
        path = _owned(root, session_path)
        if not path.is_dir():
            inputs.file(session_path)
            continue
        members = inputs.directory(session_path)
        inputs.file(f"{session_path}/measurement.json", document=True)
        for attempt_name in members.members:
            attempt = f"{session_path}/{attempt_name}"
            if not _owned(root, attempt).is_dir():
                continue
            inputs.directory(attempt)
            inputs.file(f"{attempt}/measurement.json", document=True)
            cache = f"{attempt}/raw-cache"
            inputs.directory(cache)
            inputs.sqlite(f"{cache}/fastf1_http_cache.sqlite")
    # Only this operation's native/raw/candidate bytes are fully replayed by
    # _candidate. Include all members, not just manifest-listed tables.
    target = f"preflight/{identity}"
    # Expand despite metadata directory enumeration above.
    for relative in list(inputs.directories):
        if relative == target or relative.startswith(target + "/"):
            del inputs.directories[relative]
    inputs.tree(target)
    inputs.tree(f"study/candidates/canonical/{identity}")
    report = inputs.documents.get(f"study/session-measurements/{identity}.json", {})
    session = {"session_id": identity}
    selected_evidence = evidence_path(session, report)
    preserved = set(preservation_inputs)
    if selected_evidence != default_evidence_path(session):
        preserved.add(default_evidence_path(session))
    if preserved:
        # Only the separately owned fresh output namespace may grow. The old
        # leaf itself, including every legacy byte and directory, is immutable.
        inputs.directory("study/openf1", mutable=True, excluded=frozenset({"reverified"}))
        for relative in sorted(preserved):
            if relative != default_evidence_path(session):
                raise ValueError("enrichment preservation input must be the original session leaf")
            path = _owned(root, relative)
            _unlinked_path(path)
            if not path.is_dir():
                raise ValueError(
                    "original enrichment evidence must remain present for reverification"
                )
            inputs.tree(relative)
    bundle = report.get("raw_bundle_sha256")
    if isinstance(bundle, str):
        inputs.directory("study/raw-bundles")
        inputs.file(f"study/raw-bundles/{bundle}.zip")
    # Only source proof documents grant dependency closure. Native/frozen sample
    # metadata for OTHER sessions is consumed for ownership comparison, not as
    # authority to load their expanded cache directories.
    scanned = {
        relative
        for relative in inputs.documents
        if relative.startswith("preflight/") or relative.startswith("study/")
    }
    while remaining := set(inputs.documents) - scanned:
        for relative in sorted(remaining):
            inputs.references(inputs.documents[relative])
            scanned.add(relative)
    # A receipt check validates every ancestor once before hashing and again
    # afterwards, instead of repeating the identical ancestor walk per file.
    # No path is trusted by stat alone: every regular input is still byte-hashed.
    for relative in list(inputs.files) + list(inputs.directories):
        parent = Path(relative).parent
        while parent != Path("."):
            name = parent.as_posix()
            if name not in inputs.directories:
                inputs.directory(name)
            parent = parent.parent
    return inputs


def _historical_end(root: Path, entry: dict[str, Any], provider: dict[str, Any]) -> datetime:
    """Use the latest fully bound date without changing the original provider row."""
    end = datetime.fromisoformat(provider["date_end"].replace("Z", "+00:00"))
    marker = entry.get("completion_temporal_sha256")
    if marker is not None:
        path = _unlinked_path(_owned(root, f"completion-temporal/{entry['session_id']}.json"))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != marker:
            raise ValueError("historical temporal provider date proof checksum differs")
        proof = json.loads(raw)
        actual = datetime.fromisoformat(
            proof["provider_session"]["date_end"].replace("Z", "+00:00")
        )
        if actual.tzinfo is None:
            raise ValueError("historical temporal provider end is naive")
        end = max(end, actual)
    return end


class _Admission:
    """Created afresh inside one operation, with no global or caller bypass state."""

    def __init__(
        self,
        root: Path,
        session: dict[str, Any],
        *,
        preservation_inputs: tuple[str, ...] = (),
    ):
        self.root = _unlinked_path(root)
        self.session = session
        self.session_identity = json.dumps(session, sort_keys=True, allow_nan=False)
        self.identity = validate_safe_identifier(session["session_id"])
        self._windows = os.name == "nt"
        self._leases: _LeaseSet | None = None
        self._entered = False
        self._used = False
        self._preservation_inputs = preservation_inputs

    def __enter__(self) -> Self:
        if self._used:
            raise ValueError("historical admission is operation-local and cannot be reused")
        self._used = True
        try:
            self._admit()
        except BaseException as exc:
            self._close(exc)
            raise
        return self

    def _admit(self) -> None:
        from downforce_core.detailed import openf1
        from downforce_core.detailed.input_leases import _LeaseSet

        self.inputs = _collect(self.root, self.identity, self._preservation_inputs)
        if self._windows:
            self._leases = _LeaseSet(
                self.root,
                {
                    relative: (value.identity, self._digest(value))
                    for relative, value in self.inputs.files.items()
                    if value.identity is not None
                },
                {
                    relative: value.identity
                    for relative, value in self.inputs.directories.items()
                    if value.identity is not None
                },
            )
        self.clock = openf1._authority_now()
        openf1.validated_historical_session(self.root, self.session)
        self._same_collection()
        catalog = self.inputs.documents["reconciled-inventory.json"]
        self.deadlines = [
            datetime.fromisoformat(row["scheduled_at"])
            for row in catalog["sessions"]
            if row["state"] == "future"
        ] + [
            datetime.fromisoformat(row["date_start"].replace("Z", "+00:00"))
            for row in catalog["provider_entries"]
            if row.get("canonical_session_id") is None and row["reconciliation_state"] == "future"
        ]
        providers = self.inputs.documents["openf1-inventory.json"]["sessions"]
        provider = next(
            row for row in providers if row["session_key"] == self.session["openf1_session_key"]
        )
        self.end = _historical_end(self.root, self.session, provider)
        self._entered = True
        self.check()

    @staticmethod
    def _digest(value: _File) -> str:
        digest = value.sha256
        if digest is None or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("held input lease requires its complete initial byte hash receipt")
        return digest

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close(exc)

    def _close(self, primary: BaseException | None = None) -> None:
        self._entered = False
        if self._leases is not None:
            self._leases.close(primary)

    def _same_collection(self) -> None:
        current = _collect(self.root, self.identity, self._preservation_inputs)
        if current.files != self.inputs.files or current.directories != self.inputs.directories:
            raise ValueError("enrichment authority inputs changed during full validation")

    def check(self) -> None:
        from downforce_core.detailed import openf1

        if not self._entered:
            raise ValueError("historical admission requires its active operation context")
        if json.dumps(self.session, sort_keys=True, allow_nan=False) != self.session_identity:
            raise ValueError("enrichment session differs from admitted canonical identity")
        now = openf1._authority_now()
        if now < self.clock:
            raise ValueError("enrichment authority clock moved backwards")
        if any(now > deadline for deadline in self.deadlines):
            raise ValueError("enrichment canonical future exclusion expired")
        if self.end + timedelta(minutes=30) >= now:
            raise ValueError("provider session is future or within the paid/live data window")
        _unlinked_path(self.root)
        self._directories()
        if self._windows:
            if self._leases is None:
                raise ValueError("Windows admission requires complete held read leases")
            self._leases.complete(
                {name for name, value in self.inputs.files.items() if value.identity is not None},
                {
                    name
                    for name, value in self.inputs.directories.items()
                    if value.identity is not None
                },
            )
        for relative, expected_file in self.inputs.files.items():
            if self._windows and expected_file.identity is not None:
                assert self._leases is not None
                self._leases.file(relative, expected_file.identity, self._digest(expected_file))
            elif self._windows:
                try:
                    _owned(self.root, relative).stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise ValueError(f"enrichment authority input changed: {relative}")
            elif _file(_owned(self.root, relative), ancestors=False) != expected_file:
                raise ValueError(f"enrichment authority input changed: {relative}")
        self._directories()
        self.clock = now  # Only a fully successful gate advances the rollback floor.

    def _directories(self) -> None:
        for relative, expected in self.inputs.directories.items():
            path = self.root if relative == "." else _owned(self.root, relative)
            current = _directory(
                path,
                mutable=expected.mutable,
                excluded=expected.excluded,
                prefix=expected.prefix,
                ancestors=False,
            )
            # Runtime may first appear for own budgets/lock; those are deliberately
            # outside immutable authority. Its historical retry membership is not.
            if relative == "runtime" and expected.identity is None and not current.members:
                if current.identity is not None:
                    if self._windows:
                        if self._leases is None:
                            raise ValueError("runtime binding requires held read leases")
                        self._leases.bind_directory(relative, current.identity)
                    self.inputs.directories[relative] = current
                continue
            if current != expected:
                raise ValueError(f"enrichment authority directory changed: {relative}")
            if self._windows and current.identity is not None:
                if self._leases is None:
                    raise ValueError("authority directory requires its held read lease")
                self._leases.directory(relative)

    def finish(self) -> None:
        from downforce_core.detailed import openf1

        self.check()
        self._same_collection()
        openf1.validated_historical_session(self.root, self.session)
        self._same_collection()
        self.check()
