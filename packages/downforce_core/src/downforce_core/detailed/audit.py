"""Reproducible inventory and columnar storage measurements, before bulk ingestion."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.catalog import discover_schedule
from downforce_core.detailed.status import guard_operation, parquet_peak_bytes


def inventory(
    fetch_schedule: Callable[[int], Iterable[Mapping[str, object]]],
    *,
    first_year: int = 2018,
    now: datetime,
) -> dict[str, Any]:
    if now.tzinfo is None or not 2018 <= first_year <= now.year:
        raise ValueError("invalid discovery range or timezone")
    sessions: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for year in range(first_year, now.year + 1):
        try:
            discovered = discover_schedule(year, fetch_schedule(year), now=now)
            if not discovered:
                raise ValueError("empty provider schedule")
            sessions.extend(s.to_dict() for s in discovered)
        except Exception as exc:
            # One provider year must not erase all successfully discovered seasons.
            failures.append({"season": year, "error_type": type(exc).__name__})
    counts = Counter(str(s["completion"]) for s in sessions)
    groups = Counter((int(str(s["season"])), str(s["session_type"])) for s in sessions)
    return {
        "schema_version": "detailed-discovery-v1",
        "discovered_at": now.isoformat(),
        "first_year": first_year,
        "last_year": now.year,
        "complete_inventory": not failures,
        "failed_years": [f["season"] for f in failures],
        "failures": failures,
        "summary": {
            "sessions": len(sessions),
            "weekends": len({str(s["event_id"]) for s in sessions}),
            "completed": counts["completed"],
            "past-unverified": counts["past-unverified"],
            "future": counts["future"],
            "unknown": counts["unknown"],
        },
        "by_season_type": [
            {"season": year, "session_type": category, "count": count}
            for (year, category), count in sorted(groups.items())
        ],
        "sessions": sessions,
    }


def write_sample_table(frame: pd.DataFrame, path: Path) -> dict[str, Any]:
    """Measure unsampled provider tables with native temporal/discrete types.

    Preflight evidence only; this is not the canonical publication boundary.
    Exclusive creation prevents accidentally changing a prior measured sample.
    """
    copy = pd.DataFrame(frame).copy()
    if "source_index" in copy.columns:
        raise ValueError("provider column conflicts with source_index")
    copy.insert(0, "source_index", copy.index)
    table = pa.Table.from_pandas(copy, preserve_index=False)
    if path.exists():
        raise FileExistsError(path)
    guard_operation(
        path, next_peak_bytes=parquet_peak_bytes(table), operation="native Parquet capture"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    primary_denial: PermissionError | None = None
    published = False
    stage = "write"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        ) as handle:
            temporary = Path(handle.name)
            pq.write_table(
                table,
                handle,
                compression="zstd",
                row_group_size=8192,
                use_compliant_nested_type=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        stage = "read-after-write validation"
        # Own the reader lifetime explicitly: no reader survives publication or
        # cleanup on Windows. An external sharing violation still stops the run.
        with pq.ParquetFile(temporary) as reader:
            if not reader.read().equals(table, check_metadata=True):
                raise ValueError("native Parquet round trip changed source values or schema")
        # Atomic exclusive publication on both Windows and POSIX. Unlike rename
        # on POSIX, a concurrent existing destination can never be overwritten.
        stage = "exclusive hardlink publication"
        os.link(temporary, path)
        published = True
    except PermissionError as exc:
        primary_denial = exc
        exc.add_note(f"native sample {stage}: temporary={temporary}; destination={path}")
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception as exc:
                context = (
                    f"native sample cleanup: temporary={temporary}; destination={path}; "
                    f"published={published}; prior_stage={stage}"
                )
                if primary_denial is None:
                    exc.add_note(context)
                    raise
                primary_denial.add_note(
                    f"{context}; secondary_error={exc!r}; "
                    f"winerror={getattr(exc, 'winerror', None)}; "
                    f"filename={getattr(exc, 'filename', None)}"
                )
    return {
        "rows": table.num_rows,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "columns": table.column_names,
        "schema": str(table.schema),
    }
