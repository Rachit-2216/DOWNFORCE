import json
from pathlib import Path

import pandas as pd
import pytest
from downforce_core.detailed.audit import write_sample_table
from downforce_core.detailed.measurement import measure_candidate
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest


def test_candidate_measurement_uses_actual_canonical_bytes_and_keeps_source(tmp_path: Path) -> None:
    root = tmp_path / ".downforce" / "detailed"
    identity = "2018-01-practice-1"
    attempt = root / "preflight" / identity / "attempt"
    cache = attempt / "raw-cache"
    cache.mkdir(parents=True)
    (cache / "provider-cache.bin").write_bytes(b"evidence" * 100)
    original = source_manifest(cache)
    stats = write_sample_table(
        pd.DataFrame({"Speed": [100.0, None]}), attempt / "car_data/44.parquet"
    )
    measurement = {
        "session": {"session_id": identity, "season": 2018, "session_type": "practice-1"},
        "tables": {"car_data/44": stats},
        "fastf1_version": "test",
        "raw_bytes": 800,
    }
    for path in [attempt / "measurement.json", attempt.parent / "measurement.json"]:
        path.write_text(json.dumps(measurement))
    store = RawEvidenceStore(root / "study" / "raw-bundles")
    packed = store.pack(cache, session_id=identity, provider="fastf1", provider_version="test")
    report = {
        "round_trip_exact": True,
        "bundle_sha256": packed,
        "observed_peak_temporary_bytes": 2000,
    }
    result = measure_candidate(tmp_path, root, measurement, report)
    assert result["canonical_bytes"] > 0
    assert result["metadata_bytes"] > 0
    assert result["raw_evidence_bytes"] == (store.root / f"{packed}.zip").stat().st_size
    assert source_manifest(cache) == original
    assert measure_candidate(tmp_path, root, measurement, report) == result
    # A 20 ms observer can miss a tiny fixture's transient peak. Never authorize
    # working space below the deterministic source-plus-archive requirement.
    underestimated = measure_candidate(
        tmp_path, root, measurement, {**report, "observed_peak_temporary_bytes": 0}
    )
    assert underestimated["temporary_peak_bytes"] >= (
        800 + underestimated["raw_evidence_bytes"] + 2 * underestimated["canonical_bytes"]
    )


def test_candidate_rechecks_space_before_measurement_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from downforce_core.detailed import status

    root = tmp_path / "detailed"
    identity = "2018-01-practice-1"
    attempt = root / "preflight" / identity / "attempt"
    cache = attempt / "raw-cache"
    cache.mkdir(parents=True)
    (cache / "raw").write_bytes(b"evidence")
    stats = write_sample_table(pd.DataFrame({"Speed": [1.25]}), attempt / "car_data/44.parquet")
    measurement = {
        "session": {"session_id": identity, "season": 2018, "session_type": "practice-1"},
        "tables": {"car_data/44": stats},
        "fastf1_version": "test",
    }
    (attempt / "measurement.json").write_text(json.dumps(measurement))
    store = RawEvidenceStore(root / "study" / "raw-bundles")
    bundle = store.pack(cache, session_id=identity, provider="fastf1", provider_version="test")
    frees = iter([10**12, 30 * 1024**3])
    monkeypatch.setattr(status.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(frees)))
    with pytest.raises(status.CapacityStop):
        measure_candidate(
            tmp_path,
            root,
            measurement,
            {"round_trip_exact": True, "bundle_sha256": bundle, "observed_peak_temporary_bytes": 0},
        )
    assert not (root / "study" / "session-measurements").exists()
