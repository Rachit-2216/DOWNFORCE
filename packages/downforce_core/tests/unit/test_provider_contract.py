from __future__ import annotations

import inspect
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    LoadOptions,
    ProviderCapabilities,
    ProviderSession,
    ProviderTable,
    RaceDataProvider,
    SessionRef,
    encode_provider_metadata,
    thaw_provider_metadata,
)


def _not_requested_tables() -> dict[DatasetName, ProviderTable]:
    return {
        name: ProviderTable(name=name, availability=DatasetAvailability.NOT_REQUESTED)
        for name in DatasetName
    }


def _provider_session_for_validation(
    *,
    metadata: Mapping[str, object],
    tables: Mapping[DatasetName, ProviderTable],
) -> ProviderSession:
    return ProviderSession(
        session=SessionRef(2024, 1, "R"),
        provider_name="stub",
        provider_version="1.0",
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
        metadata=metadata,
        tables=tables,
    )


class _TypedProviderStub:
    @property
    def name(self) -> str:
        return "typed-stub"

    @property
    def version(self) -> str:
        return "1.0"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            drivers=True,
            laps=True,
            weather=True,
            race_control=True,
            race_positions=True,
            track_positions=False,
            car_telemetry=False,
            live=False,
        )

    async def load_session(
        self, session: SessionRef, options: LoadOptions | None = None
    ) -> ProviderSession:
        return ProviderSession(
            session=session,
            provider_name=self.name,
            provider_version=self.version,
            retrieved_at=datetime(2024, 7, 7, 16, tzinfo=UTC),
            metadata={"event": "British Grand Prix"},
            tables=_not_requested_tables(),
        )


# Mypy validates this structural assignment as part of the root typecheck gate.
_TYPED_PROVIDER_WITNESS: RaceDataProvider = _TypedProviderStub()


def test_race_data_provider_contract_has_a_typechecked_async_witness() -> None:
    assert _TYPED_PROVIDER_WITNESS.name == "typed-stub"
    assert inspect.iscoroutinefunction(_TypedProviderStub.load_session)
    assert inspect.iscoroutinefunction(_TYPED_PROVIDER_WITNESS.load_session)


def test_provider_capabilities_are_explicit_booleans() -> None:
    capabilities = ProviderCapabilities(
        drivers=True,
        laps=True,
        weather=True,
        race_control=False,
        race_positions=True,
        track_positions=False,
        car_telemetry=True,
        live=False,
    )

    assert capabilities.drivers is True
    assert capabilities.laps is True
    assert capabilities.weather is True
    assert capabilities.race_control is False
    assert capabilities.race_positions is True
    assert capabilities.track_positions is False
    assert capabilities.car_telemetry is True
    assert capabilities.live is False

    with pytest.raises(TypeError):
        ProviderCapabilities(
            drivers=True,
            laps=1,  # type: ignore[arg-type]
            weather=True,
            race_control=True,
            race_positions=True,
            track_positions=True,
            car_telemetry=True,
            live=True,
        )


def test_dataset_availability_preserves_available_empty_and_absent_states() -> None:
    available_data = pa.table({"lap_number": [1]})
    empty_data = pa.table({"lap_number": pa.array([], type=pa.int64())})

    available = ProviderTable(
        name=DatasetName.LAPS,
        availability=DatasetAvailability.AVAILABLE,
        data=available_data,
    )
    empty = ProviderTable(
        name=DatasetName.LAPS,
        availability=DatasetAvailability.EMPTY,
        data=empty_data,
    )
    unsupported = ProviderTable(
        name=DatasetName.TRACK_POSITIONS,
        availability=DatasetAvailability.UNSUPPORTED,
    )
    not_requested = ProviderTable(
        name=DatasetName.CAR_TELEMETRY,
        availability=DatasetAvailability.NOT_REQUESTED,
    )
    error = ProviderTable(
        name=DatasetName.WEATHER,
        availability=DatasetAvailability.ERROR,
        error="upstream timeout",
    )

    assert available.data is not None
    assert available.data.to_pydict() == {"lap_number": [1]}
    assert empty.data is not None
    assert empty.data.num_rows == 0
    assert unsupported.data is None and unsupported.error is None
    assert not_requested.data is None and not_requested.error is None
    assert error.data is None and error.error == "upstream timeout"


