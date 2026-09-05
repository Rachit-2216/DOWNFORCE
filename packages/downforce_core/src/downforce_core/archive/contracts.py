"""Typed contracts for the capability-aware DOWNFORCE historical archive."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import cast


class ArchiveEventStatus(StrEnum):
    COMPLETED = "completed"
    UPCOMING = "upcoming"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class SyncLifecycle(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    FETCHING = "fetching"
    NORMALIZING = "normalizing"
    VALIDATING = "validating"
    WRITING = "writing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class QualityStatus(StrEnum):
    VERIFIED = "verified"
    GOOD = "good"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNUSABLE = "unusable"


class CapabilityTier(StrEnum):
    ARCHIVE = "archive"
    LAP_DATA = "lap_data"
    LAP_AND_PIT = "lap_and_pit"
    DETAILED_TIMING = "detailed_timing"
    TELEMETRY = "telemetry"
    FULL_DOWNFORCE = "full_downforce"


@dataclass(frozen=True, slots=True)
class RaceDataCapabilities:
    """Observed data capabilities; every flag must be backed by stored evidence."""

    results: bool = False
    grid: bool = False
    lap_times: bool = False
    lap_positions: bool = False
    pit_stops: bool = False
    stints: bool = False
    compounds: bool = False
    weather: bool = False
    race_control: bool = False
    track_positions: bool = False
    telemetry: bool = False
    speed: bool = False
    throttle: bool = False
    brake: bool = False
    gear: bool = False
    rpm: bool = False
    drs: bool = False
    ml_intelligence: bool = False
    strategy_simulation: bool = False
    counterfactual_support: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {field.name: cast(bool, getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RaceDataCapabilities:
        return cls(**{field.name: bool(value.get(field.name, False)) for field in fields(cls)})

    @property
    def tier(self) -> CapabilityTier:
        if (
            self.telemetry
            and self.track_positions
            and self.weather
            and self.race_control
            and self.compounds
            and self.ml_intelligence
            and self.strategy_simulation
        ):
            return CapabilityTier.FULL_DOWNFORCE
        if self.telemetry or self.track_positions:
            return CapabilityTier.TELEMETRY
        if self.weather or self.race_control or self.stints or self.compounds:
            return CapabilityTier.DETAILED_TIMING
        if (self.lap_times or self.lap_positions) and self.pit_stops:
            return CapabilityTier.LAP_AND_PIT
        if self.lap_times or self.lap_positions:
            return CapabilityTier.LAP_DATA
        return CapabilityTier.ARCHIVE


@dataclass(frozen=True, slots=True)
class ProviderProvenance:
    provider: str
    provider_version: str
    source: str
    source_url: str
    retrieved_at_utc: str
    raw_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "source": self.source,
            "source_url": self.source_url,
            "retrieved_at_utc": self.retrieved_at_utc,
            "raw_sha256": self.raw_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ProviderProvenance:
        return cls(**{name: str(value[name]) for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class DataQuality:
    status: QualityStatus
    reasons: tuple[str, ...]
    metrics: dict[str, int | float | str | bool | None]
    validated_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "validated_at_utc": self.validated_at_utc,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DataQuality:
        reasons = value.get("reasons", [])
        metrics = value.get("metrics", {})
        return cls(
            status=QualityStatus(str(value["status"])),
            reasons=tuple(str(item) for item in cast(list[object], reasons)),
            metrics=cast(dict[str, int | float | str | bool | None], metrics),
            validated_at_utc=str(value["validated_at_utc"]),
        )


@dataclass(frozen=True, slots=True)
class HistoricalSession:
    session_id: str
    session_type: str
    status: ArchiveEventStatus
    sync_status: SyncLifecycle
    capabilities: RaceDataCapabilities
    quality: DataQuality
    provenance: tuple[ProviderProvenance, ...]
    row_counts: dict[str, int]
    data_revision: str | None = None
    legacy_session_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "status": self.status.value,
            "sync_status": self.sync_status.value,
            "capability_tier": self.capabilities.tier.value,
            "capabilities": self.capabilities.to_dict(),
            "quality": self.quality.to_dict(),
            "provenance": [item.to_dict() for item in self.provenance],
            "row_counts": dict(self.row_counts),
            "data_revision": self.data_revision,
            "legacy_session_id": self.legacy_session_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> HistoricalSession:
        return cls(
            session_id=str(value["session_id"]),
            session_type=str(value["session_type"]),
            status=ArchiveEventStatus(str(value["status"])),
            sync_status=SyncLifecycle(str(value["sync_status"])),
            capabilities=RaceDataCapabilities.from_dict(
                cast(dict[str, object], value["capabilities"])
            ),
            quality=DataQuality.from_dict(cast(dict[str, object], value["quality"])),
            provenance=tuple(
                ProviderProvenance.from_dict(cast(dict[str, object], item))
                for item in cast(list[object], value.get("provenance", []))
            ),
            row_counts={
                str(name): int(str(count))
                for name, count in cast(dict[str, object], value.get("row_counts", {})).items()
            },
            data_revision=cast(str | None, value.get("data_revision")),
            legacy_session_id=cast(str | None, value.get("legacy_session_id")),
        )


@dataclass(frozen=True, slots=True)
class HistoricalEvent:
    event_id: str
    season: int
    round_number: int
    name: str
    official_name: str
    event_date: str
    circuit_name: str
    locality: str | None
    country: str | None
    country_code: str | None
    status: ArchiveEventStatus
    sessions: tuple[HistoricalSession, ...]
    drivers: tuple[str, ...]
    teams: tuple[str, ...]

    @property
    def race_session(self) -> HistoricalSession:
        return self.sessions[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "season": self.season,
            "round_number": self.round_number,
            "name": self.name,
            "official_name": self.official_name,
            "event_date": self.event_date,
            "circuit_name": self.circuit_name,
            "locality": self.locality,
            "country": self.country,
            "country_code": self.country_code,
            "status": self.status.value,
            "sessions": [item.to_dict() for item in self.sessions],
            "drivers": list(self.drivers),
            "teams": list(self.teams),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> HistoricalEvent:
        return cls(
            event_id=str(value["event_id"]),
            season=int(str(value["season"])),
            round_number=int(str(value["round_number"])),
            name=str(value["name"]),
            official_name=str(value["official_name"]),
            event_date=str(value["event_date"]),
            circuit_name=str(value["circuit_name"]),
            locality=cast(str | None, value.get("locality")),
            country=cast(str | None, value.get("country")),
            country_code=cast(str | None, value.get("country_code")),
            status=ArchiveEventStatus(str(value["status"])),
            sessions=tuple(
                HistoricalSession.from_dict(cast(dict[str, object], item))
                for item in cast(list[object], value["sessions"])
            ),
            drivers=tuple(str(item) for item in cast(list[object], value.get("drivers", []))),
            teams=tuple(str(item) for item in cast(list[object], value.get("teams", []))),
        )


@dataclass(frozen=True, slots=True)
class HistoricalSeason:
    year: int
    events: tuple[HistoricalEvent, ...]

    @property
    def completed_event_count(self) -> int:
        return sum(item.status is ArchiveEventStatus.COMPLETED for item in self.events)

    def to_dict(self) -> dict[str, object]:
        return {
            "year": self.year,
            "event_count": len(self.events),
            "completed_event_count": self.completed_event_count,
            "events": [item.to_dict() for item in self.events],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> HistoricalSeason:
        return cls(
            year=int(str(value["year"])),
            events=tuple(
                HistoricalEvent.from_dict(cast(dict[str, object], item))
                for item in cast(list[object], value["events"])
            ),
        )


@dataclass(frozen=True, slots=True)
class HistoricalCatalog:
    catalog_version: str
    generated_at_utc: str
    archive_start_year: int
    latest_completed_event_id: str | None
    latest_completed_event_date: str | None
    source_revision: str
    seasons: tuple[HistoricalSeason, ...]

    @property
    def events(self) -> tuple[HistoricalEvent, ...]:
        return tuple(event for season in self.seasons for event in season.events)

    def to_dict(self) -> dict[str, object]:
        events = self.events
        completed = [item for item in events if item.status is ArchiveEventStatus.COMPLETED]
        return {
            "catalog_version": self.catalog_version,
            "generated_at_utc": self.generated_at_utc,
            "archive_start_year": self.archive_start_year,
            "latest_completed_event_id": self.latest_completed_event_id,
            "latest_completed_event_date": self.latest_completed_event_date,
            "source_revision": self.source_revision,
            "season_count": len(self.seasons),
            "event_count": len(events),
            "completed_event_count": len(completed),
            "seasons": [item.to_dict() for item in self.seasons],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> HistoricalCatalog:
        return cls(
            catalog_version=str(value["catalog_version"]),
            generated_at_utc=str(value["generated_at_utc"]),
            archive_start_year=int(str(value["archive_start_year"])),
            latest_completed_event_id=cast(str | None, value.get("latest_completed_event_id")),
            latest_completed_event_date=cast(str | None, value.get("latest_completed_event_date")),
            source_revision=str(value["source_revision"]),
            seasons=tuple(
                HistoricalSeason.from_dict(cast(dict[str, object], item))
                for item in cast(list[object], value["seasons"])
            ),
        )


__all__ = [
    "ArchiveEventStatus",
    "CapabilityTier",
    "DataQuality",
    "HistoricalCatalog",
    "HistoricalEvent",
    "HistoricalSeason",
    "HistoricalSession",
    "ProviderProvenance",
    "QualityStatus",
    "RaceDataCapabilities",
    "SyncLifecycle",
]
