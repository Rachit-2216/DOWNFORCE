"""Resumable, failure-isolated historical archive synchronization."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.archive.contracts import (
    ArchiveEventStatus,
    DataQuality,
    HistoricalCatalog,
    HistoricalEvent,
    HistoricalSeason,
    HistoricalSession,
    ProviderProvenance,
    QualityStatus,
    RaceDataCapabilities,
    SyncLifecycle,
)
from downforce_core.archive.jolpica import (
    JOLPICA_BASE_URL,
    JOLPICA_PROVIDER_VERSION,
    ArchiveSourceRace,
    JolpicaClient,
    JolpicaDumpReader,
    completed_by_date,
)
from downforce_core.archive.quality import evaluate_archive_race
from downforce_core.archive.schemas import ARCHIVE_SCHEMAS, CATALOG_VERSION, ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.ml.artifacts import ArtifactStore, ArtifactUnavailableError
from downforce_core.storage.repository import DownforceRepository
from downforce_core.storage.schemas import CanonicalTableName


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _event_id(season: int, round_number: int) -> str:
    return f"event-{season}-round-{round_number:02d}"


def _session_key(session_id: str) -> tuple[int, int]:
    match = re.fullmatch(r"archive-(\d{4})-round-(\d{2})-race", session_id)
    if match is None:
        raise ValueError("archive session ID is invalid")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True, slots=True)
class ArchiveSyncResult:
    catalog: HistoricalCatalog
    completed: int
    upcoming: int
    cancelled: int
    failed: int
    cache_hits: int
    written: int
    source_revision: str
    provider_source_revision: str
    storage: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog": {
                "season_count": len(self.catalog.seasons),
                "event_count": len(self.catalog.events),
                "latest_completed_event_id": self.catalog.latest_completed_event_id,
                "latest_completed_event_date": self.catalog.latest_completed_event_date,
            },
            "completed": self.completed,
            "upcoming": self.upcoming,
            "cancelled": self.cancelled,
            "failed": self.failed,
            "cache_hits": self.cache_hits,
            "written": self.written,
            "source_revision": self.source_revision,
            "provider_source_revision": self.provider_source_revision,
            "storage": self.storage,
        }


def _upcoming_quality(timestamp: str) -> DataQuality:
    return DataQuality(
        status=QualityStatus.PARTIAL,
        reasons=("upcoming_event_not_ingested",),
        metrics={"result_rows": 0, "lap_rows": 0, "pit_stop_rows": 0},
        validated_at_utc=timestamp,
    )


def _mixed_catalog_source_revision(events: list[HistoricalEvent]) -> str:
    payload = [
        {
            "event_id": event.event_id,
            "status": event.status.value,
            "session_id": event.race_session.session_id,
            "data_revision": event.race_session.data_revision,
            "provenance": sorted(item.raw_sha256 for item in event.race_session.provenance),
        }
        for event in sorted(events, key=lambda item: (item.season, item.round_number))
    ]
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"archive-mixed-source-sha256-{digest}"


def _cancelled_quality(timestamp: str) -> DataQuality:
    return DataQuality(
        status=QualityStatus.PARTIAL,
        reasons=("cancelled_event_not_ingested",),
        metrics={"result_rows": 0, "lap_rows": 0, "pit_stop_rows": 0},
        validated_at_utc=timestamp,
    )


class HistoricalArchiveSync:
    def __init__(
        self,
        project_root: Path,
        *,
        store: HistoricalArchiveStore | None = None,
        client: JolpicaClient | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store or HistoricalArchiveStore(self.project_root)
        self.client = client or JolpicaClient(self.store)

    def _detailed_sessions(
        self,
    ) -> dict[tuple[int, int], tuple[str, RaceDataCapabilities, ProviderProvenance]]:
        repository = DownforceRepository(self.project_root)
        ml_sessions, ml_circuits = self._ml_support()
        detailed: dict[tuple[int, int], tuple[str, RaceDataCapabilities, ProviderProvenance]] = {}
        for summary in repository.list_sessions():
            manifest = repository.load_manifest(summary.session_id)
            session = manifest.session
            season = int(str(session["season"]))
            round_number = int(str(session["round_number"]))
            tables = manifest.tables
            row_counts = {
                name: tables[name.value].row_count
                for name in CanonicalTableName
                if name.value in tables
            }

            def rows(
                name: CanonicalTableName,
                counts: dict[CanonicalTableName, int] = row_counts,
            ) -> int:
                return counts.get(name, 0)

            channels: set[str] = set()
            if rows(CanonicalTableName.TELEMETRY_INDEX):
                index_table = repository.load_table(
                    summary.session_id, CanonicalTableName.TELEMETRY_INDEX
                )
                for value in index_table.column("channel_names").to_pylist():
                    channels.update(str(item).casefold() for item in cast(list[object], value))
            circuit_name = str(session.get("circuit_name") or "")
            ml_available = summary.session_id in ml_sessions and circuit_name in ml_circuits
            capabilities = RaceDataCapabilities(
                results=rows(CanonicalTableName.DRIVER_CLASSIFICATIONS) > 0,
                grid=True,
                lap_times=rows(CanonicalTableName.LAPS) > 0,
                lap_positions=rows(CanonicalTableName.RACE_POSITIONS) > 0,
                pit_stops=rows(CanonicalTableName.PIT_STOPS) > 0,
                stints=rows(CanonicalTableName.STINTS) > 0,
                compounds=rows(CanonicalTableName.STINTS) > 0,
                weather=rows(CanonicalTableName.WEATHER) > 0,
                race_control=rows(CanonicalTableName.RACE_CONTROL) > 0,
                track_positions=rows(CanonicalTableName.TRACK_POSITIONS) > 0,
                telemetry=rows(CanonicalTableName.TELEMETRY_INDEX) > 0,
                speed="speed" in channels,
                throttle="throttle" in channels,
                brake="brake" in channels,
                gear=bool({"ngear", "gear"} & channels),
                rpm="rpm" in channels,
                drs="drs" in channels,
                ml_intelligence=ml_available,
                strategy_simulation=ml_available,
                counterfactual_support=ml_available,
            )
            dataset_digest = summary.dataset_id.rsplit("-", maxsplit=1)[-1]
            provenance = ProviderProvenance(
                provider=summary.provider,
                provider_version=str(manifest.provider.get("version", "unknown")),
                source="downforce-canonical-v1",
                source_url=f"local://canonical/{summary.session_id}",
                retrieved_at_utc=summary.created_at_utc.isoformat().replace("+00:00", "Z"),
                raw_sha256=dataset_digest,
            )
            detailed[(season, round_number)] = (
                summary.session_id,
                capabilities,
                provenance,
            )
        return detailed

    def _ml_support(self) -> tuple[set[str], set[str]]:
        path = self.project_root / "docs" / "ml" / "benchmark-corpus.json"
        if not path.is_file():
            return set(), set()
        payload = cast(dict[str, object], json.loads(path.read_text("utf-8")))
        sessions = {
            str(cast(dict[str, object], item)["session_id"])
            for item in cast(list[object], payload.get("sessions", []))
        }
        try:
            bundle = ArtifactStore(self.project_root).load()
            pit = cast(dict[str, object], bundle["pit_loss"])
            supported = pit["supported_circuits"]
            if not isinstance(supported, list) or any(
                not isinstance(item, str) for item in supported
            ):
                raise ArtifactUnavailableError("pit circuit support is malformed")
            circuits = set(cast(list[str], supported))
        except (ArtifactUnavailableError, KeyError, TypeError, ValueError):
            return set(), set()
        return sessions, circuits

    def _current_schedule_race(
        self, item: dict[str, object], *, provenance_hash: str
    ) -> ArchiveSourceRace:
        season = int(str(item["season"]))
        round_number = int(str(item["round"]))
        circuit = cast(dict[str, object], item.get("Circuit", {}))
        location = cast(dict[str, object], circuit.get("Location", {}))
        empty = {
            table_name: pa.Table.from_pylist([], schema=schema)
            for table_name, schema in ARCHIVE_SCHEMAS.items()
        }
        provenance = ProviderProvenance(
            provider="jolpica",
            provider_version=JOLPICA_PROVIDER_VERSION,
            source="jolpica-classic-schedule",
            source_url=f"{JOLPICA_BASE_URL}/ergast/f1/{season}",
            retrieved_at_utc=_utc_now(),
            raw_sha256=provenance_hash,
        )
        return ArchiveSourceRace(
            season=season,
            round_number=round_number,
            name=str(item["raceName"]),
            event_date=str(item["date"]),
            circuit_name=str(circuit.get("circuitName", "Unknown circuit")),
            locality=str(location["locality"]) if location.get("locality") else None,
            country=str(location["country"]) if location.get("country") else None,
            country_code=None,
            results=empty[ArchiveTableName.RESULTS],
            laps=empty[ArchiveTableName.LAPS],
            pit_stops=empty[ArchiveTableName.PIT_STOPS],
            drivers=(),
            teams=(),
            provenance=provenance,
            cancelled=str(item.get("is_cancelled", "false")).casefold() in {"true", "1", "yes"},
        )

    @staticmethod
    def _completed_event_from_manifest(manifest: dict[str, object]) -> HistoricalEvent:
        capabilities = RaceDataCapabilities.from_dict(
            cast(dict[str, object], manifest["capabilities"])
        )
        quality = DataQuality.from_dict(cast(dict[str, object], manifest["quality"]))
        provenance = tuple(
            ProviderProvenance.from_dict(cast(dict[str, object], item))
            for item in cast(list[object], manifest["provenance"])
        )
        table_metadata = cast(dict[str, dict[str, object]], manifest["tables"])
        session = HistoricalSession(
            session_id=str(manifest["session_id"]),
            session_type="race",
            status=ArchiveEventStatus.COMPLETED,
            sync_status=SyncLifecycle.COMPLETE,
            capabilities=capabilities,
            quality=quality,
            provenance=provenance,
            row_counts={
                table_name.value: int(str(table_metadata[table_name.value]["rows"]))
                for table_name in ArchiveTableName
            },
            data_revision=str(manifest["data_revision"]),
            legacy_session_id=cast(str | None, manifest.get("legacy_session_id")),
        )
        return HistoricalEvent(
            event_id=str(manifest["event_id"]),
            season=int(str(manifest["season"])),
            round_number=int(str(manifest["round_number"])),
            name=str(manifest["event_name"]),
            official_name=str(manifest["official_name"]),
            event_date=str(manifest["event_date"]),
            circuit_name=str(manifest["circuit_name"]),
            locality=cast(str | None, manifest.get("locality")),
            country=cast(str | None, manifest.get("country")),
            country_code=cast(str | None, manifest.get("country_code")),
            status=ArchiveEventStatus.COMPLETED,
            sessions=(session,),
            drivers=tuple(str(item) for item in cast(list[object], manifest["drivers"])),
            teams=tuple(str(item) for item in cast(list[object], manifest["teams"])),
        )

    def sync(
        self,
        *,
        start_year: int = 2000,
        end_year: int | None = None,
        include_upcoming: bool = True,
        today: date | None = None,
        session_id: str | None = None,
    ) -> ArchiveSyncResult:
        with self.store.exclusive_lock("archive-sync"):
            return self._sync(
                start_year=start_year,
                end_year=end_year,
                include_upcoming=include_upcoming,
                today=today,
                session_id=session_id,
            )

    def rebuild_catalog(self) -> HistoricalCatalog:
        """Rebuild discovery metadata from active immutable manifests without provider I/O."""

        with self.store.exclusive_lock("archive-sync"):
            try:
                previous = self.store.load_catalog()
            except StorageIntegrityError:
                previous = None
            completed_events = [
                self._completed_event_from_manifest(self.store.load_manifest(session_id))
                for session_id in self.store.session_ids()
            ]
            completed_ids = {event.event_id for event in completed_events}
            completed_sessions = {event.race_session.session_id for event in completed_events}
            if len(completed_ids) != len(completed_events) or len(completed_sessions) != len(
                completed_events
            ):
                raise StorageIntegrityError("archive active manifests contain duplicate identities")
            preserved = (
                [
                    event
                    for event in previous.events
                    if event.status is not ArchiveEventStatus.COMPLETED
                    and event.event_id not in completed_ids
                ]
                if previous is not None
                else []
            )
            events = completed_events + preserved
            seasons = tuple(
                HistoricalSeason(
                    year=year,
                    events=tuple(
                        sorted(
                            (event for event in events if event.season == year),
                            key=lambda event: event.round_number,
                        )
                    ),
                )
                for year in sorted({event.season for event in events})
            )
            latest = max(
                completed_events,
                key=lambda event: (event.event_date, event.season, event.round_number),
                default=None,
            )
            timestamp = _utc_now()
            source_revision = (
                previous.source_revision
                if previous is not None
                else "archive-source-sha256-"
                + sha256(
                    "".join(
                        sorted(str(event.race_session.data_revision) for event in completed_events)
                    ).encode()
                ).hexdigest()
            )
            catalog = HistoricalCatalog(
                catalog_version=CATALOG_VERSION,
                generated_at_utc=timestamp,
                archive_start_year=min((season.year for season in seasons), default=2000),
                latest_completed_event_id=None if latest is None else latest.event_id,
                latest_completed_event_date=None if latest is None else latest.event_date,
                source_revision=source_revision,
                seasons=seasons,
            )
            self.store.save_catalog(catalog)
            quality_counts = Counter(
                event.race_session.quality.status.value for event in catalog.events
            )
            tier_counts = Counter(
                event.race_session.capabilities.tier.value for event in catalog.events
            )
            self.store.save_quality_report(
                {
                    "generated_at_utc": timestamp,
                    "catalog_version": CATALOG_VERSION,
                    "source_revision": source_revision,
                    "completed_events": len(completed_events),
                    "upcoming_events": sum(
                        event.status is ArchiveEventStatus.UPCOMING for event in preserved
                    ),
                    "cancelled_events": sum(
                        event.status is ArchiveEventStatus.CANCELLED for event in preserved
                    ),
                    "failed_events": 0,
                    "quality_status_counts": dict(sorted(quality_counts.items())),
                    "capability_tier_counts": dict(sorted(tier_counts.items())),
                    "failures": [],
                }
            )
            return catalog

    def _sync(
        self,
        *,
        start_year: int,
        end_year: int | None,
        include_upcoming: bool,
        today: date | None,
        session_id: str | None,
    ) -> ArchiveSyncResult:
        if start_year < 1950:
            raise ValueError("archive start year cannot precede the F1 championship")
        current_day = today or datetime.now(UTC).date()
        final_year = end_year or current_day.year
        if final_year < start_year:
            raise ValueError("archive end year precedes start year")
        target_key = _session_key(session_id) if session_id is not None else None
        if target_key is not None and not start_year <= target_key[0] <= final_year:
            raise ValueError("archive session is outside the requested year range")
        started_at = _utc_now()
        self.store.write_sync_state(
            {
                "status": SyncLifecycle.FETCHING.value,
                "started_at_utc": started_at,
                "start_year": start_year,
                "end_year": final_year,
            }
        )
        descriptor = self.client.dump_descriptor()
        dump_path = self.client.fetch_dump(descriptor)
        races = {
            (race.season, race.round_number): race
            for race in JolpicaDumpReader(dump_path, descriptor).races(
                start_year=start_year, end_year=final_year
            )
        }
        source_digests = [descriptor.file_hash]
        if final_year >= current_day.year and start_year <= current_day.year:
            schedule, schedule_digests = self.client.season_schedule(current_day.year)
            source_digests.extend(schedule_digests)
            for item in schedule:
                round_number = int(str(item["round"]))
                key = (current_day.year, round_number)
                source = races.get(key)
                if source is None:
                    source = self._current_schedule_race(item, provenance_hash=schedule_digests[-1])
                    races[key] = source
                if source.results.num_rows == 0 and completed_by_date(
                    str(item["date"]), today=current_day
                ):
                    patch, patch_digest = self.client.race_payload(current_day.year, round_number)
                    source_digests.append(patch_digest)
                    if patch is not None:
                        races[key] = patch

        base_catalog: HistoricalCatalog | None = None
        if target_key is not None:
            if target_key not in races:
                raise ValueError("archive session was not discovered from the provider")
            races = {target_key: races[target_key]}
            base_catalog = self.store.load_catalog()

        source_revision = (
            "archive-source-sha256-"
            + sha256("".join(sorted(set(source_digests))).encode()).hexdigest()
        )
        detailed = self._detailed_sessions()
        self.store.write_sync_state(
            {
                "status": SyncLifecycle.NORMALIZING.value,
                "started_at_utc": started_at,
                "source_revision": source_revision,
                "discovered_events": len(races),
            }
        )
        timestamp = _utc_now()
        completed = upcoming = cancelled = failed = cache_hits = written = 0
        events: list[HistoricalEvent] = []
        quality_counts: Counter[str] = Counter()
        tier_counts: Counter[str] = Counter()
        failures: list[dict[str, object]] = []

        for index, ((season, round_number), source) in enumerate(sorted(races.items()), start=1):
            if not (start_year <= season <= final_year):
                continue
            event_id = _event_id(season, round_number)
            is_complete = source.results.num_rows > 0
            if source.cancelled:
                cancelled += 1
                quality = _cancelled_quality(timestamp)
                session = HistoricalSession(
                    session_id=source.session_id,
                    session_type="race",
                    status=ArchiveEventStatus.CANCELLED,
                    sync_status=SyncLifecycle.SKIPPED,
                    capabilities=RaceDataCapabilities(),
                    quality=quality,
                    provenance=(source.provenance,),
                    row_counts={"results": 0, "laps": 0, "pit_stops": 0},
                )
                events.append(self._event(source, ArchiveEventStatus.CANCELLED, session))
                quality_counts[quality.status.value] += 1
                tier_counts[session.capabilities.tier.value] += 1
                continue
            if not is_complete and not include_upcoming:
                continue
            if not is_complete:
                upcoming += 1
                quality = _upcoming_quality(timestamp)
                session = HistoricalSession(
                    session_id=source.session_id,
                    session_type="race",
                    status=ArchiveEventStatus.UPCOMING,
                    sync_status=SyncLifecycle.DISCOVERED,
                    capabilities=RaceDataCapabilities(),
                    quality=quality,
                    provenance=(source.provenance,),
                    row_counts={"results": 0, "laps": 0, "pit_stops": 0},
                )
                events.append(self._event(source, ArchiveEventStatus.UPCOMING, session))
                quality_counts[quality.status.value] += 1
                tier_counts[session.capabilities.tier.value] += 1
                continue
            completed += 1
            try:
                capabilities, quality = evaluate_archive_race(
                    source.results,
                    source.laps,
                    source.pit_stops,
                    season=season,
                    validated_at_utc=timestamp,
                    expected_session_id=source.session_id,
                )
                provenance = [source.provenance]
                legacy_session_id: str | None = None
                if (season, round_number) in detailed:
                    legacy_session_id, detailed_capabilities, detailed_provenance = detailed[
                        (season, round_number)
                    ]
                    capabilities = detailed_capabilities
                    provenance.append(detailed_provenance)
                try:
                    active_before = self.store.active_revision(source.session_id)
                    valid_before = active_before is not None and self.store.revision_is_valid(
                        source.session_id, active_before
                    )
                except StorageIntegrityError:
                    active_before = None
                    valid_before = False
                revision = self.store.write_session(
                    source.session_id,
                    {
                        ArchiveTableName.RESULTS: source.results,
                        ArchiveTableName.LAPS: source.laps,
                        ArchiveTableName.PIT_STOPS: source.pit_stops,
                    },
                    source_revision=source.provenance.raw_sha256,
                    manifest={
                        "event_id": event_id,
                        "season": season,
                        "round_number": round_number,
                        "event_name": source.name,
                        "official_name": source.name,
                        "event_date": source.event_date,
                        "circuit_name": source.circuit_name,
                        "locality": source.locality,
                        "country": source.country,
                        "country_code": source.country_code,
                        "drivers": list(source.drivers),
                        "teams": list(source.teams),
                        "legacy_session_id": legacy_session_id,
                        "capabilities": capabilities.to_dict(),
                        "quality": quality.to_dict(),
                        "provenance": [item.to_dict() for item in provenance],
                    },
                )
                if active_before == revision and valid_before:
                    cache_hits += 1
                else:
                    written += 1
                published_event = self._completed_event_from_manifest(
                    self.store.load_manifest(source.session_id)
                )
                events.append(published_event)
                published_session = published_event.race_session
                quality_counts[published_session.quality.status.value] += 1
                tier_counts[published_session.capabilities.tier.value] += 1
            except Exception as exc:
                failed += 1
                quality = DataQuality(
                    status=QualityStatus.UNUSABLE,
                    reasons=("session_sync_failed",),
                    metrics={"error_type": type(exc).__name__},
                    validated_at_utc=timestamp,
                )
                session = HistoricalSession(
                    session_id=source.session_id,
                    session_type="race",
                    status=ArchiveEventStatus.COMPLETED,
                    sync_status=SyncLifecycle.FAILED,
                    capabilities=RaceDataCapabilities(results=True),
                    quality=quality,
                    provenance=(source.provenance,),
                    row_counts={
                        "results": cast(int, source.results.num_rows),
                        "laps": cast(int, source.laps.num_rows),
                        "pit_stops": cast(int, source.pit_stops.num_rows),
                    },
                )
                events.append(self._event(source, ArchiveEventStatus.COMPLETED, session))
                failures.append(
                    {
                        "event_id": event_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                quality_counts[quality.status.value] += 1
                tier_counts[session.capabilities.tier.value] += 1
            if index % 25 == 0:
                self.store.write_sync_state(
                    {
                        "status": SyncLifecycle.WRITING.value,
                        "started_at_utc": started_at,
                        "source_revision": source_revision,
                        "processed_events": index,
                        "discovered_events": len(races),
                        "failed_events": failed,
                    }
                )

        catalog_source_revision = source_revision
        if base_catalog is not None and target_key is not None:
            target_event_id = _event_id(*target_key)
            events = [
                event for event in base_catalog.events if event.event_id != target_event_id
            ] + events
            catalog_source_revision = (
                base_catalog.source_revision
                if base_catalog.source_revision == source_revision
                else _mixed_catalog_source_revision(events)
            )

        seasons = tuple(
            HistoricalSeason(
                year=year,
                events=tuple(
                    sorted(
                        (event for event in events if event.season == year),
                        key=lambda event: event.round_number,
                    )
                ),
            )
            for year in sorted({event.season for event in events})
        )
        completed_events = sorted(
            (event for event in events if event.status is ArchiveEventStatus.COMPLETED),
            key=lambda event: (event.event_date, event.season, event.round_number),
        )
        latest = completed_events[-1] if completed_events else None
        catalog = HistoricalCatalog(
            catalog_version=CATALOG_VERSION,
            generated_at_utc=timestamp,
            archive_start_year=(
                base_catalog.archive_start_year if base_catalog is not None else start_year
            ),
            latest_completed_event_id=None if latest is None else latest.event_id,
            latest_completed_event_date=None if latest is None else latest.event_date,
            source_revision=catalog_source_revision,
            seasons=seasons,
        )
        self.store.save_catalog(catalog)
        catalog_quality_counts = Counter(
            event.race_session.quality.status.value for event in catalog.events
        )
        catalog_tier_counts = Counter(
            event.race_session.capabilities.tier.value for event in catalog.events
        )
        catalog_completed = sum(
            event.status is ArchiveEventStatus.COMPLETED for event in catalog.events
        )
        catalog_upcoming = sum(
            event.status is ArchiveEventStatus.UPCOMING for event in catalog.events
        )
        catalog_cancelled = sum(
            event.status is ArchiveEventStatus.CANCELLED for event in catalog.events
        )
        catalog_failed = sum(
            event.race_session.sync_status is SyncLifecycle.FAILED for event in catalog.events
        )
        quality_report = {
            "generated_at_utc": timestamp,
            "catalog_version": CATALOG_VERSION,
            "source_revision": catalog_source_revision,
            "completed_events": catalog_completed,
            "upcoming_events": catalog_upcoming,
            "cancelled_events": catalog_cancelled,
            "failed_events": catalog_failed,
            "quality_status_counts": dict(sorted(catalog_quality_counts.items())),
            "capability_tier_counts": dict(sorted(catalog_tier_counts.items())),
            "failures": failures,
        }
        self.store.save_quality_report(quality_report)
        final_status = SyncLifecycle.PARTIAL.value if failed else SyncLifecycle.COMPLETE.value
        self.store.write_sync_state(
            {
                "status": final_status,
                "started_at_utc": started_at,
                "completed_at_utc": _utc_now(),
                "source_revision": catalog_source_revision,
                "provider_source_revision": source_revision,
                "target_session_id": session_id,
                "completed_events": completed,
                "upcoming_events": upcoming,
                "cancelled_events": cancelled,
                "failed_events": failed,
                "cache_hits": cache_hits,
                "written": written,
            }
        )
        return ArchiveSyncResult(
            catalog=catalog,
            completed=completed,
            upcoming=upcoming,
            cancelled=cancelled,
            failed=failed,
            cache_hits=cache_hits,
            written=written,
            source_revision=catalog_source_revision,
            provider_source_revision=source_revision,
            storage=self.store.storage_report(),
        )

    def _event(
        self,
        source: ArchiveSourceRace,
        status: ArchiveEventStatus,
        session: HistoricalSession,
    ) -> HistoricalEvent:
        return HistoricalEvent(
            event_id=_event_id(source.season, source.round_number),
            season=source.season,
            round_number=source.round_number,
            name=source.name,
            official_name=source.name,
            event_date=source.event_date,
            circuit_name=source.circuit_name,
            locality=source.locality,
            country=source.country,
            country_code=source.country_code,
            status=status,
            sessions=(session,),
            drivers=source.drivers,
            teams=source.teams,
        )


__all__ = ["ArchiveSyncResult", "HistoricalArchiveSync"]
