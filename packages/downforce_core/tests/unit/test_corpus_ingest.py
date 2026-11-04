"""Production ingestion exercises real Arrow, immutable stores and study proofs."""

import importlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.detailed.cli import project_study
from downforce_core.detailed.evidence import schedule_sessions, schedule_timing_paths, schedule_url
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.raw_evidence import source_manifest
from downforce_core.detailed.status import CapacityStop
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.parquet import file_sha256
from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]

cache = importlib.import_module("test_detailed_evidence").cache
response = importlib.import_module("test_detailed_evidence").response
schedule_fixture = importlib.import_module("test_detailed_evidence").schedule_fixture
study = importlib.import_module("test_detailed_proofs").study
native_session = importlib.import_module("test_detailed_study_runner").native_session
save = importlib.import_module("test_detailed_study_runner").save


def module() -> Any:
    name = "downforce_core.detailed.corpus_ingest"
    assert importlib.util.find_spec(name) is not None, "production corpus ingestion is missing"
    return importlib.import_module(name)


def test_selection_is_all_completed_sorted_and_filtered() -> None:
    rows = [
        {"session_id": f"{year}-01-{kind}", "season": year, "session_type": kind, "state": state}
        for year, kind, state in [
            (2024, "race", "completed"),
            (2018, "practice-1", "completed"),
            (2024, "practice-1", "completed"),
            (2025, "race", "future"),
            (2023, "race", "cancelled"),
        ]
    ]
    select = module().select_sessions
    assert [row["session_id"] for row in select(rows)] == [
        "2018-01-practice-1",
        "2024-01-practice-1",
        "2024-01-race",
    ]
    assert len(select(rows, season=2024)) == 2
    assert len(select(rows, from_season=2020, session_types={"race"})) == 1
    with pytest.raises(ValueError, match="exclusive"):
        select(rows, season=2024, from_season=2020)


def test_session_identity_selection_is_explicit_and_fail_closed() -> None:
    rows = [
        {
            "session_id": "2018-01-race",
            "season": 2018,
            "session_type": "race",
            "state": "completed",
        },
        {
            "session_id": "2024-01-race",
            "season": 2024,
            "session_type": "race",
            "state": "completed",
        },
        {"session_id": "2026-01-race", "season": 2026, "session_type": "race", "state": "future"},
    ]
    select = module().select_sessions
    assert select(rows, session_ids={"2024-01-race"}) == [rows[1]]
    assert select(rows, session_ids={"2024-01-race"}, session_types={"race"}, season=2024) == [
        rows[1]
    ]
    for requested in ({"unknown"}, {"2026-01-race"}, {"2024-01-race", "unknown"}):
        with pytest.raises(ValueError, match="requested session"):
            select(rows, session_ids=requested)
    with pytest.raises(ValueError, match="requested session"):
        select(rows, session_ids={"2024-01-race"}, season=2018)


