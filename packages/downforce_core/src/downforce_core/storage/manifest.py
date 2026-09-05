"""Strict, deterministic JSON manifests for immutable canonical datasets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import cast

from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.domain.time import ensure_utc
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.schemas import CanonicalTableName

MANIFEST_VERSION = "1.0.0"

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise StorageIntegrityError(f"{field_name} must be a nonempty trimmed string")
    return value


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageIntegrityError(f"{field_name} must be an integer or null")
    return value


def _freeze_json(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise StorageIntegrityError(f"{path} contains a nonfinite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise StorageIntegrityError(f"{path} contains an invalid object key")
            result[key] = _freeze_json(child, path=f"{path}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(child, path=f"{path}[{index}]") for index, child in enumerate(value)
        )
    raise StorageIntegrityError(f"{path} contains unsupported {type(value).__name__}")


def _thaw_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(child) for child in value]
    raise TypeError(f"cannot encode manifest value {type(value).__name__}")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StorageIntegrityError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise StorageIntegrityError(f"{field_name} keys must be strings")
    return cast(Mapping[str, object], value)


@dataclass(frozen=True, slots=True)
class TableArtifact:
    name: CanonicalTableName
    source_dataset: str
    availability: str
    materialized: bool
    path: str | None
    row_count: int
    sha256: str | None
    schema_fingerprint: str
    min_session_time_ms: int | None = None
    max_session_time_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, CanonicalTableName):
            raise StorageIntegrityError("table name is invalid")
        _required_text(self.source_dataset, "source_dataset")
        _required_text(self.availability, "availability")
        if type(self.materialized) is not bool:
            raise StorageIntegrityError("materialized must be a boolean")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int):
            raise StorageIntegrityError("row_count must be an integer")
        if self.row_count < 0:
            raise StorageIntegrityError("row_count must be nonnegative")
        _required_text(self.schema_fingerprint, "schema_fingerprint")
        if self.materialized:
            _required_text(self.path, "path")
            _required_text(self.sha256, "sha256")
        elif self.path is not None or self.sha256 is not None or self.row_count != 0:
            raise StorageIntegrityError("non-materialized table must not claim a file or rows")
        if (
            self.min_session_time_ms is not None
            and self.max_session_time_ms is not None
            and self.max_session_time_ms < self.min_session_time_ms
        ):
            raise StorageIntegrityError("table maximum time precedes its minimum")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name.value,
            "source_dataset": self.source_dataset,
            "availability": self.availability,
            "materialized": self.materialized,
            "path": self.path,
            "row_count": self.row_count,
            "sha256": self.sha256,
            "schema_fingerprint": self.schema_fingerprint,
            "min_session_time_ms": self.min_session_time_ms,
            "max_session_time_ms": self.max_session_time_ms,
        }

    @classmethod
    def from_dict(cls, value: object) -> TableArtifact:
        data = _mapping(value, "table artifact")
        try:
            return cls(
                name=CanonicalTableName(_required_text(data.get("name"), "table.name")),
                source_dataset=_required_text(data.get("source_dataset"), "table.source_dataset"),
                availability=_required_text(data.get("availability"), "table.availability"),
                materialized=cast(bool, data.get("materialized")),
                path=cast(str | None, data.get("path")),
                row_count=cast(int, data.get("row_count")),
                sha256=cast(str | None, data.get("sha256")),
                schema_fingerprint=_required_text(
                    data.get("schema_fingerprint"), "table.schema_fingerprint"
                ),
                min_session_time_ms=_optional_int(
                    data.get("min_session_time_ms"), "table.min_session_time_ms"
                ),
                max_session_time_ms=_optional_int(
                    data.get("max_session_time_ms"), "table.max_session_time_ms"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise StorageIntegrityError("table artifact is malformed") from exc


@dataclass(frozen=True, slots=True)
class SessionManifest:
    dataset_id: str
    snapshot_id: str
    session_id: str
    canonical_schema_version: str
    normalization_version: str
    storage_format_version: str
    created_at_utc: datetime
    session: Mapping[str, object]
    provider: Mapping[str, object]
    software: Mapping[str, object]
    capabilities: Mapping[str, object]
    completeness: Mapping[str, object]
    tables: Mapping[str, TableArtifact]
    warnings: tuple[str, ...]
    validation_issues: tuple[Mapping[str, object], ...]
    knowledge_time: Mapping[str, object]
    provider_metadata: Mapping[str, object]
    requested_session: Mapping[str, object]
    timeline_version: str | None = None
    replay_version: str | None = None
    telemetry_materialized: bool = False
    manifest_version: str = MANIFEST_VERSION
    status: str = "complete"

    def __post_init__(self) -> None:
        for field_name in (
            "dataset_id",
            "snapshot_id",
            "session_id",
            "canonical_schema_version",
            "normalization_version",
            "storage_format_version",
            "manifest_version",
            "status",
        ):
            _required_text(getattr(self, field_name), field_name)
        validate_safe_identifier(self.dataset_id, field_name="dataset_id")
        validate_safe_identifier(self.snapshot_id, field_name="snapshot_id")
        validate_safe_identifier(self.session_id, field_name="session_id")
        if self.status != "complete":
            raise StorageIntegrityError("only complete manifests are readable")
        for field_name in ("timeline_version", "replay_version"):
            value = getattr(self, field_name)
            if value is not None:
                _required_text(value, field_name)
        object.__setattr__(
            self,
            "created_at_utc",
            ensure_utc(self.created_at_utc, field_name="created_at_utc"),
        )
        for field_name in (
            "session",
            "provider",
            "software",
            "capabilities",
            "completeness",
            "knowledge_time",
            "provider_metadata",
            "requested_session",
        ):
            frozen = _freeze_json(getattr(self, field_name), path=field_name)
            if not isinstance(frozen, Mapping):
                raise StorageIntegrityError(f"{field_name} must be an object")
            object.__setattr__(self, field_name, frozen)
        copied_tables = dict(self.tables)
        if set(copied_tables) != {name.value for name in CanonicalTableName}:
            raise StorageIntegrityError("manifest must describe every canonical table")
        if any(key != artifact.name.value for key, artifact in copied_tables.items()):
            raise StorageIntegrityError("table artifact key does not match its name")
        object.__setattr__(self, "tables", MappingProxyType(copied_tables))
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.warnings
        ):
            raise StorageIntegrityError("warnings must be nonempty strings")
        frozen_issues: list[Mapping[str, object]] = []
        for index, issue in enumerate(self.validation_issues):
            frozen = _freeze_json(issue, path=f"validation_issues[{index}]")
            if not isinstance(frozen, Mapping):
                raise StorageIntegrityError("validation issue must be an object")
            frozen_issues.append(frozen)
        object.__setattr__(self, "validation_issues", tuple(frozen_issues))
        if type(self.telemetry_materialized) is not bool or self.telemetry_materialized:
            raise StorageIntegrityError("Step 2 telemetry must remain index-only")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "manifest_version": self.manifest_version,
            "status": self.status,
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "canonical_schema_version": self.canonical_schema_version,
            "normalization_version": self.normalization_version,
            "timeline_version": self.timeline_version,
            "replay_version": self.replay_version,
            "storage_format_version": self.storage_format_version,
            "created_at_utc": self.created_at_utc.isoformat().replace("+00:00", "Z"),
            "session": _thaw_json(self.session),
            "provider": _thaw_json(self.provider),
            "software": _thaw_json(self.software),
            "capabilities": _thaw_json(self.capabilities),
            "completeness": _thaw_json(self.completeness),
            "tables": {name: artifact.to_dict() for name, artifact in sorted(self.tables.items())},
            "warnings": list(self.warnings),
            "validation_issues": [_thaw_json(issue) for issue in self.validation_issues],
            "knowledge_time": _thaw_json(self.knowledge_time),
            "telemetry_materialized": self.telemetry_materialized,
            "provider_metadata": _thaw_json(self.provider_metadata),
            "requested_session": _thaw_json(self.requested_session),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict()) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SessionManifest:
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StorageIntegrityError("manifest is not valid JSON") from exc
        data = _mapping(raw, "manifest")
        tables_raw = _mapping(data.get("tables"), "manifest.tables")
        try:
            created = datetime.fromisoformat(
                _required_text(data.get("created_at_utc"), "created_at_utc").replace("Z", "+00:00")
            )
            warnings_raw = data.get("warnings")
            issues_raw = data.get("validation_issues")
            if not isinstance(warnings_raw, list) or not isinstance(issues_raw, list):
                raise StorageIntegrityError("manifest warnings/issues must be arrays")
            return cls(
                manifest_version=_required_text(data.get("manifest_version"), "manifest_version"),
                status=_required_text(data.get("status"), "status"),
                dataset_id=_required_text(data.get("dataset_id"), "dataset_id"),
                snapshot_id=_required_text(data.get("snapshot_id"), "snapshot_id"),
                session_id=_required_text(data.get("session_id"), "session_id"),
                canonical_schema_version=_required_text(
                    data.get("canonical_schema_version"), "canonical_schema_version"
                ),
                normalization_version=_required_text(
                    data.get("normalization_version"), "normalization_version"
                ),
                timeline_version=cast(str | None, data.get("timeline_version")),
                replay_version=cast(str | None, data.get("replay_version")),
                storage_format_version=_required_text(
                    data.get("storage_format_version"), "storage_format_version"
                ),
                created_at_utc=created,
                session=_mapping(data.get("session"), "session"),
                provider=_mapping(data.get("provider"), "provider"),
                software=_mapping(data.get("software"), "software"),
                capabilities=_mapping(data.get("capabilities"), "capabilities"),
                completeness=_mapping(data.get("completeness"), "completeness"),
                tables={name: TableArtifact.from_dict(value) for name, value in tables_raw.items()},
                warnings=tuple(cast(list[str], warnings_raw)),
                validation_issues=tuple(
                    _mapping(value, "validation issue") for value in issues_raw
                ),
                knowledge_time=_mapping(data.get("knowledge_time"), "knowledge_time"),
                telemetry_materialized=cast(bool, data.get("telemetry_materialized")),
                provider_metadata=_mapping(data.get("provider_metadata"), "provider_metadata"),
                requested_session=_mapping(data.get("requested_session"), "requested_session"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, StorageIntegrityError):
                raise
            raise StorageIntegrityError("manifest is malformed") from exc


__all__ = [
    "MANIFEST_VERSION",
    "JsonValue",
    "SessionManifest",
    "TableArtifact",
    "canonical_json",
]
