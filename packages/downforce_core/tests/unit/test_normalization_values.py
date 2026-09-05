from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.domain.enums import DriverStatus, TrackStatus, TyreCompound
from downforce_core.normalization.values import (
    as_bool,
    as_float,
    as_int,
    as_session_time_ms,
    as_text,
    normalize_missing,
    normalize_status,
)


@pytest.mark.parametrize(
    "value",
    [None, pd.NA, pd.NaT, np.nan, np.float64("nan"), pa.scalar(None)],
)
def test_provider_null_variants_become_none(value: object) -> None:
    assert normalize_missing(value) is None


def test_scalar_value_conversion_preserves_zero_and_uses_exact_types() -> None:
    assert as_int("0") == 0
    assert as_float(np.float64(1.25)) == 1.25
    assert as_bool(np.bool_(False)) is False
    assert as_text("VER") == "VER"
    assert as_text("  TRACK LIMITS  ") == "TRACK LIMITS"
    assert as_text(" \t ") is None
    assert as_text("") is None


def test_session_time_conversion_is_half_up_and_rejects_naive_datetimes() -> None:
    origin = datetime(2024, 7, 7, 14, tzinfo=UTC)

    assert as_session_time_ms(timedelta(microseconds=1_500)) == 2
    assert as_session_time_ms(datetime(2024, 7, 7, 14, 0, 0, 1_500, tzinfo=UTC), origin=origin) == 2
    with pytest.raises(ValueError, match="timezone-aware"):
        as_session_time_ms(datetime(2024, 7, 7, 14), origin=origin)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SOFT", TyreCompound.SOFT),
        ("C5", TyreCompound.UNKNOWN),
        (None, TyreCompound.UNKNOWN),
    ],
)
def test_compound_mapping_is_controlled(raw: str | None, expected: TyreCompound) -> None:
    assert TyreCompound.from_raw(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", TrackStatus.CLEAR),
        ("2", TrackStatus.YELLOW),
        ("3", TrackStatus.UNKNOWN),
        ("4", TrackStatus.SAFETY_CAR),
        ("5", TrackStatus.RED_FLAG),
        ("6", TrackStatus.VIRTUAL_SAFETY_CAR),
        ("7", TrackStatus.VSC_ENDING),
        ("9", TrackStatus.UNKNOWN),
    ],
)
def test_all_fastf1_track_status_codes_are_cautiously_mapped(
    raw: str, expected: TrackStatus
) -> None:
    assert TrackStatus.from_raw(raw) is expected


@pytest.mark.parametrize(
    ("classified", "raw_status", "expected"),
    [
        ("1", "Finished", DriverStatus.FINISHED),
        ("R", "Engine", DriverStatus.RETIRED),
        ("D", "Disqualified", DriverStatus.DISQUALIFIED),
        ("E", "Excluded", DriverStatus.DISQUALIFIED),
        ("N", "Not classified", DriverStatus.NOT_CLASSIFIED),
        ("2", "+1 Lap", DriverStatus.FINISHED),
        ("3", "+ 2 Laps", DriverStatus.FINISHED),
        (None, "Engine", DriverStatus.UNKNOWN),
        ("1", "Engine", DriverStatus.UNKNOWN),
    ],
)
def test_classification_status_mapping_uses_only_controlled_evidence(
    classified: str | None,
    raw_status: str | None,
    expected: DriverStatus,
) -> None:
    assert normalize_status(classified, raw_status) is expected
