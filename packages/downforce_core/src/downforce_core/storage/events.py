"""Exact Arrow codec for canonical significant replay events."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.events import RaceEvent, RaceEventType, canonical_event_payload
from downforce_core.domain.identifiers import DriverId, SessionId
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.schemas import CANONICAL_SCHEMAS, CanonicalTableName


def events_to_table(events: Sequence[RaceEvent]) -> pa.Table:
    rows = [
        {
            "session_id": str(event.session_id),
            "event_id": event.event_id,
            "session_time_ms": event.session_time_ms,
            "priority": event.priority,
            "sequence": event.sequence,
            "event_type": event.event_type.value,
            "driver_id": None if event.driver_id is None else str(event.driver_id),
            "source": event.source,
            "source_key": event.source_key,
            "payload_json": canonical_event_payload(event.payload),
            "normalization_version": event.normalization_version,
        }
        for event in events
    ]
    return pa.Table.from_pylist(rows, schema=CANONICAL_SCHEMAS[CanonicalTableName.EVENTS])


def events_from_table(table: pa.Table) -> tuple[RaceEvent, ...]:
    if not table.schema.equals(CANONICAL_SCHEMAS[CanonicalTableName.EVENTS], check_metadata=False):
        raise StorageIntegrityError("canonical event schema is incompatible")
    events: list[RaceEvent] = []
    for row in cast(list[dict[str, object]], table.to_pylist()):
        payload_raw = row["payload_json"]
        if not isinstance(payload_raw, str):
            raise StorageIntegrityError("canonical event payload is malformed")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            raise StorageIntegrityError("canonical event payload is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise StorageIntegrityError("canonical event payload must be an object")
        raw_driver = row["driver_id"]
        try:
            event = RaceEvent(
                session_id=SessionId(str(row["session_id"])),
                session_time_ms=cast(int, row["session_time_ms"]),
                sequence=cast(int, row["sequence"]),
                event_type=RaceEventType(str(row["event_type"])),
                driver_id=None if raw_driver is None else DriverId(str(raw_driver)),
                source=cast(str, row["source"]),
                source_key=cast(str | None, row["source_key"]),
                payload=cast(dict[str, object], payload),  # type: ignore[arg-type]
                normalization_version=cast(str, row["normalization_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise StorageIntegrityError("canonical event row is malformed") from exc
        if event.priority != row["priority"] or event.event_id != row["event_id"]:
            raise StorageIntegrityError("canonical event identity failed verification")
        events.append(event)
    if tuple(event.sequence for event in events) != tuple(range(len(events))):
        raise StorageIntegrityError("canonical event sequence is not contiguous")
    if tuple(event.sort_key for event in events) != tuple(
        sorted(event.sort_key for event in events)
    ):
        raise StorageIntegrityError("canonical events are not deterministically ordered")
    return tuple(events)


__all__ = ["events_from_table", "events_to_table"]
