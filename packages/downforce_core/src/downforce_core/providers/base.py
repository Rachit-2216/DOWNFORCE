"""Provider-neutral asynchronous session-loading contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Protocol

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.enums import SessionType
from downforce_core.domain.identifiers import SessionId, make_session_id
from downforce_core.domain.time import ensure_utc


class DatasetName(StrEnum):
    DRIVERS = "drivers"
    LAPS = "laps"
    WEATHER = "weather"
    RACE_CONTROL = "race-control"
    RACE_POSITIONS = "race-positions"
    TRACK_POSITIONS = "track-positions"
    CAR_TELEMETRY = "car-telemetry"


class DatasetAvailability(StrEnum):
    AVAILABLE = "available"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    NOT_REQUESTED = "not-requested"
    ERROR = "error"


@dataclass(frozen=True, slots=True, init=False)
class SessionRef:
    """Validated selector with raw lookup values and canonical cache identity."""

    season: int = field(compare=False)
    event: int | str = field(compare=False)
    session: SessionType = field(compare=False)
    session_id: SessionId = field(init=False, repr=False)

    def __init__(self, season: int, event: int | str, session: str | SessionType) -> None:
        if isinstance(session, SessionType):
            parsed = session
        elif isinstance(session, str):
            if not session or session != session.strip():
                raise ValueError(
                    "session must be a nonempty code or name without surrounding whitespace"
                )
            parsed = SessionType.from_raw(session)
        else:
            raise TypeError("session must be a supported session code or name")
        if parsed is SessionType.UNKNOWN:
            raise ValueError("session must be a supported session code or name")
        # ID construction validates season, event selector and generated path safety.
        session_id = make_session_id(season, event, parsed)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "session", parsed)
        object.__setattr__(self, "session_id", session_id)

    @property
    def session_type(self) -> SessionType:
        return self.session


@dataclass(frozen=True, slots=True)
class LoadOptions:
    """Explicit datasets and cache behavior requested from a provider."""

    datasets: frozenset[DatasetName] = field(default_factory=lambda: frozenset(DatasetName))
    force_refresh: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.datasets, frozenset):
            raise TypeError("datasets must be a frozenset")
        if any(not isinstance(dataset, DatasetName) for dataset in self.datasets):
            raise TypeError("datasets must contain only DatasetName values")
        if type(self.force_refresh) is not bool:
            raise TypeError("force_refresh must be a bool")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Provider feature declaration; no capability is inferred from returned data."""

    drivers: bool
    laps: bool
    weather: bool
    race_control: bool
    race_positions: bool
    track_positions: bool
    car_telemetry: bool
    live: bool

    def __post_init__(self) -> None:
        for capability in fields(self):
            if type(getattr(self, capability.name)) is not bool:
                raise TypeError(f"{capability.name} must be a bool")

    def supports(self, dataset: DatasetName) -> bool:
        if not isinstance(dataset, DatasetName):
            raise TypeError("dataset must be a DatasetName")
        return {
            DatasetName.DRIVERS: self.drivers,
            DatasetName.LAPS: self.laps,
            DatasetName.WEATHER: self.weather,
            DatasetName.RACE_CONTROL: self.race_control,
            DatasetName.RACE_POSITIONS: self.race_positions,
            DatasetName.TRACK_POSITIONS: self.track_positions,
            DatasetName.CAR_TELEMETRY: self.car_telemetry,
        }[dataset]


@dataclass(frozen=True, slots=True)
class ProviderTable:
    """One owned Arrow table and its explicit provider availability state.

    Construction performs one Arrow IPC roundtrip at the adapter boundary. That deliberate
    copy prevents mutable NumPy or pandas buffers owned by an adapter from changing the raw
    provider snapshot after it crosses into core.
    """

    name: DatasetName
    availability: DatasetAvailability
    data: pa.Table | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, DatasetName):
            raise TypeError("name must be a DatasetName")
        if not isinstance(self.availability, DatasetAvailability):
            raise TypeError("availability must be a DatasetAvailability")
        if self.data is not None and not isinstance(self.data, pa.Table):
            raise TypeError("provider data must be an immutable pyarrow.Table")
        if self.error is not None:
            if not isinstance(self.error, str):
                raise TypeError("error must be a string or None")
            if not self.error.strip() or self.error != self.error.strip():
                raise ValueError("error must be a nonempty, trimmed message")

        if self.availability is DatasetAvailability.AVAILABLE:
            if self.data is None or self.data.num_rows == 0:
                raise ValueError("AVAILABLE requires a nonempty pyarrow.Table")
            if self.error is not None:
                raise ValueError("AVAILABLE must not include an error")
        elif self.availability is DatasetAvailability.EMPTY:
            if self.data is None or self.data.num_rows != 0:
                raise ValueError("EMPTY requires an empty pyarrow.Table")
            if self.error is not None:
                raise ValueError("EMPTY must not include an error")
        elif self.availability is DatasetAvailability.ERROR:
            if self.data is not None:
                raise ValueError("ERROR must not include data")
            if self.error is None:
                raise ValueError("ERROR requires an actionable error message")
        elif self.data is not None or self.error is not None:
            raise ValueError(
                f"{self.availability.value} must not include provider data or an error"
            )
        if self.data is not None:
            object.__setattr__(self, "data", _copy_arrow_table(self.data))


