"""A corpus-wide download needs measured evidence and disk headroom."""

import pytest
from downforce_core.detailed.preflight import StorageSample, measurement_sample, project_storage


def test_no_samples_fail_closed() -> None:
    with pytest.raises(ValueError, match="sample"):
        project_storage([], candidate_sessions=1000, free_bytes=100_000)


def test_projection_uses_upper_sample_envelope_and_explicit_reserve() -> None:
    samples = [
        StorageSample(2018, "practice-1", 100, {"car": 30, "position": 20}),
        StorageSample(2024, "race", 200, {"car": 60, "position": 40}),
    ]
    result = project_storage(
        samples, candidate_sessions=100, free_bytes=50_000, reserve_bytes=10_000
    )
    assert result.projected_bytes == 37_500  # max measured total * 100 * 1.25
    assert result.safe_to_sync
    assert result.largest_source == "raw_cache"
    blocked = project_storage(
        samples, candidate_sessions=100, free_bytes=40_000, reserve_bytes=10_000
    )
    assert not blocked.safe_to_sync


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_invalid_measurements_cannot_authorize_download(bad: float) -> None:
    with pytest.raises(ValueError):
        StorageSample(2018, "race", bad, {})  # type: ignore[arg-type]


def test_missing_required_eras_blocks_even_when_disk_has_space() -> None:
    result = project_storage(
        [StorageSample(2018, "race", 1, {"laps": 1})],
        candidate_sessions=10,
        free_bytes=10**12,
        required_years={2018, 2020, 2022, 2024, 2026},
    )
    assert not result.safe_to_sync
    assert result.missing_years == [2020, 2022, 2024, 2026]


def test_optional_corner_absence_does_not_invalidate_measured_storage() -> None:
    measured = {
        "session": {"season": 2018, "session_type": "practice-1"},
        "raw_bytes": 1000,
        "canonical_sample_bytes": {"laps": 20, "car_data": 50},
        "completion_confirmed": True,
        "failures": [{"channel": "corners", "error_type": "KeyError"}],
    }
    assert measurement_sample(measured).total_bytes == 1070
    measured["failures"] = [{"channel": "car_data/44", "error_type": "ArrowInvalid"}]
    with pytest.raises(ValueError, match="incomplete"):
        measurement_sample(measured)


def test_unconfirmed_session_measurement_cannot_authorize_bulk_download() -> None:
    with pytest.raises(ValueError, match="completion"):
        measurement_sample({"completion_confirmed": False})
