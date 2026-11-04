"""Production-only historical entry admission; never weakens study admission."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from downforce_core.detailed.catalog import canonical_session_type
from downforce_core.detailed.corpus_gate import CorpusAuthorityStop
from downforce_core.detailed.enrichment_authority import _file, _historical_end
from downforce_core.detailed.openf1 import _same
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.detailed.raw_evidence import RawEvidenceStore
from downforce_core.detailed.storage import DetailedStore
from downforce_core.detailed.study_runner import _path, _read


def authority_now() -> datetime:
    return datetime.now(UTC)


class ProductionGate(Protocol):
    """Current source/binding authority, independent of ingestion capacity policy."""

    paths: DetailedPaths
    catalog: dict[str, Any]
    sources: dict[str, dict[str, Any]]
    bindings: dict[str, str]

    def check(self, entry: dict[str, Any] | None = None, *, peak: int = 0) -> None: ...


class ProductionAuthority:
    """Full global proof precedes admission; callbacks recheck real current authority.

    Native files are full byte-hash pinned, not trusted by mtime/size or saved flags.
    The owner must reconstruct CorpusGate at its public workflow completion.
    """

    def __init__(self, project: Path, gate: ProductionGate, entry: dict[str, Any]) -> None:
        self.gate, self.entry = gate, deepcopy(entry)
        self.root, self.identity = gate.paths.root, entry["session_id"]
        gate.check(self.entry)
        if type(entry.get("season")) is not int or entry["season"] < 2023:
            raise CorpusAuthorityStop("OpenF1 requires a supported completed-era session")
        key = entry.get("openf1_session_key")
        if type(key) is not int or key <= 0:
            raise CorpusAuthorityStop(
                "production enrichment requires a uniquely mapped provider key"
            )
        self.key = key
        providers = _read(_path(self.root, "openf1-inventory.json"))["sessions"]
        originals = [row for row in providers if row.get("session_key") == key]
        mappings = [
            row for row in gate.catalog["provider_entries"] if row.get("session_key") == key
        ]
        if len(originals) != 1 or len(mappings) != 1:
            raise CorpusAuthorityStop("provider source and mapping must be unique")
        original, mapping = originals[0], mappings[0]
        if (
            mapping.get("canonical_session_id") != self.identity
            or mapping.get("reconciliation_state") != "completed"
        ):
            raise CorpusAuthorityStop(
                "provider mapping is not bound to this completed canonical entry"
            )
        effective = original
        if entry.get("completion_temporal_sha256") is not None:
            proof = _read(_path(self.root, f"completion-temporal/{self.identity}.json"))
            effective = proof["provider_session"]
        provider_fields = {
            name: value
            for name, value in mapping.items()
            if name not in {"canonical_session_id", "reconciliation_state"}
        }
        if provider_fields != original and provider_fields != effective:
            raise CorpusAuthorityStop(
                "canonical provider mapping differs from its actual source proof"
            )
        if (
            original.get("year") != entry["season"]
            or canonical_session_type(entry["season"], str(original.get("session_name", "")))
            != entry["session_type"]
        ):
            raise CorpusAuthorityStop("provider era/category differs from canonical session")
        self.end = _historical_end(self.root, entry, original)
        start = datetime.fromisoformat(effective["date_start"].replace("Z", "+00:00"))
        if start.tzinfo is None or self.end.tzinfo is None or self.end <= start:
            raise CorpusAuthorityStop("provider historical dates are naive or inverted")
        source = gate.sources[self.identity]
        completion = _read(_path(self.root, f"completion/{self.identity}.json"))
        timing_path = source.get("timing_path", completion.get("timing_path"))
        if (
            not isinstance(timing_path, str)
            or not timing_path
            or ".." in Path(timing_path).parts
            or timing_path.startswith("/")
        ):
            raise CorpusAuthorityStop("canonical native timing path is unavailable or unsafe")
        self.native_api_path = "/static/" + timing_path
        self.store = DetailedStore(project, detailed_root=self.root)
        self.manifest = self.store.validate(self.identity)
        self.revision = self.manifest["revision"]
        metadata = self.manifest["metadata"]
        if not _same(metadata.get("session"), entry) or metadata.get("provider") != "fastf1":
            raise CorpusAuthorityStop(
                "production native revision differs from current canonical entry"
            )
        bundle = metadata.get("raw_bundle_sha256")
        if not isinstance(bundle, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle):
            raise CorpusAuthorityStop("native revision lacks bound original raw evidence")
        raw = RawEvidenceStore(gate.paths.raw).validate(bundle)
        if (
            raw["session_id"] != self.identity
            or raw["provider"] != "fastf1"
            or raw["provider_version"] != metadata.get("provider_version")
        ):
            raise CorpusAuthorityStop("native raw evidence differs from production revision")
        native = _path(self.root, f"canonical/{self.identity}/revisions/{self.revision}")
        self.native_directory = native
        self.native_members = {
            "manifest.json",
            *(f"{name}.parquet" for name in self.manifest["tables"]),
        }
        self.pins = {
            path: _file(path)
            for path in (
                _path(self.root, f"canonical/{self.identity}/active.json"),
                *(native / name for name in self.native_members),
                _path(self.root, f"raw/{bundle}.zip"),
                _path(self.root, "openf1-inventory.json"),
            )
        }
        self.clock = authority_now()
        self.binding = {
            "session": deepcopy(entry),
            "session_key": key,
            "native_revision": self.revision,
            "native_api_path": self.native_api_path,
            "raw_bundle_sha256": bundle,
            "authority_sha256": {
                path.relative_to(self.root).as_posix(): pin.sha256
                for path, pin in self.pins.items()
            },
            "historical_end": self.end.isoformat(),
            "storage_bindings": deepcopy(gate.bindings),
        }
        self.check()

    def check(self) -> None:
        self.gate.check(self.entry)
        now = authority_now()
        if now.tzinfo is None or now < self.clock:
            raise CorpusAuthorityStop("production enrichment authority clock moved backwards")
        if self.end + timedelta(minutes=30) >= now:
            raise CorpusAuthorityStop(
                "provider session is future or within the paid/live data window"
            )
        _unlinked_path(self.root)
        if {
            path.name for path in _unlinked_path(self.native_directory).iterdir()
        } != self.native_members:
            raise CorpusAuthorityStop("native revision member inventory changed")
        for path, expected in self.pins.items():
            if _file(path) != expected:
                raise CorpusAuthorityStop("production native/source authority bytes changed")
        self.clock = now

    def validate_binding(self, binding: dict[str, Any]) -> None:
        if json.dumps(binding, sort_keys=True, allow_nan=False) != json.dumps(
            self.binding, sort_keys=True, allow_nan=False
        ):
            raise CorpusAuthorityStop(
                "saved enrichment belongs to a different current native/source binding"
            )
        self.check()