def test_run_corpus_exact_single_identity_does_not_load_other_completed_sessions(
    tmp_path: Path,
) -> None:
    paths = ready(tmp_path, additional=2)
    calls: list[tuple[str, bool]] = []

    def only(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        calls.append((entry["session_id"], offline))
        return loader(entry, cache_path, offline)

    result = module().run_corpus(
        tmp_path, detailed_root=paths.root, session_ids={"2018-02-practice-1"}, load_session=only
    )
    assert result["selected"] == 1 and result["attempted"] == 1 and result["failed"] == 0
    assert result["scope_complete"] is True and result["complete"] is False
    assert calls == [("2018-02-practice-1", False), ("2018-02-practice-1", True)]


def ready(tmp_path: Path, *, additional: int = 0) -> DetailedPaths:
    paths, _ = study(tmp_path)
    assert isinstance(paths, DetailedPaths)
    root = paths.root
    if additional:
        discovery = json.loads((root / "inventory.json").read_text())
        catalog = json.loads((root / "reconciled-inventory.json").read_text())
        original, canonical = discovery["sessions"][0], catalog["sessions"][0]
        for number in range(2, additional + 2):
            identity = f"2018-{number:02d}-practice-1"
            row = {**original, "session_id": identity, "round_number": number}
            discovery["sessions"].append(row)
            catalog["sessions"].append({**canonical, **row})
            source = json.loads((root / "completion/2018-01-practice-1.json").read_text())
            save(root / f"completion/{identity}.json", {**source, "session_id": identity})
            catalog["completion_evidence_sha256"][identity] = file_sha256(
                root / f"completion/{identity}.json"
            )
        save(root / "inventory.json", discovery)
        catalog["discovery_sha256"] = file_sha256(root / "inventory.json")
        save(root / "reconciled-inventory.json", catalog)
        plan = json.loads((root / "study/sample-plan.json").read_text())
        plan["inventory_sha256"] = file_sha256(root / "reconciled-inventory.json")
        save(root / "study/sample-plan.json", plan)
    project_study(paths)
    measurement = json.loads(
        (paths.root / "preflight/2018-01-practice-1/measurement.json").read_text()
    )
    metrics = json.loads(
        (paths.root / "study/session-measurements/2018-01-practice-1.json").read_text()
    )
    receipt = paths.root / "study/provider-restore/2018-01-practice-1.json"
    receipt.parent.mkdir()
    receipt.write_text(
        json.dumps(
            {
                "session_id": "2018-01-practice-1",
                "offline": True,
                "native_tables_exact": True,
                "tables_checked": list(measurement["tables"]),
                "load_seconds": 0.01,
                "bundle_sha256": metrics["raw_bundle_sha256"],
            }
        )
    )
    return paths


def loader(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
    cache.mkdir(parents=True, exist_ok=True)
    if not offline:
        (cache / "captured-provider.bin").write_bytes(b"captured-native-provider-fixture")
    else:
        assert (cache / "captured-provider.bin").read_bytes() == b"captured-native-provider-fixture"
    session = native_session()
    session.api_path = "/static/" + entry["timing_path"]
    return session


def test_gate_requires_actual_current_proof_not_forged_safe_boolean(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, override=tmp_path / "detailed")
    paths.root.mkdir()
    (paths.root / "stratified-projection.json").write_text(json.dumps({"safe_to_sync": True}))
    with pytest.raises(ValueError, match="verified storage"):
        module().run_corpus(tmp_path, detailed_root=paths.root)
    assert not (paths.root / "corpus").exists()


def test_reuses_frozen_sample_without_mutating_it_and_is_idempotent(tmp_path: Path) -> None:
    paths = ready(tmp_path)
    before = source_manifest(paths.root / "preflight")
    frozen = {
        path: (file_sha256(path), path.stat().st_mtime_ns)
        for path in (paths.root / "study").rglob("*")
        if path.is_file()
    }
    result = module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        load_session=lambda *_: pytest.fail("captured native data must be reused"),
    )
    assert result["attempted"] == 0
    assert result["reused"] == 1
    assert result["failed"] == 0
    row = result["sessions"][0]
    assert row["state"] == "complete"
    assert len(row["raw_identity"]) == len(row["canonical_identity"]) == 64
    assert row["availability"]["pos_data"]["state"] == "unavailable"
    assert row["quality"] == "partial"
    assert source_manifest(paths.root / "preflight") == before
    assert frozen == {path: (file_sha256(path), path.stat().st_mtime_ns) for path in frozen}
    second = module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        load_session=lambda *_: pytest.fail("resume must not load"),
    )
    assert second["attempted"] == 0 and second["reused"] == 1
    assert second["sessions"][0]["canonical_identity"] == row["canonical_identity"]


