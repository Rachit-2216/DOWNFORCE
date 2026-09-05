"""Immutable aggregate and validation result types for canonical normalization."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import overload

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.domain.models import (
    DriverClassificationRecord,
    DriverRecord,
    LapRecord,
    PitStopRecord,
    RaceControlRecord,
    RacePositionRecord,
    SessionMetadata,
    SourceProvenance,
    StintRecord,
    TelemetryIndexRecord,
    TrackPositionRecord,
    WeatherRecord,
)
from downforce_core.domain.time import ensure_utc
from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    SessionRef,
)


class ValidationLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


def _required_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a nonempty, trimmed string")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    level: ValidationLevel
    code: str
    table: str
    message: str
    row_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.level, ValidationLevel):
            raise TypeError("level must be a ValidationLevel")
        for field_name in ("code", "table", "message"):
            _required_text(getattr(self, field_name), field_name)
        if self.row_key is not None:
            _required_text(self.row_key, "row_key")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.issues, tuple):
            raise TypeError("issues must be an immutable tuple")
        if any(not isinstance(issue, ValidationIssue) for issue in self.issues):
            raise TypeError("issues must contain only ValidationIssue values")

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level is ValidationLevel.ERROR)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level is ValidationLevel.WARNING)

    @property
    def is_valid(self) -> bool:
        return not self.errors


_TRACK_POSITION_COLUMNS = (
    "driver_id",
    "session_time_ms",
    "x_m",
    "y_m",
    "z_m",
    "raw_status",
)


def _empty_track_position_table() -> pa.Table:
    return pa.table(
        {
            "driver_id": pa.array([], type=pa.string()),
            "session_time_ms": pa.array([], type=pa.int64()),
            "x_m": pa.array([], type=pa.float64()),
            "y_m": pa.array([], type=pa.float64()),
            "z_m": pa.array([], type=pa.float64()),
            "raw_status": pa.array([], type=pa.string()),
        }
    )


def _string_value_type(value: pa.DataType) -> pa.DataType:
    return value.value_type if pa.types.is_dictionary(value) else value


@dataclass(frozen=True, slots=True, eq=False)
class CanonicalTrackPositions:
    """Dense canonical track positions backed by immutable Arrow columns.

    Per-sample dataclasses and provenance hashes are created only through bounded lazy
    iteration. The canonical table is the scalable storage/API boundary.
    """

    session_id: SessionId
    table: pa.Table
    provider_name: str
    provider_version: str
    retrieved_at: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, SessionId):
            raise TypeError("session_id must be a SessionId")
        if not isinstance(self.table, pa.Table):
            raise TypeError("track position table must be a pyarrow.Table")
        if tuple(self.table.column_names) != _TRACK_POSITION_COLUMNS:
            raise ValueError("track position table has an invalid canonical schema")
        driver_type = _string_value_type(self.table.schema.field("driver_id").type)
        status_type = _string_value_type(self.table.schema.field("raw_status").type)
        if not pa.types.is_string(driver_type) or not pa.types.is_string(status_type):
            raise TypeError("track position driver/status columns must contain strings")
        expected_types = {
            "session_time_ms": pa.int64(),
            "x_m": pa.float64(),
            "y_m": pa.float64(),
            "z_m": pa.float64(),
        }
        for name, expected in expected_types.items():
            if self.table.schema.field(name).type != expected:
                raise TypeError(f"track position {name} must have type {expected}")
        for name in ("driver_id", "session_time_ms", "x_m", "y_m"):
            if self.table.column(name).null_count:
                raise ValueError(f"track position {name} must not contain nulls")
        _required_text(self.provider_name, "provider_name")
        _required_text(self.provider_version, "provider_version")
        _required_text(self.source, "source")
        object.__setattr__(
            self,
            "retrieved_at",
            ensure_utc(self.retrieved_at, field_name="retrieved_at"),
        )

    @classmethod
    def empty(
        cls,
        *,
        session_id: SessionId,
        provider_name: str,
        provider_version: str,
        retrieved_at: datetime,
        source: str,
    ) -> CanonicalTrackPositions:
        return cls(
            session_id=session_id,
            table=_empty_track_position_table(),
            provider_name=provider_name,
            provider_version=provider_version,
            retrieved_at=retrieved_at,
            source=source,
        )

    def __len__(self) -> int:
        return int(self.table.num_rows)

    @property
    def nbytes(self) -> int:
        return int(self.table.nbytes)

    @overload
    def __getitem__(self, index: int) -> TrackPositionRecord: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[TrackPositionRecord, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> TrackPositionRecord | tuple[TrackPositionRecord, ...]:
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))
        normalized = index + len(self) if index < 0 else index
        if normalized < 0 or normalized >= len(self):
            raise IndexError("track position index out of range")
        return self._record_at(normalized)

    def __iter__(self) -> Iterator[TrackPositionRecord]:
        return self.iter_records()

    def iter_records(self, *, batch_size: int = 1_024) -> Iterator[TrackPositionRecord]:
        """Yield bounded lazy record views for compatibility-oriented consumers."""

        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        for batch in self.table.to_batches(max_chunksize=batch_size):
            for index in range(batch.num_rows):
                yield self._record_from_columns(batch, index)

    def _record_at(self, index: int) -> TrackPositionRecord:
        return self._record_from_columns(self.table.slice(index, 1), 0)

    def _record_from_columns(
        self,
        source: pa.RecordBatch | pa.Table,
        index: int,
    ) -> TrackPositionRecord:
        driver_id = DriverId(source["driver_id"][index].as_py())
        session_time_ms = int(source["session_time_ms"][index].as_py())
        x_m = float(source["x_m"][index].as_py())
        y_m = float(source["y_m"][index].as_py())
        raw_z = source["z_m"][index].as_py()
        raw_status = source["raw_status"][index].as_py()
        row_identity = "\x1f".join(
            (
                str(driver_id),
                str(session_time_ms),
                x_m.hex(),
                y_m.hex(),
                "" if raw_z is None else float(raw_z).hex(),
                "" if raw_status is None else str(raw_status),
            )
        )
        return TrackPositionRecord(
            session_id=self.session_id,
            driver_id=driver_id,
            session_time_ms=session_time_ms,
            x_m=x_m,
            y_m=y_m,
            z_m=None if raw_z is None else float(raw_z),
            raw_status=None if raw_status is None else str(raw_status),
            provenance=SourceProvenance(
                provider=self.provider_name,
                provider_version=self.provider_version,
                source=self.source,
                retrieved_at=self.retrieved_at,
                source_record_id=sha256(row_identity.encode("utf-8")).hexdigest(),
            ),
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple) and not other:
            return len(self) == 0
        if not isinstance(other, CanonicalTrackPositions):
            return False
        return (
            self.session_id == other.session_id
            and self.provider_name == other.provider_name
            and self.provider_version == other.provider_version
            and self.retrieved_at == other.retrieved_at
            and self.source == other.source
            and self.table.equals(other.table)
        )


def _freeze_value(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} must be finite")
        return value
    if isinstance(value, datetime):
        return ensure_utc(value, field_name=path)
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"{path} keys must be nonempty strings")
            frozen[key] = _freeze_value(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(child, path=f"{path}[]") for child in value)
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class NormalizedSession:
    """One provider-free canonical session snapshot; telemetry payloads remain lazy."""

    metadata: SessionMetadata
    drivers: tuple[DriverRecord, ...]
    classifications: tuple[DriverClassificationRecord, ...]
    laps: tuple[LapRecord, ...]
    stints: tuple[StintRecord, ...]
    pit_stops: tuple[PitStopRecord, ...]
    weather: tuple[WeatherRecord, ...]
    race_control: tuple[RaceControlRecord, ...]
    race_positions: tuple[RacePositionRecord, ...]
    track_positions: CanonicalTrackPositions
    telemetry_index: tuple[TelemetryIndexRecord, ...]
    capabilities: ProviderCapabilities
    completeness: Mapping[DatasetName, DatasetAvailability]
    warnings: tuple[str, ...]
    provider_name: str
    provider_version: str
    retrieved_at: datetime
    provider_metadata: Mapping[str, object]
    requested_session: SessionRef
    validation_report: ValidationReport = field(default_factory=ValidationReport)
    telemetry_materialized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, SessionMetadata):
            raise TypeError("metadata must be SessionMetadata")
        record_fields = {
            "drivers": DriverRecord,
            "classifications": DriverClassificationRecord,
            "laps": LapRecord,
            "stints": StintRecord,
            "pit_stops": PitStopRecord,
            "weather": WeatherRecord,
            "race_control": RaceControlRecord,
            "race_positions": RacePositionRecord,
            "telemetry_index": TelemetryIndexRecord,
        }
        for field_name, record_type in record_fields.items():
            records = getattr(self, field_name)
            if not isinstance(records, tuple):
                raise TypeError(f"{field_name} must be an immutable tuple")
            if any(not isinstance(record, record_type) for record in records):
                raise TypeError(f"{field_name} contains an invalid record")
        if self.track_positions == ():
            object.__setattr__(
                self,
                "track_positions",
                CanonicalTrackPositions.empty(
                    session_id=self.metadata.session_id,
                    provider_name=self.provider_name,
                    provider_version=self.provider_version,
                    retrieved_at=self.retrieved_at,
                    source=f"{self.provider_name}.track-positions",
                ),
            )
        elif not isinstance(self.track_positions, CanonicalTrackPositions):
            raise TypeError("track_positions must be CanonicalTrackPositions")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise TypeError("capabilities must be ProviderCapabilities")
        if not isinstance(self.completeness, Mapping):
            raise TypeError("completeness must be a mapping")
        completeness = dict(self.completeness)
        if set(completeness) != set(DatasetName):
            raise ValueError("completeness must include every DatasetName")
        if any(not isinstance(state, DatasetAvailability) for state in completeness.values()):
            raise TypeError("completeness values must be DatasetAvailability values")
        object.__setattr__(self, "completeness", MappingProxyType(completeness))
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) or not warning.strip() for warning in self.warnings
        ):
            raise TypeError("warnings must be an immutable tuple of nonempty strings")
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))
        _required_text(self.provider_name, "provider_name")
        _required_text(self.provider_version, "provider_version")
        object.__setattr__(
            self,
            "retrieved_at",
            ensure_utc(self.retrieved_at, field_name="retrieved_at"),
        )
        if not isinstance(self.provider_metadata, Mapping):
            raise TypeError("provider_metadata must be a mapping")
        frozen_metadata = _freeze_value(self.provider_metadata, path="provider_metadata")
        if not isinstance(frozen_metadata, Mapping):
            raise TypeError("provider_metadata must be a mapping")
        object.__setattr__(self, "provider_metadata", frozen_metadata)
        if not isinstance(self.requested_session, SessionRef):
            raise TypeError("requested_session must be SessionRef")
        if not isinstance(self.validation_report, ValidationReport):
            raise TypeError("validation_report must be ValidationReport")
        if type(self.telemetry_materialized) is not bool:
            raise TypeError("telemetry_materialized must be a bool")
        if self.telemetry_materialized:
            raise ValueError("canonical car telemetry is index metadata only")


# Both names are exported while callers migrate toward the more explicit canonical wording.
CanonicalSession = NormalizedSession


__all__ = [
    "CanonicalTrackPositions",
    "CanonicalSession",
    "NormalizedSession",
    "ValidationIssue",
    "ValidationLevel",
    "ValidationReport",
]
