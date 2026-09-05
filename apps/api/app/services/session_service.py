"""Canonical session queries with bounded Arrow-native filtering and pagination."""

from __future__ import annotations

from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import SessionDataIncompleteError
from downforce_core.providers import DatasetAvailability
from downforce_core.storage import CanonicalTableName, DownforceRepository, SessionManifest


class SessionService:
    def __init__(self, repository: DownforceRepository) -> None:
        self.repository = repository

    def list_sessions(self, *, offset: int, limit: int) -> dict[str, object]:
        sessions = self.repository.list_sessions()
        selected = sessions[offset : offset + limit]
        return {
            "items": [
                {
                    "session_id": item.session_id,
                    "dataset_id": item.dataset_id,
                    "season": item.season,
                    "event_name": item.event_name,
                    "session_type": item.session_type,
                    "provider": item.provider,
                    "created_at_utc": item.created_at_utc,
                }
                for item in selected
            ],
            "offset": offset,
            "limit": limit,
            "total": len(sessions),
        }

    def session(self, session_id: str) -> dict[str, object]:
        manifest = self.repository.load_manifest(session_id)
        serialized = manifest.to_dict()
        return {
            "session_id": manifest.session_id,
            "dataset_id": manifest.dataset_id,
            "snapshot_id": manifest.snapshot_id,
            "session": serialized["session"],
            "provider": serialized["provider"],
            "capabilities": serialized["capabilities"],
            "completeness": serialized["completeness"],
            "tables": {
                name: {
                    "availability": artifact.availability,
                    "materialized": artifact.materialized,
                    "row_count": artifact.row_count,
                    "min_session_time_ms": artifact.min_session_time_ms,
                    "max_session_time_ms": artifact.max_session_time_ms,
                }
                for name, artifact in manifest.tables.items()
            },
            "warnings": list(manifest.warnings),
            "canonical_schema_version": manifest.canonical_schema_version,
            "normalization_version": manifest.normalization_version,
            "timeline_version": manifest.timeline_version,
            "replay_version": manifest.replay_version,
        }

    def _available_table(
        self, session_id: str, table_name: CanonicalTableName
    ) -> tuple[SessionManifest, pa.Table]:
        manifest = self.repository.load_manifest(session_id)
        artifact = manifest.tables[table_name.value]
        if artifact.availability in {
            DatasetAvailability.UNSUPPORTED.value,
            DatasetAvailability.NOT_REQUESTED.value,
            DatasetAvailability.ERROR.value,
        }:
            raise SessionDataIncompleteError(
                f"canonical table {table_name.value} is {artifact.availability}"
            )
        return manifest, self.repository.load_table(manifest.session_id, table_name)

    def drivers(self, session_id: str) -> dict[str, object]:
        _, table = self._available_table(session_id, CanonicalTableName.DRIVERS)
        rows = cast(list[dict[str, object]], table.to_pylist())
        return {
            "items": [
                {
                    "driver_id": row["driver_id"],
                    "racing_number": row["racing_number"],
                    "abbreviation": row["abbreviation"],
                    "full_name": row["full_name"],
                    "team_name": row["team_name"],
                    "country_code": row["country_code"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def laps(
        self,
        session_id: str,
        *,
        driver_id: str | None,
        from_lap: int | None,
        to_lap: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        _, table = self._available_table(session_id, CanonicalTableName.LAPS)
        if driver_id is not None:
            validate_safe_identifier(driver_id, field_name="driver_id")
            table = table.filter(pc.equal(table.column("driver_id"), driver_id))
        if from_lap is not None:
            table = table.filter(pc.greater_equal(table.column("lap_number"), from_lap))
        if to_lap is not None:
            table = table.filter(pc.less_equal(table.column("lap_number"), to_lap))
        total = table.num_rows
        rows = cast(list[dict[str, object]], table.slice(offset, limit).to_pylist())
        fields = (
            "driver_id",
            "lap_number",
            "lap_start_time_ms",
            "lap_end_time_ms",
            "lap_time_ms",
            "sector_1_time_ms",
            "sector_2_time_ms",
            "sector_3_time_ms",
            "stint_number",
            "compound",
            "raw_compound",
            "tyre_life_laps",
            "is_personal_best",
            "is_accurate",
            "is_generated",
            "is_deleted",
            "deleted_reason",
            "raw_track_status",
        )
        return {
            "items": [{name: row[name] for name in fields} for row in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
        }

    def track_positions(
        self,
        session_id: str,
        *,
        driver_id: str | None,
        from_ms: int | None,
        to_ms: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, object]:
        _, table = self._available_table(session_id, CanonicalTableName.TRACK_POSITIONS)
        if driver_id is not None:
            validate_safe_identifier(driver_id, field_name="driver_id")
            table = table.filter(pc.equal(table.column("driver_id"), driver_id))
        if from_ms is not None:
            table = table.filter(pc.greater_equal(table.column("session_time_ms"), from_ms))
        if to_ms is not None:
            table = table.filter(pc.less_equal(table.column("session_time_ms"), to_ms))
        total = table.num_rows
        fields = ("driver_id", "session_time_ms", "x_m", "y_m", "z_m", "raw_status")
        rows = cast(list[dict[str, object]], table.slice(offset, limit).to_pylist())
        return {
            "items": [{name: row[name] for name in fields} for row in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
        }

    def telemetry_index(self, session_id: str, *, driver_id: str | None) -> dict[str, object]:
        _, table = self._available_table(session_id, CanonicalTableName.TELEMETRY_INDEX)
        if driver_id is not None:
            validate_safe_identifier(driver_id, field_name="driver_id")
            table = table.filter(pc.equal(table.column("driver_id"), driver_id))
        fields = (
            "driver_id",
            "start_time_ms",
            "end_time_ms",
            "data_key",
            "channel_names",
            "sample_count",
            "lap_number",
        )
        rows = cast(list[dict[str, object]], table.to_pylist())
        return {
            "items": [{name: row[name] for name in fields} for row in rows],
            "total": len(rows),
        }


__all__ = ["SessionService"]
