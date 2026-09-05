"""Small deterministic row/provenance helpers shared by normalizers."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from typing import cast

from downforce_core.domain.models import SourceProvenance
from downforce_core.domain.time import duration_to_milliseconds, ensure_utc
from downforce_core.normalization.values import normalize_missing
from downforce_core.providers.base import DatasetAvailability, DatasetName, ProviderSession

type RawRow = Mapping[str, object]
type RowKey = tuple[object, ...]


def _stable_value(value: object) -> object:
    normalized = normalize_missing(value)
    if normalized is None or isinstance(normalized, (str, bool, int)):
        return normalized
    if isinstance(normalized, float):
        return normalized
    if isinstance(normalized, datetime):
        return ensure_utc(normalized).isoformat().replace("+00:00", "Z")
    if isinstance(normalized, timedelta):
        return {"duration_ms": duration_to_milliseconds(normalized, allow_negative=True)}
    if isinstance(normalized, Mapping):
        return {str(key): _stable_value(child) for key, child in sorted(normalized.items())}
    if isinstance(normalized, (list, tuple)):
        return [_stable_value(child) for child in normalized]
    return str(normalized)


def stable_json(value: object) -> str:
    return json.dumps(
        _stable_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_record_id(source: str, value: object) -> str:
    digest = sha256(f"{source}\0{stable_json(value)}".encode()).hexdigest()
    return f"record-{digest}"


def provenance(session: ProviderSession, source: str, value: object) -> SourceProvenance:
    return SourceProvenance(
        provider=session.provider_name,
        provider_version=session.provider_version,
        source=source,
        retrieved_at=session.retrieved_at,
        source_record_id=stable_record_id(source, value),
    )


def rows_for(
    session: ProviderSession,
    name: DatasetName,
    warnings: list[str],
) -> list[dict[str, object]]:
    table = session.table(name)
    if table.availability is DatasetAvailability.ERROR:
        warnings.append(f"{name.value}.provider-error: {table.error}")
        return []
    if table.data is None:
        return []
    return cast(list[dict[str, object]], table.data.to_pylist())


def dedupe_rows(
    rows: Sequence[dict[str, object]],
    *,
    key: Callable[[RawRow], RowKey],
    table: str,
    warnings: list[str],
) -> list[dict[str, object]]:
    """Drop exact duplicates and resolve conflicting canonical keys by stable row order."""

    grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for row in rows:
        key_text = stable_json(key(row))
        grouped.setdefault(key_text, []).append((stable_json(row), row))

    selected: list[tuple[str, str, dict[str, object]]] = []
    for key_text, candidates in grouped.items():
        by_payload = {payload: row for payload, row in candidates}
        payload = min(by_payload)
        if len(by_payload) > 1:
            warnings.append(f"{table}.conflicting-duplicate: key={key_text}")
        selected.append((key_text, payload, by_payload[payload]))
    selected.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in selected]


__all__ = [
    "RawRow",
    "dedupe_rows",
    "provenance",
    "rows_for",
    "stable_json",
    "stable_record_id",
]