def _copy_arrow_table(table: pa.Table) -> pa.Table:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    with pa.ipc.open_stream(sink.getvalue()) as reader:
        return reader.read_all()


def _freeze_metadata(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"metadata value at {path} must be finite")
        return value
    if isinstance(value, datetime):
        return ensure_utc(value, field_name=f"metadata value at {path}")
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"metadata key at {path} must be a nonempty string")
            frozen[key] = _freeze_metadata(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_metadata(child, path=f"{path}[{index}]") for index, child in enumerate(value)
        )
    raise TypeError(
        f"metadata value at {path} has unsupported type {type(value).__name__}; "
        "provider-specific objects are forbidden"
    )


type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def _metadata_to_json_value(value: object, *, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"metadata value at {path} must be finite")
        return value
    if isinstance(value, datetime):
        normalized = ensure_utc(value, field_name=f"metadata value at {path}")
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        thawed: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"metadata key at {path} must be a nonempty string")
            thawed[key] = _metadata_to_json_value(child, path=f"{path}.{key}")
        return thawed
    if isinstance(value, (list, tuple)):
        return [
            _metadata_to_json_value(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    raise TypeError(
        f"metadata value at {path} has unsupported type {type(value).__name__}; "
        "provider-specific and filesystem path objects are forbidden"
    )


def thaw_provider_metadata(metadata: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return an independent JSON-compatible tree from frozen provider metadata."""

    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")
    thawed = _metadata_to_json_value(metadata, path="metadata")
    if not isinstance(thawed, dict):
        raise TypeError("metadata must be a mapping")
    return thawed


def encode_provider_metadata(metadata: Mapping[str, object]) -> str:
    """Encode provider metadata as deterministic JSON without filesystem access."""

    return json.dumps(
        thaw_provider_metadata(metadata),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class ProviderSession:
    """Complete immutable raw boundary returned by a provider adapter."""

    session: SessionRef
    provider_name: str
    provider_version: str
    retrieved_at: datetime
    metadata: Mapping[str, object]
    tables: Mapping[DatasetName, ProviderTable]

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionRef):
            raise TypeError("session must be a SessionRef")
        for field_name in ("provider_name", "provider_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} must be a nonempty, trimmed string")
        object.__setattr__(
            self,
            "retrieved_at",
            ensure_utc(self.retrieved_at, field_name="retrieved_at"),
        )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        frozen_metadata = _freeze_metadata(self.metadata, path="metadata")
        if not isinstance(frozen_metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", frozen_metadata)

        if not isinstance(self.tables, Mapping):
            raise TypeError("tables must be a mapping")
        copied_tables: dict[DatasetName, ProviderTable] = {}
        for name, table in self.tables.items():
            if not isinstance(name, DatasetName):
                raise TypeError("table keys must be DatasetName values")
            if not isinstance(table, ProviderTable):
                raise TypeError("table values must be ProviderTable values")
            if table.name is not name:
                raise ValueError("table mapping key must match ProviderTable.name")
            copied_tables[name] = table
        if set(copied_tables) != set(DatasetName):
            raise ValueError("tables must state availability for every dataset")
        object.__setattr__(self, "tables", MappingProxyType(copied_tables))

    def table(self, name: DatasetName) -> ProviderTable:
        """Return an explicitly classified provider table."""

        if not isinstance(name, DatasetName):
            raise TypeError("name must be a DatasetName")
        return self.tables[name]


class RaceDataProvider(Protocol):
    """Asynchronous port implemented by isolated provider adapters."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def load_session(
        self, session: SessionRef, options: LoadOptions | None = None
    ) -> ProviderSession: ...


__all__ = [
    "DatasetAvailability",
    "DatasetName",
    "LoadOptions",
    "ProviderCapabilities",
    "ProviderSession",
    "ProviderTable",
    "RaceDataProvider",
    "SessionRef",
    "encode_provider_metadata",
    "thaw_provider_metadata",
]