def test_new_load_offline_exact_reparse_optional_channels_and_filter_counts(tmp_path: Path) -> None:
    paths = ready(tmp_path, additional=2)
    calls = []

    def load(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        calls.append((entry["session_id"], offline))
        return loader(entry, cache, offline)

    result = module().run_corpus(tmp_path, detailed_root=paths.root, limit=2, load_session=load)
    assert result["attempted"] == 1 and result["reused"] == 1 and result["failed"] == 0
    assert result["skipped"] == 1 and not result["complete"]
    assert calls == [("2018-02-practice-1", False), ("2018-02-practice-1", True)]
    row = result["sessions"][1]
    table = DetailedStore(tmp_path, detailed_root=paths.root).read_table(
        row["session_id"], "car-data-44"
    )
    assert table["speed"].to_pylist() == [123.125, 125.5]
    assert str(table["speed"].type) == "double"
    assert row["availability"]["corners"]["state"] == "unavailable"
    assert list((paths.root / "corpus/staging/2018-02-practice-1").glob("attempt-*/raw-cache"))


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("HTTP 403"),
        CapacityStop("full"),
        RateLimitExceededError(),
        KeyboardInterrupt(),
    ],
)
def test_fatal_load_failure_stops_operation_and_preserves_checkpoint(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    paths = ready(tmp_path, additional=2)
    calls = []

    def load(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        calls.append(entry["session_id"])
        raise failure

    with pytest.raises(type(failure)):
        module().run_corpus(tmp_path, detailed_root=paths.root, load_session=load)
    assert calls == ["2018-02-practice-1"]
    job = json.loads((paths.root / "corpus/jobs/2018-02-practice-1.json").read_text())
    assert job["state"] == "FAILED" and job["checkpoint"] == "DOWNLOADING"


def test_ordinary_failure_continues_and_next_run_resumes_without_revision_churn(
    tmp_path: Path,
) -> None:
    paths = ready(tmp_path, additional=2)

    def failing(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        if entry["round_number"] == 2:
            raise RuntimeError("ordinary provider failure")
        return loader(entry, cache, offline)

    first = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=failing)
    assert first["failed"] == 1 and first["attempted"] == 2
    assert first["sessions"][-1]["state"] == "complete"
    second = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=loader)
    assert second["attempted"] == 1 and second["reused"] == 2 and second["failed"] == 0
    assert (
        second["sessions"][-1]["canonical_identity"] == first["sessions"][-1]["canonical_identity"]
    )


def test_raw_checkpoint_resume_does_not_download_after_offline_mismatch(tmp_path: Path) -> None:
    paths = ready(tmp_path, additional=1)
    calls = []

    def bad_restore(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        calls.append(offline)
        session = loader(entry, cache, offline)
        if offline:
            session.car_data["44"].loc[0, "Speed"] = 0.0
        return session

    first = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=bad_restore)
    assert first["failed"] == 1 and calls == [False, True]
    job = json.loads((paths.root / "corpus/jobs/2018-02-practice-1.json").read_text())
    assert job["checkpoint"] == "RAW_EVIDENCE_PUBLISHED"
    calls.clear()

    def good_restore(entry: dict[str, Any], cache: Path, offline: bool) -> Any:
        calls.append(offline)
        assert offline
        return loader(entry, cache, offline)

    second = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=good_restore)
    assert second["failed"] == 0 and second["attempted"] == 0 and calls == [True]


