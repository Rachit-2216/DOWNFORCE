"""Arrow-native normalization for dense geometric track-position samples."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from downforce_core.domain.identifiers import DriverId, make_driver_id
from downforce_core.domain.models import SessionMetadata
from downforce_core.normalization.models import CanonicalTrackPositions
from downforce_core.normalization.values import as_float
from downforce_core.providers.base import DatasetAvailability, DatasetName, ProviderSession

_OBSERVED_SOURCE = "pos"


def _empty(session: ProviderSession, metadata: SessionMetadata) -> CanonicalTrackPositions:
    return CanonicalTrackPositions.empty(
        session_id=metadata.session_id,
        provider_name=session.provider_name,
        provider_version=session.provider_version,
        retrieved_at=session.retrieved_at,
        source=f"{session.provider_name}.track-positions",
    )


def _column_or_null(table: pa.Table, name: str, data_type: pa.DataType) -> pa.ChunkedArray:
    if name not in table.column_names:
        return pa.chunked_array([pa.nulls(table.num_rows, type=data_type)])
    return table.column(name)


def _duration_to_milliseconds(column: pa.ChunkedArray) -> pa.ChunkedArray:
    data_type = column.type
    if pa.types.is_duration(data_type):
        units_per_millisecond = {
            "s": 1 / 1_000,
            "ms": 1,
            "us": 1_000,
            "ns": 1_000_000,
        }[data_type.unit]
        numeric = pc.cast(column, pa.int64())
        if units_per_millisecond == 1:
            return numeric
        scaled = pc.divide(numeric, units_per_millisecond)
        return pc.cast(pc.round(scaled, ndigits=0, round_mode="half_up"), pa.int64())
    if pa.types.is_integer(data_type):
        return pc.cast(column, pa.int64())
    if pa.types.is_floating(data_type):
        return pc.cast(pc.round(column, ndigits=0, round_mode="half_up"), pa.int64())
    raise TypeError(f"track position time column has unsupported type {data_type}")


def _timestamp_to_session_milliseconds(
    column: pa.ChunkedArray,
    origin: datetime | None,
) -> pa.ChunkedArray:
    if origin is None:
        return pa.chunked_array([pa.nulls(len(column), type=pa.int64())])
    data_type = column.type
    if not pa.types.is_timestamp(data_type):
        raise TypeError(f"track position date column has unsupported type {data_type}")
    units_per_millisecond = {
        "s": 1 / 1_000,
        "ms": 1,
        "us": 1_000,
        "ns": 1_000_000,
    }[data_type.unit]
    epoch_values = pc.cast(column, pa.int64())
    epoch_milliseconds = (
        epoch_values
        if units_per_millisecond == 1
        else pc.cast(
            pc.round(
                pc.divide(epoch_values, units_per_millisecond),
                ndigits=0,
                round_mode="half_up",
            ),
            pa.int64(),
        )
    )
    origin_milliseconds = int(origin.timestamp() * 1_000)
    return pc.subtract(epoch_milliseconds, origin_milliseconds)


def _session_time_column(table: pa.Table, metadata: SessionMetadata) -> pa.ChunkedArray:
    candidates: list[pa.ChunkedArray] = []
    for name in ("session_time", "time"):
        if name in table.column_names:
            candidates.append(_duration_to_milliseconds(table.column(name)))
    if "date" in table.column_names:
        candidates.append(
            _timestamp_to_session_milliseconds(
                table.column("date"),
                metadata.session_origin_utc,
            )
        )
    if not candidates:
        raise ValueError("track-position table has no documented time column")
    return pc.coalesce(*candidates)


def _normalized_source(table: pa.Table) -> pa.ChunkedArray:
    source = _column_or_null(table, "source", pa.string())
    if pa.types.is_dictionary(source.type):
        source = pc.cast(source, pa.string())
    elif not pa.types.is_string(source.type):
        source = pc.cast(source, pa.string())
    return pc.utf8_lower(pc.utf8_trim_whitespace(source))


def _warn_rejected_sources(
    source: pa.ChunkedArray,
    observed_mask: pa.ChunkedArray,
    warnings: list[str],
) -> None:
    rejected = pc.filter(pc.fill_null(source, "<missing>"), pc.invert(observed_mask))
    if not len(rejected):
        return
    counts = pc.value_counts(rejected).to_pylist()
    for item in sorted(counts, key=lambda value: str(value["values"])):
        warnings.append(
            "track_positions.non-observed-source: "
            f"source={item['values']},samples={item['counts']}: samples omitted"
        )


def _all_valid(*columns: pa.ChunkedArray) -> pa.ChunkedArray:
    mask = pc.is_valid(columns[0])
    for column in columns[1:]:
        mask = pc.and_(mask, pc.is_valid(column))
    return mask


def _equal_with_nulls(
    left: pa.ChunkedArray,
    right: pa.ChunkedArray,
) -> pa.ChunkedArray:
    both_null = pc.and_(pc.is_null(left), pc.is_null(right))
    return pc.fill_null(pc.or_(pc.equal(left, right), both_null), False)


def _dedupe_sorted(table: pa.Table, warnings: list[str]) -> pa.Table:
    if table.num_rows < 2:
        return table
    driver = table.column("driver_number")
    times = table.column("session_time_ms")
    same_key = pc.and_(
        pc.equal(driver.slice(1), driver.slice(0, table.num_rows - 1)),
        pc.equal(times.slice(1), times.slice(0, table.num_rows - 1)),
    )
    same_payload = pc.and_(
        _equal_with_nulls(
            table.column("x_m").slice(1),
            table.column("x_m").slice(0, table.num_rows - 1),
        ),
        _equal_with_nulls(
            table.column("y_m").slice(1),
            table.column("y_m").slice(0, table.num_rows - 1),
        ),
    )
    for name in ("z_m", "raw_status"):
        same_payload = pc.and_(
            same_payload,
            _equal_with_nulls(
                table.column(name).slice(1),
                table.column(name).slice(0, table.num_rows - 1),
            ),
        )
    conflict_count = pc.sum(pc.and_(same_key, pc.invert(same_payload))).as_py() or 0
    if conflict_count:
        warnings.append(
            f"track_positions.conflicting-duplicate: adjacent_conflicts={conflict_count}"
        )
    keep = pa.chunked_array(
        [
            pa.array([True], type=pa.bool_()),
            pc.invert(same_key).combine_chunks(),
        ]
    )
    return table.filter(keep)


def _canonical_driver_ids(
    numbers: pa.ChunkedArray,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
) -> pa.DictionaryArray:
    unique_numbers = sorted(int(value) for value in pc.unique(numbers).to_pylist())
    values = pa.array(unique_numbers, type=pa.int64())
    indices = pc.index_in(numbers, value_set=values).combine_chunks()
    dictionary = pa.array(
        [
            str(driver_ids.get(number, make_driver_id(metadata.session_id, number)))
            for number in unique_numbers
        ],
        type=pa.string(),
    )
    return pa.DictionaryArray.from_arrays(indices, dictionary)


def normalize_track_positions(
    session: ProviderSession,
    metadata: SessionMetadata,
    driver_ids: Mapping[int, DriverId],
    warnings: list[str],
) -> CanonicalTrackPositions:
    """Normalize dense samples with Arrow kernels and no whole-table Python rows."""

    provider_table = session.table(DatasetName.TRACK_POSITIONS)
    if provider_table.availability is DatasetAvailability.ERROR:
        warnings.append(f"track-positions.provider-error: {provider_table.error}")
    raw = provider_table.data
    if raw is None or raw.num_rows == 0:
        return _empty(session, metadata)
    scale = as_float(session.metadata.get("coordinate_scale_to_m"))
    if scale is None or scale <= 0:
        warnings.append("track_positions.unverified-coordinate-scale: samples omitted")
        return _empty(session, metadata)
    required = {"driver_number", "x", "y", "source"}
    missing = sorted(required - set(raw.column_names))
    if missing:
        raise ValueError(f"track-position table omitted {', '.join(missing)}")

    source = _normalized_source(raw)
    observed_mask = pc.fill_null(pc.equal(source, _OBSERVED_SOURCE), False)
    _warn_rejected_sources(source, observed_mask, warnings)
    observed = raw.filter(observed_mask)
    if observed.num_rows == 0:
        return _empty(session, metadata)

    driver_numbers = pc.cast(observed.column("driver_number"), pa.int64())
    session_times = _session_time_column(observed, metadata)
    x_values = pc.cast(observed.column("x"), pa.float64())
    y_values = pc.cast(observed.column("y"), pa.float64())
    z_values = pc.cast(_column_or_null(observed, "z", pa.float64()), pa.float64())
    raw_status = _column_or_null(observed, "status", pa.string())
    if pa.types.is_dictionary(raw_status.type):
        raw_status = pc.cast(raw_status, pa.string())
    elif not pa.types.is_string(raw_status.type):
        raw_status = pc.cast(raw_status, pa.string())

    valid = _all_valid(driver_numbers, session_times, x_values, y_values)
    valid = pc.and_(valid, pc.greater_equal(session_times, 0))
    discarded = observed.num_rows - (pc.sum(pc.cast(valid, pa.int64())).as_py() or 0)
    if discarded:
        warnings.append(f"track_positions.incomplete-sample: samples={discarded} omitted")
    driver_numbers = pc.filter(driver_numbers, valid)
    session_times = pc.filter(session_times, valid)
    x_values = pc.multiply(pc.filter(x_values, valid), scale)
    y_values = pc.multiply(pc.filter(y_values, valid), scale)
    z_values = pc.multiply(pc.filter(z_values, valid), scale)
    raw_status = pc.filter(raw_status, valid)
    if len(driver_numbers) == 0:
        return _empty(session, metadata)
    for name, values in (("x", x_values), ("y", y_values), ("z", z_values)):
        finite_or_null = pc.fill_null(pc.is_finite(values), True)
        if not bool(pc.all(finite_or_null).as_py()):
            raise ValueError(f"track-position {name} contains a nonfinite coordinate")

    working = pa.table(
        {
            "driver_number": driver_numbers,
            "session_time_ms": session_times,
            "x_m": x_values,
            "y_m": y_values,
            "z_m": z_values,
            "raw_status": raw_status,
        }
    ).sort_by(
        [
            ("session_time_ms", "ascending"),
            ("driver_number", "ascending"),
            ("x_m", "ascending"),
            ("y_m", "ascending"),
            ("z_m", "ascending"),
            ("raw_status", "ascending"),
        ]
    )
    working = _dedupe_sorted(working, warnings)
    canonical = pa.table(
        {
            "driver_id": _canonical_driver_ids(
                working.column("driver_number"), metadata, driver_ids
            ),
            "session_time_ms": working.column("session_time_ms"),
            "x_m": working.column("x_m"),
            "y_m": working.column("y_m"),
            "z_m": working.column("z_m"),
            "raw_status": pc.dictionary_encode(working.column("raw_status")),
        }
    )
    return CanonicalTrackPositions(
        session_id=metadata.session_id,
        table=canonical,
        provider_name=session.provider_name,
        provider_version=session.provider_version,
        retrieved_at=session.retrieved_at,
        source=f"{session.provider_name}.track-positions",
    )


__all__ = ["normalize_track_positions"]