@pytest.mark.parametrize(
    "table",
    [
        ProviderTable(name=DatasetName.LAPS, availability=DatasetAvailability.NOT_REQUESTED),
    ],
)
def test_provider_table_is_immutable(table: ProviderTable) -> None:
    with pytest.raises((AttributeError, TypeError)):
        table.error = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"availability": DatasetAvailability.AVAILABLE},
        {
            "availability": DatasetAvailability.AVAILABLE,
            "data": pa.table({"x": []}),
        },
        {
            "availability": DatasetAvailability.AVAILABLE,
            "data": pa.table({"x": [1]}),
            "error": "partial failure",
        },
        {"availability": DatasetAvailability.EMPTY},
        {
            "availability": DatasetAvailability.EMPTY,
            "data": pa.table({"x": [1]}),
        },
        {"availability": DatasetAvailability.ERROR},
        {
            "availability": DatasetAvailability.ERROR,
            "data": pa.table({"x": [1]}),
            "error": "failed",
        },
        {
            "availability": DatasetAvailability.UNSUPPORTED,
            "data": pa.table({"x": [1]}),
        },
        {
            "availability": DatasetAvailability.NOT_REQUESTED,
            "error": "not an error",
        },
    ],
)
def test_dataset_availability_enforces_data_and_error_consistency(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProviderTable(name=DatasetName.LAPS, **kwargs)  # type: ignore[arg-type]


def test_provider_table_rejects_provider_specific_or_mutable_tabular_objects() -> None:
    with pytest.raises(TypeError):
        ProviderTable(
            name=DatasetName.LAPS,
            availability=DatasetAvailability.AVAILABLE,
            data=[{"lap_number": 1}],
        )


def test_provider_table_owns_arrow_buffers_after_one_boundary_copy() -> None:
    source_values = np.array([1, 2], dtype=np.int64)
    source_table = pa.table({"value": source_values})
    provider_table = ProviderTable(
        name=DatasetName.LAPS,
        availability=DatasetAvailability.AVAILABLE,
        data=source_table,
    )

    source_values[0] = 99

    assert source_table.column("value")[0].as_py() == 99
    assert provider_table.data is not None
    assert provider_table.data.column("value")[0].as_py() == 1


def test_provider_session_freezes_metadata_and_table_mappings() -> None:
    retrieved_at = datetime(2024, 7, 7, 21, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    metadata: dict[str, object] = {
        "event": "British Grand Prix",
        "nested": {
            "tags": ["historical", "race"],
            "captured_at": retrieved_at,
            "complete": True,
            "count": 2,
            "ratio": 0.5,
            "missing": None,
        },
    }
    tables = _not_requested_tables()

    provider_session = ProviderSession(
        session=SessionRef(2024, "British Grand Prix", "R"),
        provider_name="stub",
        provider_version="1.0",
        retrieved_at=retrieved_at,
        metadata=metadata,
        tables=tables,
    )

    metadata["event"] = "mutated"
    tables[DatasetName.LAPS] = ProviderTable(
        name=DatasetName.LAPS,
        availability=DatasetAvailability.ERROR,
        error="mutated",
    )

    assert provider_session.provider_name == "stub"
    assert provider_session.provider_version == "1.0"
    assert provider_session.retrieved_at == datetime(2024, 7, 7, 16, tzinfo=UTC)
    assert provider_session.retrieved_at.tzinfo is UTC
    assert provider_session.metadata["event"] == "British Grand Prix"
    assert provider_session.metadata["nested"] == {
        "tags": ("historical", "race"),
        "captured_at": datetime(2024, 7, 7, 16, tzinfo=UTC),
        "complete": True,
        "count": 2,
        "ratio": 0.5,
        "missing": None,
    }
    assert (
        provider_session.tables[DatasetName.LAPS].availability is DatasetAvailability.NOT_REQUESTED
    )

    with pytest.raises(TypeError):
        provider_session.metadata["event"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        provider_session.tables[DatasetName.LAPS] = tables[DatasetName.LAPS]  # type: ignore[index]

    thawed = thaw_provider_metadata(provider_session.metadata)
    assert thawed == {
        "event": "British Grand Prix",
        "nested": {
            "tags": ["historical", "race"],
            "captured_at": "2024-07-07T16:00:00Z",
            "complete": True,
            "count": 2,
            "ratio": 0.5,
            "missing": None,
        },
    }
    assert json.loads(encode_provider_metadata(provider_session.metadata)) == thawed

    nested = thawed["nested"]
    assert isinstance(nested, dict)
    tags = nested["tags"]
    assert isinstance(tags, list)
    tags.append("mutated")
    frozen_nested = provider_session.metadata["nested"]
    assert isinstance(frozen_nested, Mapping)
    frozen_nested_mapping: Mapping[str, object] = frozen_nested
    assert frozen_nested_mapping["tags"] == ("historical", "race")
    with pytest.raises(TypeError):
        frozen_nested_mapping["count"] = 3  # type: ignore[index]


def test_provider_session_requires_complete_dataset_statuses_and_safe_metadata() -> None:
    with pytest.raises(ValueError, match="every dataset"):
        _provider_session_for_validation(metadata={}, tables={})
    with pytest.raises(TypeError, match="metadata"):
        _provider_session_for_validation(
            metadata={"provider_object": object()},
            tables=_not_requested_tables(),
        )
    with pytest.raises(ValueError, match="finite"):
        _provider_session_for_validation(
            metadata={"bad": float("nan")},
            tables=_not_requested_tables(),
        )


@pytest.mark.parametrize("bad", [Path("provider.cache"), object()])
def test_metadata_encoder_rejects_path_and_provider_objects_without_access(bad: object) -> None:
    with pytest.raises(TypeError, match="unsupported type"):
        thaw_provider_metadata({"bad": bad})
    with pytest.raises(TypeError, match="unsupported type"):
        encode_provider_metadata({"bad": bad})


def test_metadata_encoder_rejects_nonfinite_and_naive_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        encode_provider_metadata({"bad": float("inf")})
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_provider_metadata({"bad": datetime(2024, 1, 1)})


def test_provider_session_rejects_naive_retrieval_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ProviderSession(
            session=SessionRef(2024, 1, "R"),
            provider_name="stub",
            provider_version="1.0",
            retrieved_at=datetime(2024, 1, 1),
            metadata={},
            tables=_not_requested_tables(),
        )


def test_importing_domain_and_provider_contract_does_not_import_fastf1() -> None:
    script = """
import sys
assert not any(name == 'fastf1' or name.startswith('fastf1.') for name in sys.modules)
import downforce_core.domain
import downforce_core.providers.base
assert not any(name == 'fastf1' or name.startswith('fastf1.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
