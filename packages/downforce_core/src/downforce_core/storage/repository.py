"""Atomic canonical writer and strict repository readers over local Parquet."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.enums import DataQuality, SessionType
from downforce_core.domain.events import RaceEvent
from downforce_core.domain.identifiers import SessionId, validate_safe_identifier
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
    WeatherRecord,
)
from downforce_core.exceptions import (
    SchemaVersionError,
    SessionNotFoundError,
    StorageIntegrityError,
)
from downforce_core.normalization.models import (
    CanonicalTrackPositions,
    NormalizedSession,
    ValidationIssue,
    ValidationLevel,
    ValidationReport,
)
from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    SessionRef,
    thaw_provider_metadata,
)
from downforce_core.storage.events import events_from_table, events_to_table
from downforce_core.storage.layout import StorageLayout, ensure_contained, write_text_atomic
from downforce_core.storage.manifest import SessionManifest, TableArtifact, canonical_json
from downforce_core.storage.parquet import (
    canonical_tables,
    decode_records,
    file_sha256,
    read_parquet,
    table_time_range,
    write_parquet,
)
from downforce_core.storage.schemas import (
    CANONICAL_SCHEMAS,
    SOURCE_DATASET,
    CanonicalTableName,
    schema_fingerprint,
)
from downforce_core.versions import (
    CANONICAL_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    REPLAY_VERSION,
    STORAGE_FORMAT_VERSION,
    TIMELINE_VERSION,
)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    dataset_id: str
    season: int
    event_name: str
    session_type: str
    provider: str
    created_at_utc: datetime


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _provenance_dict(value: SourceProvenance) -> dict[str, object]:
    return {
        "provider": value.provider,
        "provider_version": value.provider_version,
        "source": value.source,
        "retrieved_at": _iso(value.retrieved_at),
        "source_record_id": value.source_record_id,
        "source_published_at": _iso(value.source_published_at),
    }


def _metadata_dict(value: SessionMetadata) -> dict[str, object]:
    return {
        "season": value.season,
        "event_name": value.event_name,
        "session_name": value.session_name,
        "session_type": value.session_type.value,
        "round_number": value.round_number,
        "country_code": value.country_code,
        "circuit_name": value.circuit_name,
        "scheduled_start_utc": _iso(value.scheduled_start_utc),
        "session_start_utc": _iso(value.session_start_utc),
        "session_end_utc": _iso(value.session_end_utc),
        "session_origin_utc": _iso(value.session_origin_utc),
        "data_quality": value.data_quality.value,
        "provenance": _provenance_dict(value.provenance),
    }


def _capabilities_dict(value: ProviderCapabilities) -> dict[str, object]:
    return {
        "drivers": value.drivers,
        "laps": value.laps,
        "weather": value.weather,
        "race_control": value.race_control,
        "race_positions": value.race_positions,
        "track_positions": value.track_positions,
        "car_telemetry": value.car_telemetry,
        "live": value.live,
    }


def _core_version() -> str:
    try:
        return version("downforce-core")
    except PackageNotFoundError:
        return "0.1.0"


def _read_json(path: Path, description: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise StorageIntegrityError(f"{description} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageIntegrityError(f"{description} is unreadable") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StorageIntegrityError(f"{description} is malformed")
    return cast(Mapping[str, object], value)


def _dataset_identity_payload(
    *,
    session_id: str,
    snapshot_id: str,
    artifacts: Mapping[str, TableArtifact],
    timeline_version: str | None,
    replay_version: str | None,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "snapshot_id": snapshot_id,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "storage_format_version": STORAGE_FORMAT_VERSION,
        "timeline_version": timeline_version,
        "replay_version": replay_version,
        "tables": {
            name: {
                "source_dataset": artifact.source_dataset,
                "availability": artifact.availability,
                "materialized": artifact.materialized,
                "path": artifact.path,
                "row_count": artifact.row_count,
                "sha256": artifact.sha256,
                "schema_fingerprint": artifact.schema_fingerprint,
                "min_session_time_ms": artifact.min_session_time_ms,
                "max_session_time_ms": artifact.max_session_time_ms,
            }
            for name, artifact in sorted(artifacts.items())
        },
    }


def _dataset_id(
    *,
    session_id: str,
    snapshot_id: str,
    artifacts: Mapping[str, TableArtifact],
    timeline_version: str | None,
    replay_version: str | None,
) -> str:
    payload = _dataset_identity_payload(
        session_id=session_id,
        snapshot_id=snapshot_id,
        artifacts=artifacts,
        timeline_version=timeline_version,
        replay_version=replay_version,
    )
    return f"dataset-sha256-{sha256(canonical_json(payload).encode()).hexdigest()}"


def _validation_dict(issue: ValidationIssue) -> dict[str, object]:
    return {
        "level": issue.level.value,
        "code": issue.code,
        "table": issue.table,
        "message": issue.message,
        "row_key": issue.row_key,
    }


def _manifest(
    *,
    normalized: NormalizedSession,
    snapshot_id: str,
    artifacts: Mapping[str, TableArtifact],
    created_at: datetime,
    events_materialized: bool,
) -> SessionManifest:
    session_id = str(normalized.metadata.session_id)
    dataset_id = _dataset_id(
        session_id=session_id,
        snapshot_id=snapshot_id,
        artifacts=artifacts,
        timeline_version=TIMELINE_VERSION if events_materialized else None,
        replay_version=REPLAY_VERSION if events_materialized else None,
    )
    return SessionManifest(
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        session_id=session_id,
        canonical_schema_version=CANONICAL_SCHEMA_VERSION,
        normalization_version=NORMALIZATION_VERSION,
        storage_format_version=STORAGE_FORMAT_VERSION,
        created_at_utc=created_at,
        session=_metadata_dict(normalized.metadata),
        provider={
            "name": normalized.provider_name,
            "version": normalized.provider_version,
            "retrieved_at_utc": _iso(normalized.retrieved_at),
            "raw_snapshot_id": snapshot_id,
        },
        software={
            "downforce_core_version": _core_version(),
            "pyarrow_version": pa.__version__,
        },
        capabilities=_capabilities_dict(normalized.capabilities),
        completeness={name.value: state.value for name, state in normalized.completeness.items()},
        tables=artifacts,
        warnings=normalized.warnings,
        validation_issues=tuple(
            _validation_dict(issue) for issue in normalized.validation_report.issues
        ),
        knowledge_time={
            "model": "event-time replay from a post-session provider snapshot",
            "exact_source_publication_times_available": False,
            "limitation": (
                "FastF1 does not expose exact publication times for every historical row; "
                "canonical state excludes final classification from the replay timeline."
            ),
        },
        provider_metadata=thaw_provider_metadata(normalized.provider_metadata),
        requested_session={
            "season": normalized.requested_session.season,
            "event": normalized.requested_session.event,
            "session": normalized.requested_session.session.value,
            "requested_session_id": str(normalized.requested_session.session_id),
        },
        timeline_version=TIMELINE_VERSION if events_materialized else None,
        replay_version=REPLAY_VERSION if events_materialized else None,
    )


class DownforceRepository:
    """Filesystem repository that exposes only verified canonical data."""

    def __init__(self, project_root: str | Path) -> None:
        self.layout = StorageLayout(project_root)

    def write_session(
        self,
        normalized: NormalizedSession,
        snapshot_id: str,
        *,
        events: tuple[RaceEvent, ...] | None = None,
    ) -> SessionManifest:
        validate_safe_identifier(snapshot_id, field_name="snapshot_id")
        session_id = str(normalized.metadata.session_id)
        tables = canonical_tables(normalized)
        if events is not None:
            tables[CanonicalTableName.EVENTS] = events_to_table(events)
        stage = Path(tempfile.mkdtemp(prefix="normalized-", dir=self.layout.staging_root))
        try:
            artifacts: dict[str, TableArtifact] = {}
            for table_name in CanonicalTableName:
                source_dataset = SOURCE_DATASET[table_name]
                if table_name is CanonicalTableName.EVENTS:
                    availability = (
                        DatasetAvailability.AVAILABLE
                        if events is not None
                        else DatasetAvailability.NOT_REQUESTED
                    )
                    materialized = events is not None
                else:
                    availability = normalized.completeness[DatasetName(source_dataset)]
                    materialized = availability in {
                        DatasetAvailability.AVAILABLE,
                        DatasetAvailability.EMPTY,
                    }
                expected_fingerprint = schema_fingerprint(CANONICAL_SCHEMAS[table_name])
                if materialized:
                    relative_path = f"tables/{table_name.value}.parquet"
                    table = tables[table_name]
                    file_path = stage / relative_path
                    write_parquet(file_path, table_name, table)
                    minimum, maximum = table_time_range(table_name, table)
                    artifact = TableArtifact(
                        name=table_name,
                        source_dataset=source_dataset,
                        availability=availability.value,
                        materialized=True,
                        path=relative_path,
                        row_count=table.num_rows,
                        sha256=file_sha256(file_path),
                        schema_fingerprint=expected_fingerprint,
                        min_session_time_ms=minimum,
                        max_session_time_ms=maximum,
                    )
                else:
                    artifact = TableArtifact(
                        name=table_name,
                        source_dataset=source_dataset,
                        availability=availability.value,
                        materialized=False,
                        path=None,
                        row_count=0,
                        sha256=None,
                        schema_fingerprint=expected_fingerprint,
                    )
                artifacts[table_name.value] = artifact

            manifest = _manifest(
                normalized=normalized,
                snapshot_id=snapshot_id,
                artifacts=artifacts,
                created_at=datetime.now(UTC),
                events_materialized=events is not None,
            )
            write_text_atomic(stage / "manifest.json", manifest.to_json())
            target = self.layout.normalized_dataset(session_id, manifest.dataset_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.replace(stage, target)
                    stage = Path()
                except OSError:
                    if not target.exists():
                        raise
            committed = self._load_dataset_manifest(session_id, manifest.dataset_id)
            self._verify_manifest(committed, target)
            write_text_atomic(
                self.layout.active_pointer(session_id),
                canonical_json({"session_id": session_id, "dataset_id": committed.dataset_id})
                + "\n",
            )
            requested_id = str(normalized.requested_session.session_id)
            write_text_atomic(
                self.layout.alias_pointer(requested_id),
                canonical_json(
                    {
                        "requested_session_id": requested_id,
                        "canonical_session_id": session_id,
                    }
                )
                + "\n",
            )
            return committed
        finally:
            if stage != Path() and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def resolve_session_id(self, session_id: str | SessionId) -> str:
        raw = str(session_id)
        validate_safe_identifier(raw, field_name="session_id")
        alias = self.layout.alias_pointer(raw)
        if not alias.exists():
            return raw
        data = _read_json(alias, "session alias")
        if data.get("requested_session_id") != raw:
            raise StorageIntegrityError("session alias does not match its filename")
        canonical = data.get("canonical_session_id")
        if not isinstance(canonical, str):
            raise StorageIntegrityError("session alias target is malformed")
        validate_safe_identifier(canonical, field_name="canonical_session_id")
        return canonical

    def session_exists(self, session_id: str | SessionId) -> bool:
        try:
            self.load_manifest(session_id)
        except SessionNotFoundError:
            return False
        return True

    def load_manifest(self, session_id: str | SessionId) -> SessionManifest:
        canonical, dataset_id = self.active_dataset_identity(session_id)
        manifest = self._load_dataset_manifest(canonical, dataset_id)
        self._verify_manifest(
            manifest,
            self.layout.normalized_dataset(canonical, dataset_id),
        )
        return manifest

    def active_dataset_identity(self, session_id: str | SessionId) -> tuple[str, str]:
        """Read the atomic active pointer without loading canonical table payloads."""

        canonical = self.resolve_session_id(session_id)
        pointer = self.layout.active_pointer(canonical)
        if not pointer.exists():
            raise SessionNotFoundError(f"canonical session is not available: {session_id}")
        data = _read_json(pointer, "active dataset pointer")
        if data.get("session_id") != canonical:
            raise StorageIntegrityError("active pointer session does not match its path")
        dataset_id = data.get("dataset_id")
        if not isinstance(dataset_id, str):
            raise StorageIntegrityError("active pointer dataset ID is malformed")
        validate_safe_identifier(dataset_id, field_name="dataset_id")
        return canonical, dataset_id

    def _load_dataset_manifest(self, session_id: str, dataset_id: str) -> SessionManifest:
        root = self.layout.normalized_dataset(session_id, dataset_id)
        if not root.is_dir():
            raise StorageIntegrityError("active canonical dataset directory is missing")
        ensure_contained(root, self.layout.data_root)
        path = root / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise StorageIntegrityError("canonical manifest is missing or unsafe")
        try:
            manifest = SessionManifest.from_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise StorageIntegrityError("canonical manifest is unreadable") from exc
        if manifest.session_id != session_id or manifest.dataset_id != dataset_id:
            raise StorageIntegrityError("canonical manifest identity does not match its path")
        return manifest

    def _verify_manifest(self, manifest: SessionManifest, root: Path) -> None:
        if manifest.canonical_schema_version != CANONICAL_SCHEMA_VERSION:
            raise SchemaVersionError("canonical schema version is incompatible")
        if manifest.normalization_version != NORMALIZATION_VERSION:
            raise SchemaVersionError("normalization version is incompatible")
        if manifest.storage_format_version != STORAGE_FORMAT_VERSION:
            raise SchemaVersionError("storage format version is incompatible")
        if (manifest.timeline_version is None) != (manifest.replay_version is None):
            raise StorageIntegrityError("timeline/replay versions must be present together")
        if manifest.timeline_version is not None and manifest.timeline_version != TIMELINE_VERSION:
            raise SchemaVersionError("timeline version is incompatible")
        if manifest.replay_version is not None and manifest.replay_version != REPLAY_VERSION:
            raise SchemaVersionError("replay version is incompatible")
        expected_id = _dataset_id(
            session_id=manifest.session_id,
            snapshot_id=manifest.snapshot_id,
            artifacts=manifest.tables,
            timeline_version=manifest.timeline_version,
            replay_version=manifest.replay_version,
        )
        if expected_id != manifest.dataset_id:
            raise StorageIntegrityError("canonical dataset identity checksum failed")
        for name in CanonicalTableName:
            artifact = manifest.tables[name.value]
            if artifact.schema_fingerprint != schema_fingerprint(CANONICAL_SCHEMAS[name]):
                raise SchemaVersionError(f"canonical table schema is incompatible: {name.value}")
            if not artifact.materialized:
                continue
            expected_path = f"tables/{name.value}.parquet"
            if artifact.path != expected_path:
                raise StorageIntegrityError(f"canonical table path is invalid: {name.value}")
            path = root / expected_path
            ensure_contained(path, root)
            if file_sha256(path) != artifact.sha256:
                raise StorageIntegrityError(f"canonical table checksum failed: {name.value}")
            table = read_parquet(path, name)
            if table.num_rows != artifact.row_count:
                raise StorageIntegrityError(f"canonical table row count failed: {name.value}")
            minimum, maximum = table_time_range(name, table)
            if (minimum, maximum) != (
                artifact.min_session_time_ms,
                artifact.max_session_time_ms,
            ):
                raise StorageIntegrityError(f"canonical table time range failed: {name.value}")

    def load_table(self, session_id: str | SessionId, table_name: CanonicalTableName) -> pa.Table:
        if not isinstance(table_name, CanonicalTableName):
            raise TypeError("table_name must be a CanonicalTableName")
        manifest = self.load_manifest(session_id)
        return self._table_from_manifest(manifest, table_name)

    def _table_from_manifest(
        self, manifest: SessionManifest, table_name: CanonicalTableName
    ) -> pa.Table:
        artifact = manifest.tables[table_name.value]
        if not artifact.materialized or artifact.path is None:
            return pa.Table.from_arrays(
                [pa.array([], type=field.type) for field in CANONICAL_SCHEMAS[table_name]],
                schema=CANONICAL_SCHEMAS[table_name],
            )
        root = self.layout.normalized_dataset(manifest.session_id, manifest.dataset_id)
        return read_parquet(root / artifact.path, table_name)

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        root = self.layout.normalized_root
        if not root.exists():
            return ()
        summaries: list[SessionSummary] = []
        for session_root in sorted(root.iterdir(), key=lambda path: path.name):
            if not session_root.is_dir() or session_root.is_symlink():
                continue
            if not (session_root / "active.json").exists():
                continue
            manifest = self.load_manifest(session_root.name)
            session_data = manifest.session
            provider_data = manifest.provider
            season = session_data.get("season")
            event_name = session_data.get("event_name")
            session_type = session_data.get("session_type")
            provider = provider_data.get("name")
            if not isinstance(season, int) or not all(
                isinstance(value, str) for value in (event_name, session_type, provider)
            ):
                raise StorageIntegrityError("session summary metadata is malformed")
            summaries.append(
                SessionSummary(
                    session_id=manifest.session_id,
                    dataset_id=manifest.dataset_id,
                    season=season,
                    event_name=cast(str, event_name),
                    session_type=cast(str, session_type),
                    provider=cast(str, provider),
                    created_at_utc=manifest.created_at_utc,
                )
            )
        return tuple(summaries)

    def load_session(
        self,
        session_id: str | SessionId,
        *,
        include_track_positions: bool = True,
    ) -> NormalizedSession:
        manifest = self.load_manifest(session_id)
        loaded = {
            name: (
                self._table_from_manifest(manifest, name)
                if include_track_positions or name is not CanonicalTableName.TRACK_POSITIONS
                else pa.Table.from_arrays(
                    [
                        pa.array([], type=field.type)
                        for field in CANONICAL_SCHEMAS[CanonicalTableName.TRACK_POSITIONS]
                    ],
                    schema=CANONICAL_SCHEMAS[CanonicalTableName.TRACK_POSITIONS],
                )
            )
            for name in CanonicalTableName
        }
        session_metadata = _decode_metadata(manifest)
        provider_name = _required_manifest_text(manifest.provider, "name")
        provider_version = _required_manifest_text(manifest.provider, "version")
        retrieved_at = _manifest_datetime(manifest.provider.get("retrieved_at_utc"), required=True)
        if retrieved_at is None:
            raise StorageIntegrityError("provider retrieval time is missing")

        track_table = loaded[CanonicalTableName.TRACK_POSITIONS]
        dense = track_table.select(
            ["driver_id", "session_time_ms", "x_m", "y_m", "z_m", "raw_status"]
        )
        if dense.num_rows:
            raw_provenance = track_table.column("provenance")[0].as_py()
            track_provenance = _decode_provenance(raw_provenance)
            track_source = track_provenance.source
            track_retrieved = track_provenance.retrieved_at
        else:
            track_source = f"{provider_name}.track-positions"
            track_retrieved = retrieved_at
        track_positions = CanonicalTrackPositions(
            session_id=session_metadata.session_id,
            table=dense,
            provider_name=provider_name,
            provider_version=provider_version,
            retrieved_at=track_retrieved,
            source=track_source,
        )

        capabilities = manifest.capabilities
        completeness = manifest.completeness
        requested = manifest.requested_session
        return NormalizedSession(
            metadata=session_metadata,
            drivers=cast(
                tuple[DriverRecord, ...],
                decode_records(CanonicalTableName.DRIVERS, loaded[CanonicalTableName.DRIVERS]),
            ),
            classifications=cast(
                tuple[DriverClassificationRecord, ...],
                decode_records(
                    CanonicalTableName.DRIVER_CLASSIFICATIONS,
                    loaded[CanonicalTableName.DRIVER_CLASSIFICATIONS],
                ),
            ),
            laps=cast(
                tuple[LapRecord, ...],
                decode_records(CanonicalTableName.LAPS, loaded[CanonicalTableName.LAPS]),
            ),
            stints=cast(
                tuple[StintRecord, ...],
                decode_records(CanonicalTableName.STINTS, loaded[CanonicalTableName.STINTS]),
            ),
            pit_stops=cast(
                tuple[PitStopRecord, ...],
                decode_records(CanonicalTableName.PIT_STOPS, loaded[CanonicalTableName.PIT_STOPS]),
            ),
            weather=cast(
                tuple[WeatherRecord, ...],
                decode_records(CanonicalTableName.WEATHER, loaded[CanonicalTableName.WEATHER]),
            ),
            race_control=cast(
                tuple[RaceControlRecord, ...],
                decode_records(
                    CanonicalTableName.RACE_CONTROL,
                    loaded[CanonicalTableName.RACE_CONTROL],
                ),
            ),
            race_positions=cast(
                tuple[RacePositionRecord, ...],
                decode_records(
                    CanonicalTableName.RACE_POSITIONS,
                    loaded[CanonicalTableName.RACE_POSITIONS],
                ),
            ),
            track_positions=track_positions,
            telemetry_index=cast(
                tuple[TelemetryIndexRecord, ...],
                decode_records(
                    CanonicalTableName.TELEMETRY_INDEX,
                    loaded[CanonicalTableName.TELEMETRY_INDEX],
                ),
            ),
            capabilities=ProviderCapabilities(
                drivers=_manifest_bool(capabilities, "drivers"),
                laps=_manifest_bool(capabilities, "laps"),
                weather=_manifest_bool(capabilities, "weather"),
                race_control=_manifest_bool(capabilities, "race_control"),
                race_positions=_manifest_bool(capabilities, "race_positions"),
                track_positions=_manifest_bool(capabilities, "track_positions"),
                car_telemetry=_manifest_bool(capabilities, "car_telemetry"),
                live=_manifest_bool(capabilities, "live"),
            ),
            completeness={
                name: DatasetAvailability(_required_manifest_text(completeness, name.value))
                for name in DatasetName
            },
            warnings=manifest.warnings,
            provider_name=provider_name,
            provider_version=provider_version,
            retrieved_at=retrieved_at,
            provider_metadata=manifest.provider_metadata,
            requested_session=SessionRef(
                cast(int, requested.get("season")),
                cast(int | str, requested.get("event")),
                _required_manifest_text(requested, "session"),
            ),
            validation_report=ValidationReport(
                tuple(_decode_validation_issue(issue) for issue in manifest.validation_issues)
            ),
            telemetry_materialized=False,
        )

    def load_events(self, session_id: str | SessionId) -> tuple[RaceEvent, ...]:
        manifest = self.load_manifest(session_id)
        artifact = manifest.tables[CanonicalTableName.EVENTS.value]
        if not artifact.materialized:
            return ()
        return events_from_table(self._table_from_manifest(manifest, CanonicalTableName.EVENTS))


def _required_manifest_text(value: Mapping[str, object], name: str) -> str:
    raw = value.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise StorageIntegrityError(f"manifest field is malformed: {name}")
    return raw


def _manifest_bool(value: Mapping[str, object], name: str) -> bool:
    raw = value.get(name)
    if type(raw) is not bool:
        raise StorageIntegrityError(f"manifest field is malformed: {name}")
    return raw


def _manifest_datetime(value: object, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StorageIntegrityError("manifest UTC timestamp must include an offset")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        raise StorageIntegrityError("manifest UTC timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageIntegrityError("manifest UTC timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise StorageIntegrityError("manifest UTC timestamp must include an offset")
    return parsed.astimezone(UTC)


def _decode_provenance(value: object) -> SourceProvenance:
    if not isinstance(value, Mapping):
        raise StorageIntegrityError("manifest provenance is malformed")
    mapping = cast(Mapping[str, object], value)
    retrieved = _manifest_datetime(mapping.get("retrieved_at"), required=True)
    if retrieved is None:
        raise StorageIntegrityError("manifest provenance retrieval time is missing")
    return SourceProvenance(
        provider=_required_manifest_text(mapping, "provider"),
        provider_version=_required_manifest_text(mapping, "provider_version"),
        source=_required_manifest_text(mapping, "source"),
        retrieved_at=retrieved,
        source_record_id=cast(str | None, mapping.get("source_record_id")),
        source_published_at=_manifest_datetime(mapping.get("source_published_at")),
    )


def _decode_metadata(manifest: SessionManifest) -> SessionMetadata:
    value = manifest.session
    season = value.get("season")
    if isinstance(season, bool) or not isinstance(season, int):
        raise StorageIntegrityError("manifest season is malformed")
    round_number = value.get("round_number")
    if round_number is not None and (
        isinstance(round_number, bool) or not isinstance(round_number, int)
    ):
        raise StorageIntegrityError("manifest round number is malformed")
    return SessionMetadata(
        session_id=SessionId(manifest.session_id),
        season=season,
        event_name=_required_manifest_text(value, "event_name"),
        session_name=_required_manifest_text(value, "session_name"),
        session_type=SessionType(_required_manifest_text(value, "session_type")),
        provenance=_decode_provenance(value.get("provenance")),
        round_number=round_number,
        country_code=cast(str | None, value.get("country_code")),
        circuit_name=cast(str | None, value.get("circuit_name")),
        scheduled_start_utc=_manifest_datetime(value.get("scheduled_start_utc")),
        session_start_utc=_manifest_datetime(value.get("session_start_utc")),
        session_end_utc=_manifest_datetime(value.get("session_end_utc")),
        session_origin_utc=_manifest_datetime(value.get("session_origin_utc")),
        data_quality=DataQuality(_required_manifest_text(value, "data_quality")),
    )


def _decode_validation_issue(value: Mapping[str, object]) -> ValidationIssue:
    return ValidationIssue(
        level=ValidationLevel(_required_manifest_text(value, "level")),
        code=_required_manifest_text(value, "code"),
        table=_required_manifest_text(value, "table"),
        message=_required_manifest_text(value, "message"),
        row_key=cast(str | None, value.get("row_key")),
    )


__all__ = ["DownforceRepository", "SessionSummary"]
