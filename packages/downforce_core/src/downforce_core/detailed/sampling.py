"""Lossless resumable native-table capture for frozen storage-study sessions."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from downforce_core.detailed.audit import write_sample_table
from downforce_core.detailed.status import CapacityStop, filesystem_ancestor, guard_operation
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.layout import ensure_contained, write_text_atomic
from downforce_core.storage.parquet import file_sha256


def capture_native_tables(session: Any, directory: Path) -> dict[str, Any]:
    """Provider-native observations only; missing channels have explicit reasons.

    A failed Parquet conversion invalidates this size measurement rather than
    silently making the sample smaller. A rerun validates existing files before
    reuse and never overwrites a different native observation.
    """
    guard_operation(directory, next_peak_bytes=2 * 1024**3, operation="native capture getters")
    tables: dict[str, Any] = {}
    availability: dict[str, Any] = {}
    failures: list[dict[str, str]] = []

    def write(name: str, native: Any) -> None:
        path = directory / f"{name}.parquet"
        filesystem_ancestor(path)
        if directory.exists():
            path = ensure_contained(path, directory, must_exist=False)
        frame = pd.DataFrame(native)
        if path.exists():
            copy = frame.copy()
            if "source_index" in copy:
                raise ValueError("provider column conflicts with source_index")
            copy.insert(0, "source_index", copy.index)
            expected = pa.Table.from_pandas(copy, preserve_index=False)
            with pq.ParquetFile(path) as reader:
                actual = reader.read()
            if not actual.equals(expected, check_metadata=True):
                raise ValueError("existing native table differs from captured provider data")
            tables[name] = {
                "rows": actual.num_rows,
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
                "columns": actual.column_names,
                "schema": str(actual.schema),
            }
        else:
            try:
                tables[name] = write_sample_table(frame, path)
                with pq.ParquetFile(path) as reader:
                    actual = reader.read()
                if str(actual.schema) != tables[name]["schema"] or actual.num_rows != len(frame):
                    raise ValueError("native schema or row count changed during serialization")
            except (PermissionError, CapacityStop):
                raise
            except Exception as exc:
                failures.append({"channel": name, "error_type": type(exc).__name__})

    for name in (
        "results",
        "laps",
        "weather_data",
        "race_control_messages",
        "session_status",
        "track_status",
    ):
        try:
            native = getattr(session, name)
        except (PermissionError, CapacityStop):
            raise
        except Exception as exc:
            availability[name] = {"state": "unavailable", "reason": type(exc).__name__}
            continue
        write(name, native)
        availability[name] = {"state": "available" if len(native) else "empty-provider-table"}
    for name in ("car_data", "pos_data"):
        try:
            partitions = getattr(session, name)
        except (PermissionError, CapacityStop):
            raise
        except Exception as exc:
            availability[name] = {"state": "unavailable", "reason": type(exc).__name__}
            continue
        if not isinstance(partitions, dict):
            raise ValueError("invalid native driver partition mapping")
        availability[name] = {
            "state": "available" if partitions else "unavailable",
            "reason": None if partitions else "provider returned no driver partitions",
            "drivers": sorted(str(driver) for driver in partitions),
        }
        for driver, native in sorted(partitions.items(), key=lambda item: str(item[0])):
            driver_id = validate_safe_identifier(str(driver))
            if not driver_id.isdigit():
                raise ValueError("invalid provider driver number")
            write(f"{name}/{driver_id}", native)
    try:
        circuit = session.get_circuit_info()
    except (PermissionError, CapacityStop):
        raise
    except Exception as exc:
        availability["corners"] = {"state": "unavailable", "reason": type(exc).__name__}
    else:
        if circuit is None:
            availability["corners"] = {
                "state": "unavailable",
                "reason": "provider returned no circuit information",
            }
        else:
            write("corners", circuit.corners)
            availability["corners"] = {
                "state": "available" if len(circuit.corners) else "empty-provider-table"
            }
    if failures:
        write_text_atomic(directory / "capture-failures.json", json.dumps({"failures": failures}))
        raise ValueError("native table serialization failed; size measurement is incomplete")
    for name in ("results", "laps", "session_status", "track_status"):
        if name not in tables or tables[name]["rows"] == 0:
            raise ValueError(f"required native table unavailable: {name}")
    if sum(table["rows"] for name, table in tables.items() if name.startswith("car_data/")) == 0:
        raise ValueError(
            "car telemetry unavailable; size measurement cannot represent this session"
        )
    totals: Counter[str] = Counter()
    for name, descriptor in tables.items():
        totals[name.split("/")[0]] += descriptor["bytes"]
    return {
        "tables": tables,
        "channel_availability": availability,
        "canonical_sample_bytes": dict(totals),
        "failures": [],
    }