def test_current_authority_change_during_load_stops_before_publication(tmp_path: Path) -> None:
    paths = ready(tmp_path, additional=2)
    calls = []

    def mutate(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        calls.append(entry["session_id"])
        session = loader(entry, cache_path, offline)
        if not offline:
            catalog = paths.root / "reconciled-inventory.json"
            catalog.write_bytes(catalog.read_bytes() + b" ")
        return session

    with pytest.raises(ValueError, match="changed during"):
        module().run_corpus(tmp_path, detailed_root=paths.root, load_session=mutate)
    assert calls == ["2018-02-practice-1"]
    assert not (paths.canonical / "2018-02-practice-1/active.json").exists()


def test_empty_car_is_honestly_unsupported_without_destroying_laps_results(tmp_path: Path) -> None:
    paths = ready(tmp_path, additional=1)

    def empty(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        session = loader(entry, cache_path, offline)
        session.car_data = {}
        return session

    result = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=empty)
    row = result["sessions"][1]
    assert row["state"] == "complete" and row["quality"] == "partial"
    assert row["availability"]["car_data"]["state"] == "telemetry-unsupported"
    manifest = DetailedStore(tmp_path, detailed_root=paths.root).validate(row["session_id"])
    assert "laps" in manifest["tables"] and "results" in manifest["tables"]
    assert not any(name.startswith("car-data-") for name in manifest["tables"])


def test_generated_native_event_uses_read_only_captured_schedule_not_index(tmp_path: Path) -> None:
    body = schedule_fixture()
    entry = schedule_sessions(body, 2022, now=datetime(2026, 1, 1, tzinfo=UTC))[0]
    entry["timing_path"] = schedule_timing_paths(body, 2022)[entry["session_id"]]
    db = tmp_path / "discovery-cache/fastf1_http_cache.sqlite"
    db.parent.mkdir()
    cache(db, [response(schedule_url(2022), body)])
    before = file_sha256(db), db.stat().st_mtime_ns
    native = importlib.import_module("downforce_core.detailed.corpus_native")
    with native.stop_latch(offline=True):
        session = native.construct_session(tmp_path, entry)
    assert session.api_path == "/static/" + entry["timing_path"]
    assert (file_sha256(db), db.stat().st_mtime_ns) == before
    with pytest.raises(ValueError, match="timing path"):
        native.construct_session(tmp_path, {**entry, "timing_path": "2022/denied/Index.json"})
    with pytest.raises(ValueError, match="schedule identity"):
        native.construct_session(tmp_path, {**entry, "country": "different provider identity"})


def test_offline_network_denial_is_not_swallowed_by_loader(tmp_path: Path) -> None:
    import requests

    paths = ready(tmp_path, additional=2)
    calls = []

    def catching(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        calls.append((entry["session_id"], offline))
        if offline:
            try:
                requests.Session().send(requests.Request("GET", "https://example.test").prepare())
            except PermissionError:
                pass
        return loader(entry, cache_path, offline)

    with pytest.raises(PermissionError, match="offline.*network"):
        module().run_corpus(tmp_path, detailed_root=paths.root, load_session=catching)
    assert calls == [("2018-02-practice-1", False), ("2018-02-practice-1", True)]


def test_lifecycle_validation_precedes_production_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downforce_core.detailed.lifecycle import Lifecycle

    paths = ready(tmp_path)
    original = Lifecycle.advance

    def inspect(self: Lifecycle, state: str, **kwargs: Any) -> None:
        if state == "VALIDATED":
            assert not (paths.canonical / self.session_id / "active.json").exists()
        return original(self, state, **kwargs)

    monkeypatch.setattr(Lifecycle, "advance", inspect)
    result = module().run_corpus(tmp_path, detailed_root=paths.root)
    assert result["failed"] == 0


def test_2026_drs_defaults_are_preserved_without_claiming_observed_off(tmp_path: Path) -> None:
    production = importlib.import_module("downforce_core.detailed.corpus_ingest")
    assert hasattr(production, "canonical_payload"), "per-field production provenance is missing"
    tables = {"car_data/44": pa.table({"DRS": pa.array([0, 0], type=pa.int64())})}
    normalized, metadata = production.canonical_payload(
        {"season": 2026}, tables, {"provider_version": "3.8.3", "availability": {}}, "a" * 64
    )
    assert normalized["car-data-44"]["drs"].to_pylist() == [0, 0]
    assert metadata["channels"]["car-data-44"]["field_availability"]["drs"] == {
        "state": "not-recorded",
        "source": "provider-parser-default",
        "provider_channel": "45",
    }


@pytest.mark.parametrize("state", ["RAW_EVIDENCE_PUBLISHED", "CANONICAL_PUBLISHED", "VALIDATED"])
def test_resume_after_durable_publication_fault_never_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from downforce_core.detailed.lifecycle import Lifecycle

    paths = ready(tmp_path, additional=1)
    original = Lifecycle.advance

    def crash(self: Lifecycle, checkpoint: str, **kwargs: Any) -> None:
        original(self, checkpoint, **kwargs)
        if self.session_id == "2018-02-practice-1" and checkpoint == state:
            raise RuntimeError("simulated process crash after durable checkpoint")

    with monkeypatch.context() as context:
        context.setattr(Lifecycle, "advance", crash)
        first = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=loader)
    assert first["failed"] == 1
    calls = []

    def offline_only(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        assert offline
        calls.append(offline)
        return loader(entry, cache_path, offline)

    second = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=offline_only)
    assert second["attempted"] == 0 and second["failed"] == 0 and second["reused"] == 2
    assert calls == ([True] if state == "RAW_EVIDENCE_PUBLISHED" else [])


@pytest.mark.parametrize("problem", ["bad-entry", "bad-native", "bad-receipt"])
def test_invalid_frozen_or_session_proof_is_never_reused(tmp_path: Path, problem: str) -> None:
    paths = ready(tmp_path)
    if problem == "bad-entry":
        source = paths.root / "completion/2018-01-practice-1.json"
        source.write_text("{}")
    elif problem == "bad-native":
        table = paths.root / "preflight/2018-01-practice-1/attempt/car_data/44.parquet"
        table.write_bytes(table.read_bytes() + b"corrupted")
    else:
        source = paths.root / "study/provider-restore/2018-01-practice-1.json"
        value = json.loads(source.read_text())
        value["bundle_sha256"] = "0" * 64
        save(source, value)
    if problem != "bad-receipt":
        with pytest.raises(ValueError, match="verified storage"):
            module().run_corpus(tmp_path, detailed_root=paths.root)
    else:
        result = module().run_corpus(tmp_path, detailed_root=paths.root)
        assert result["failed"] == 1 and result["attempted"] == 0
    assert not (paths.canonical / "2018-01-practice-1/active.json").exists()


def test_corpus_completion_does_not_mean_subset_or_empty_filter_completed_all(
    tmp_path: Path,
) -> None:
    paths = ready(tmp_path)
    result = module().run_corpus(tmp_path, detailed_root=paths.root, season=2024)
    assert result["selected"] == 0 and result["attempted"] == 0
    assert result["complete"] is False


def test_missing_projection_during_load_stops_whole_operation(tmp_path: Path) -> None:
    paths = ready(tmp_path, additional=2)
    calls: list[str] = []

    def remove(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        calls.append(entry["session_id"])
        session = loader(entry, cache_path, offline)
        if not offline:
            (paths.root / "stratified-projection.json").unlink()
        return session

    with pytest.raises(ValueError):
        module().run_corpus(tmp_path, detailed_root=paths.root, load_session=remove)
    assert calls == ["2018-02-practice-1"]


def test_native_nanoseconds_custom_fields_order_and_float64_are_preserved(tmp_path: Path) -> None:
    import pandas as pd  # type: ignore[import-untyped]

    paths = ready(tmp_path, additional=1)

    def precise(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        session = loader(entry, cache_path, offline)
        frame = session.car_data["44"]
        frame["SessionTime"] = pd.to_timedelta([1000001, 999999], unit="ns")
        frame["Date"] = pd.to_datetime([1000000000001, 1000000000002], unit="ns")
        frame["CustomProviderField"] = ["untouched", "second"]
        return session

    result = module().run_corpus(tmp_path, detailed_root=paths.root, load_session=precise)
    assert result["failed"] == 0
    table = DetailedStore(tmp_path, detailed_root=paths.root).read_table(
        "2018-02-practice-1", "car-data-44"
    )
    assert str(table["session_time"].type) == "duration[ns]"
    assert table["session_time"].cast("int64").to_pylist() == [1000001, 999999]
    assert table["timestamp"].cast("int64").to_pylist() == [1000000000001, 1000000000002]
    assert table["CustomProviderField"].to_pylist() == ["untouched", "second"]
    assert table["source_index"].to_pylist() == [0, 1]


def test_complete_frozen_resume_revalidates_restore_receipt(tmp_path: Path) -> None:
    paths = ready(tmp_path)
    first = module().run_corpus(tmp_path, detailed_root=paths.root)
    assert first["failed"] == 0
    receipt = paths.root / "study/provider-restore/2018-01-practice-1.json"
    value = json.loads(receipt.read_text())
    value["native_tables_exact"] = False
    save(receipt, value)
    second = module().run_corpus(
        tmp_path,
        detailed_root=paths.root,
        load_session=lambda *_: pytest.fail("invalid proof must not trigger download"),
    )
    assert second["failed"] == 1 and second["attempted"] == 0
    assert second["sessions"][0]["raw_identity"] == first["sessions"][0]["raw_identity"]


@pytest.mark.parametrize("kind", ["all-zero", "moving", "stationary-mix", "no-timed-laps"])
def test_all_zero_car_quality_requires_full_channel_and_clean_timed_lap_evidence(kind: str) -> None:
    moving = kind in {"moving", "stationary-mix"}
    values = [0, 100] if moving else [0, 0]
    car = pa.table(
        {
            "Speed": values,
            "RPM": values,
            "Throttle": values,
            "nGear": [0, 3] if moving else [0, 0],
            "DRS": [0, 0],
            "Brake": [False, False],
        }
    )
    laps = pa.table(
        {
            "DriverNumber": ["44"],
            "LapTime": pa.array([82_000_000_000], type=pa.duration("ns")),
            "IsAccurate": [kind != "no-timed-laps"],
        }
    )
    record = {
        "provider_version": "3.8.3",
        "availability": {
            "car_data": {"state": "available"},
            "laps": {"state": "available"},
            "results": {"state": "available"},
        },
    }
    tables, metadata = module().canonical_payload(
        {"season": 2019}, {"car_data/44": car, "laps": laps}, record, "a" * 64
    )
    assert tables["car-data-44"]["speed"].to_pylist() == values
    assert tables["car-data-44"].num_rows == 2
    assert str(tables["car-data-44"]["speed"].type) == "int64"
    if kind == "all-zero":
        assert metadata["quality"] == "partial"
        assert metadata["channel_availability"]["car_data"]["state"] == "telemetry-unsupported"
        availability = metadata["channels"]["car-data-44"]["availability"]
        assert availability["state"] == "degraded" and availability["usable"] is False
        assert availability["all_zero_channels"] == ["Speed", "RPM", "Throttle", "nGear"]
    else:
        assert metadata["channel_availability"]["car_data"]["state"] == "available"
        assert metadata["channels"]["car-data-44"]["availability"]["state"] == "available"


def test_progress_callback_follows_persisted_reuse_failure_and_new_completion(
    tmp_path: Path,
) -> None:
    paths = ready(tmp_path, additional=2)
    events: list[tuple[str, str]] = []

    def failing(entry: dict[str, Any], cache_path: Path, offline: bool) -> Any:
        if entry["round_number"] == 2:
            raise RuntimeError("ordinary provider failure")
        return loader(entry, cache_path, offline)

    def report(row: dict[str, Any]) -> None:
        job = json.loads((paths.root / "corpus/jobs" / f"{row['session_id']}.json").read_text())
        assert job["state"] == ("COMPLETE" if row["state"] == "complete" else "FAILED")
        events.append((row["session_id"], row["state"]))
        row["state"] = "callback mutation must not alter summary"

    result = module().run_corpus(
        tmp_path, detailed_root=paths.root, load_session=failing, report=report
    )
    assert events == [
        ("2018-01-practice-1", "complete"),
        ("2018-02-practice-1", "failed"),
        ("2018-03-practice-1", "complete"),
    ]
    assert [row["state"] for row in result["sessions"]] == ["complete", "failed", "complete"]
    assert result["reused"] == 1 and result["attempted"] == 2 and result["failed"] == 1
