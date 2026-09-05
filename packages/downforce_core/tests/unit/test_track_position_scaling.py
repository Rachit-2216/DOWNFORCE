from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime

import downforce_core.providers.fastf1_tables as fastf1_tables
import numpy as np
import pandas as pd
import pytest
from downforce_core.domain import (
    SessionMetadata,
    SessionType,
    SourceProvenance,
    make_driver_id,
    make_session_id,
)
from downforce_core.normalization.models import CanonicalTrackPositions
from downforce_core.normalization.track_positions import normalize_track_positions
from downforce_core.providers import (
    DatasetAvailability,
    DatasetName,
    ProviderSession,
    ProviderTable,
    SessionRef,
)


def test_dense_track_positions_remain_arrow_native_under_realistic_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples_per_driver = 25_000
    sample_index = np.arange(samples_per_driver, dtype=np.int64)

    def frame(offset: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "SessionTime": pd.to_timedelta(sample_index * 100, unit="ms"),
                "X": sample_index.astype(np.float64) + offset,
                "Y": sample_index.astype(np.float64) - offset,
                "Z": np.zeros(samples_per_driver, dtype=np.float64),
                "Status": pd.Series("OnTrack", index=range(samples_per_driver)),
                "Source": pd.Series("pos", index=range(samples_per_driver)),
            }
        )

    session_source = type(
        "PositionSession",
        (),
        {"pos_data": {"0": frame(0.0), "7": frame(10.0)}},
    )()

    def forbidden_materialization(*_: object, **__: object) -> object:
        raise AssertionError("dense position path used a forbidden pandas materialization")

    monkeypatch.setattr(pd, "concat", forbidden_materialization)
    monkeypatch.setattr(pd.DataFrame, "copy", forbidden_materialization)
    monkeypatch.setattr(fastf1_tables, "_table_from_frame", forbidden_materialization)

    warnings: list[str] = []
    tracemalloc.start()
    provider_positions = fastf1_tables._extract_track_positions(session_source, warnings)

    tables = {
        name: ProviderTable(name=name, availability=DatasetAvailability.NOT_REQUESTED)
        for name in DatasetName
    }
    tables[DatasetName.TRACK_POSITIONS] = provider_positions
    provider_session = ProviderSession(
        session=SessionRef(2024, 1, "R"),
        provider_name="fastf1",
        provider_version="3.8.3",
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
        metadata={"coordinate_scale_to_m": 0.1},
        tables=tables,
    )
    session_id = make_session_id(2024, "Scale Test Grand Prix", SessionType.RACE)
    provenance = SourceProvenance(
        provider="fixture",
        provider_version="1.0",
        source="fixture",
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    metadata = SessionMetadata(
        session_id=session_id,
        season=2024,
        event_name="Scale Test Grand Prix",
        session_name="Race",
        session_type=SessionType.RACE,
        provenance=provenance,
    )
    driver_ids = {number: make_driver_id(session_id, number) for number in (0, 7)}
    canonical = normalize_track_positions(
        provider_session,
        metadata,
        driver_ids,
        warnings,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert isinstance(canonical, CanonicalTrackPositions)
    assert len(canonical) == 50_000
    assert canonical.table.column_names == [
        "driver_id",
        "session_time_ms",
        "x_m",
        "y_m",
        "z_m",
        "raw_status",
    ]
    assert canonical.nbytes < 8 * 1024 * 1024
    assert peak < 64 * 1024 * 1024
    assert canonical[0].session_time_ms == 0
    assert canonical[-1].session_time_ms == 2_499_900
    assert canonical[0].provenance.source_record_id is not None
    assert warnings == []
