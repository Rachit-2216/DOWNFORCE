"""Content-addressed immutable snapshots of provider-boundary Arrow tables."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import SchemaVersionError, StorageIntegrityError
from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    ProviderSession,
    ProviderTable,
    SessionRef,
)
from downforce_core.storage.layout import StorageLayout, ensure_contained
from downforce_core.storage.manifest import canonical_json
from downforce_core.storage.schemas import schema_fingerprint
from downforce_core.versions import STORAGE_FORMAT_VERSION

RAW_SNAPSHOT_VERSION = "1.0.0"
_DATETIME_TAG = "$downforce_datetime_utc"


@dataclass(frozen=True, slots=True)
class RawSnapshotResult:
    snapshot_id: str
    session: ProviderSession
    reused: bool


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _encode_metadata(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StorageIntegrityError("raw metadata contains a naive datetime")
        return {_DATETIME_TAG: value.isoformat().replace("+00:00", "Z")}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _encode_metadata(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_metadata(child) for child in value]
    raise StorageIntegrityError(f"raw metadata contains unsupported {type(value).__name__}")


def _decode_metadata(value: object) -> object:
    if isinstance(value, dict):
        if set(value) == {_DATETIME_TAG}:
            raw = value[_DATETIME_TAG]
            if not isinstance(raw, str):
                raise StorageIntegrityError("raw metadata datetime tag is malformed")
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise StorageIntegrityError("raw metadata datetime is malformed") from exc
            if parsed.tzinfo is None:
                raise StorageIntegrityError("raw metadata datetime is timezone-naive")
            return parsed
        return {str(key): _decode_metadata(child) for key, child in value.items()}
    if isinstance(value, list):
        return tuple(_decode_metadata(child) for child in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise StorageIntegrityError("raw metadata value is malformed")


def _arrow_bytes(table: pa.Table) -> bytes:
    canonical = table.combine_chunks().replace_schema_metadata(None)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, canonical.schema) as writer:
        writer.write_table(canonical)
    return cast(bytes, sink.getvalue().to_pybytes())


def _write_bytes_durable(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise StorageIntegrityError(f"raw snapshot file is missing or unsafe: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageIntegrityError(f"raw snapshot JSON is unreadable: {path.name}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StorageIntegrityError(f"raw snapshot JSON is malformed: {path.name}")
    return cast(Mapping[str, object], value)


def _snapshot_identity_payload(
    *,
    canonical_session_id: str,
    session: ProviderSession,
    table_entries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    return {
        "raw_snapshot_version": RAW_SNAPSHOT_VERSION,
        "storage_format_version": STORAGE_FORMAT_VERSION,
        "canonical_session_id": canonical_session_id,
        "provider": session.provider_name,
        "provider_version": session.provider_version,
        "requested_session": {
            "season": session.session.season,
            "event": session.session.event,
            "session": session.session.session.value,
        },
        "provider_metadata": _encode_metadata(session.metadata),
        "tables": table_entries,
    }


def _table_identity_entry(table: ProviderTable, *, digest: str | None) -> dict[str, object]:
    schema = table.data.schema if table.data is not None else None
    return {
        "availability": table.availability.value,
        "error": table.error,
        "row_count": table.data.num_rows if table.data is not None else 0,
        "sha256": digest,
        "schema_fingerprint": schema_fingerprint(schema) if schema is not None else None,
    }


def commit_raw_snapshot(
    layout: StorageLayout,
    canonical_session_id: str,
    session: ProviderSession,
) -> RawSnapshotResult:
    """Commit a provider snapshot, reusing identical content and its original retrieval time."""

    validate_safe_identifier(canonical_session_id, field_name="session_id")
    stage = Path(tempfile.mkdtemp(prefix="raw-", dir=layout.staging_root))
    try:
        tables_directory = stage / "tables"
        tables_directory.mkdir(parents=True)
        identities: dict[str, Mapping[str, object]] = {}
        stored_tables: dict[str, dict[str, object]] = {}
        for name in DatasetName:
            provider_table = session.table(name)
            digest: str | None = None
            relative_path: str | None = None
            if provider_table.data is not None:
                payload = _arrow_bytes(provider_table.data)
                digest = _sha256_bytes(payload)
                relative_path = f"tables/{name.value}.arrow"
                _write_bytes_durable(stage / relative_path, payload)
            identity = _table_identity_entry(provider_table, digest=digest)
            identities[name.value] = identity
            stored_tables[name.value] = {**identity, "path": relative_path}

        identity_payload = _snapshot_identity_payload(
            canonical_session_id=canonical_session_id,
            session=session,
            table_entries=identities,
        )
        snapshot_id = (
            f"snapshot-sha256-{sha256(canonical_json(identity_payload).encode()).hexdigest()}"
        )
        validate_safe_identifier(snapshot_id, field_name="snapshot_id")
        manifest = {
            **identity_payload,
            "snapshot_id": snapshot_id,
            "retrieved_at_utc": session.retrieved_at.isoformat().replace("+00:00", "Z"),
            "tables": stored_tables,
        }
        _write_bytes_durable(
            stage / "snapshot.json",
            (canonical_json(manifest) + "\n").encode("utf-8"),
        )

        target = layout.raw_snapshot(session.provider_name, canonical_session_id, snapshot_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        reused = target.exists()
        if not reused:
            try:
                os.replace(stage, target)
                stage = Path()
            except OSError:
                if not target.exists():
                    raise
                reused = True
        loaded = load_raw_snapshot(layout, session.provider_name, canonical_session_id, snapshot_id)
        return RawSnapshotResult(snapshot_id=snapshot_id, session=loaded, reused=reused)
    finally:
        if stage != Path() and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def load_raw_snapshot(
    layout: StorageLayout,
    provider: str,
    canonical_session_id: str,
    snapshot_id: str,
) -> ProviderSession:
    """Strictly verify and reload an immutable raw provider snapshot."""

    root = layout.raw_snapshot(provider, canonical_session_id, snapshot_id)
    ensure_contained(root, layout.data_root)
    manifest = _read_json(root / "snapshot.json")
    if manifest.get("raw_snapshot_version") != RAW_SNAPSHOT_VERSION:
        raise SchemaVersionError("raw snapshot version is incompatible")
    if manifest.get("storage_format_version") != STORAGE_FORMAT_VERSION:
        raise SchemaVersionError("raw snapshot storage version is incompatible")
    for field_name, expected in (
        ("canonical_session_id", canonical_session_id),
        ("provider", provider),
        ("snapshot_id", snapshot_id),
    ):
        if manifest.get(field_name) != expected:
            raise StorageIntegrityError(f"raw snapshot {field_name} does not match its path")

    tables_raw = manifest.get("tables")
    if not isinstance(tables_raw, dict) or set(tables_raw) != {name.value for name in DatasetName}:
        raise StorageIntegrityError("raw snapshot does not describe every provider table")
    tables: dict[DatasetName, ProviderTable] = {}
    identity_entries: dict[str, Mapping[str, object]] = {}
    for name in DatasetName:
        raw_entry = tables_raw[name.value]
        if not isinstance(raw_entry, dict):
            raise StorageIntegrityError(f"raw table entry is malformed: {name.value}")
        entry = cast(dict[str, object], raw_entry)
        try:
            availability = DatasetAvailability(str(entry.get("availability")))
        except ValueError as exc:
            raise StorageIntegrityError(f"raw table state is invalid: {name.value}") from exc
        raw_path = entry.get("path")
        data: pa.Table | None = None
        if availability in {DatasetAvailability.AVAILABLE, DatasetAvailability.EMPTY}:
            if not isinstance(raw_path, str) or raw_path != f"tables/{name.value}.arrow":
                raise StorageIntegrityError(f"raw table path is invalid: {name.value}")
            table_path = root / raw_path
            ensure_contained(table_path, root)
            payload = table_path.read_bytes()
            if _sha256_bytes(payload) != entry.get("sha256"):
                raise StorageIntegrityError(f"raw table checksum failed: {name.value}")
            try:
                with pa.ipc.open_file(pa.BufferReader(payload)) as reader:
                    data = reader.read_all()
            except (pa.ArrowException, OSError) as exc:
                raise StorageIntegrityError(f"raw Arrow table is unreadable: {name.value}") from exc
            if data.num_rows != entry.get("row_count"):
                raise StorageIntegrityError(f"raw table row count failed: {name.value}")
            if schema_fingerprint(data.schema) != entry.get("schema_fingerprint"):
                raise StorageIntegrityError(f"raw table schema failed: {name.value}")
        elif raw_path is not None or entry.get("sha256") is not None:
            raise StorageIntegrityError(f"unmaterialized raw table claims a file: {name.value}")
        error = entry.get("error")
        tables[name] = ProviderTable(
            name=name,
            availability=availability,
            data=data,
            error=cast(str | None, error),
        )
        identity_entries[name.value] = {
            key: entry.get(key)
            for key in (
                "availability",
                "error",
                "row_count",
                "sha256",
                "schema_fingerprint",
            )
        }

    requested = manifest.get("requested_session")
    metadata = manifest.get("provider_metadata")
    retrieved = manifest.get("retrieved_at_utc")
    if (
        not isinstance(requested, dict)
        or not isinstance(metadata, dict)
        or not isinstance(retrieved, str)
    ):
        raise StorageIntegrityError("raw snapshot metadata is malformed")
    try:
        reference = SessionRef(
            cast(int, requested.get("season")),
            cast(int | str, requested.get("event")),
            cast(str, requested.get("session")),
        )
        retrieved_at = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise StorageIntegrityError("raw snapshot session selector is malformed") from exc

    identity_payload = {
        key: manifest.get(key)
        for key in (
            "raw_snapshot_version",
            "storage_format_version",
            "canonical_session_id",
            "provider",
            "provider_version",
            "requested_session",
            "provider_metadata",
        )
    }
    identity_payload["tables"] = identity_entries
    expected_snapshot_id = (
        f"snapshot-sha256-{sha256(canonical_json(identity_payload).encode()).hexdigest()}"
    )
    if expected_snapshot_id != snapshot_id:
        raise StorageIntegrityError("raw snapshot identity checksum failed")
    provider_version = manifest.get("provider_version")
    if not isinstance(provider_version, str):
        raise StorageIntegrityError("raw provider version is malformed")
    decoded_metadata = _decode_metadata(metadata)
    if not isinstance(decoded_metadata, Mapping):
        raise StorageIntegrityError("raw provider metadata is malformed")
    return ProviderSession(
        session=reference,
        provider_name=provider,
        provider_version=provider_version,
        retrieved_at=retrieved_at,
        metadata=cast(Mapping[str, object], decoded_metadata),
        tables=tables,
    )


__all__ = [
    "RAW_SNAPSHOT_VERSION",
    "RawSnapshotResult",
    "commit_raw_snapshot",
    "load_raw_snapshot",
]
