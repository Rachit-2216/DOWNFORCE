"""A fixed cohort survives only replayed append-only temporal authority."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from downforce_core.detailed.openf1 import measure_enrichment
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.proofs import validated_study_measurements
from downforce_core.detailed.study_io import (
    project_measured_study,
    publish_sample_plan,
    validated_frozen_plan,
)
from downforce_core.storage.parquet import file_sha256
from test_detailed_proofs import OfflineClient, candidate_fixture
from test_detailed_study_runner import native_session, run, runner, save
from test_detailed_temporal_completion import captured as captured
from test_detailed_temporal_completion import finalizer, publish, read


@pytest.fixture
def grown(captured, monkeypatch, request):
    root, future = captured
    discovery = read(root, "inventory.json")
    past = []
    historical_sources = future[:1] if getattr(request, "param", None) == "new-stratum" else future
    for n, entry in enumerate(historical_sources, 1):
        historical = {
            **entry,
            "session_id": f"2026-02-practice-{n}",
            "event_id": "2026-02",
            "round_number": 2,
            "event_name": "Earlier Grand Prix",
            "scheduled_at": "2026-01-01T00:00:00+00:00",
            "completion": "past-unverified",
        }
        past.append(historical)
        save(
            root / f"completion/{historical['session_id']}.json",
            {
                "session_id": historical["session_id"],
                "state": "completed",
                "statuses": ["Started", "Finalised"],
                "times": ["0:00:01", "0:00:02"],
                "timing_path": f"2026/earlier/practice-{n}/",
                "error": None,
            },
        )
    discovery["sessions"] += past
    save(root / "inventory.json", discovery)
    provider_inventory = read(root, "openf1-inventory.json")
    for entry, provider in zip(past, provider_inventory["sessions"][: len(past)], strict=True):
        provider_inventory["sessions"].append(
            {
                **provider,
                "session_key": provider["session_key"] + 1000,
                "date_start": entry["scheduled_at"],
                "date_end": "2026-01-01T01:00:00+00:00",
            }
        )
    save(root / "openf1-inventory.json", provider_inventory)
    # Capture fixtures must bind the exact augmented discovery bytes.
    from test_detailed_temporal_completion import INDEX, RUN

    new_hash = file_sha256(root / "inventory.json")
    for path in (root / INDEX).glob("*.json"):
        value = json.loads(path.read_bytes())
        if "discovery_sha256" in value:
            value["discovery_sha256"] = new_hash
        if "source_discovery_hex" in value:
            value["source_discovery_hex"] = (root / "inventory.json").read_bytes().hex()
        save(path, value)
    index_hash = file_sha256(root / INDEX / "response.json")
    for path in (root / RUN).glob("*.json"):
        value = json.loads(path.read_bytes())
        if "discovery_sha256" in value:
            value["discovery_sha256"] = new_hash
        if "index_capture_sha256" in value:
            value["index_capture_sha256"] = index_hash
        if "openf1_inventory_sha256" in value:
            value["openf1_inventory_sha256"] = file_sha256(root / "openf1-inventory.json")
        save(path, value)
    outcome = read(root, RUN + "/outcome.json")
    for item in outcome["responses"]:
        item["sha256"] = file_sha256(root / item["path"])
    save(root / RUN / "outcome.json", outcome)

    class InitialClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 12, tzinfo=UTC)

    with monkeypatch.context() as clock:
        clock.setattr(finalizer(), "datetime", InitialClock)
        clock.setattr(runner(), "datetime", InitialClock)
        import downforce_core.detailed.study_io as study_io

        clock.setattr(study_io, "datetime", InitialClock)
        finalizer().finalize(root, offline=True)
        initial_bytes = (root / "reconciled-inventory.json").read_bytes()
        assert run(root, lambda *_: native_session())["failed"] == 0
    plan_bytes = (root / "study/sample-plan.json").read_bytes()
    journal_bytes = (root / "study/acquisition-journal.json").read_bytes()
    plan = read(root, "study/sample-plan.json")
    snapshot = root / f"study/inventory-snapshots/{plan['inventory_sha256']}.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(initial_bytes)
    publish(captured)
    finalizer().finalize(root, offline=True)
    runner().ready_inventory(root)
    for entry in past:
        identity = entry["session_id"]
        primary = read(root, f"preflight/{identity}/measurement.json")
        attempt = next(
            path.parent
            for path in (root / f"preflight/{identity}").glob("*/measurement.json")
            if json.loads(path.read_bytes()) == primary
        )
        candidate_fixture(root, primary, attempt)
    return root, plan_bytes, journal_bytes, snapshot


def test_replayed_growth_resumes_exact_frozen_native_cohort(grown):
    root, plan_bytes, _, _ = grown
    result = run(root, lambda *_: pytest.fail("frozen cohort must be reused"))
    assert result["reused"] == 2 and result["attempted"] == 0
    assert (root / "study/sample-plan.json").read_bytes() == plan_bytes


def test_replayed_growth_projects_current_population_with_unchanged_plan_and_journal(
    grown, monkeypatch
):
    root, plan_bytes, journal_bytes, _ = grown
    for selected in read(root, "study/sample-plan.json")["selected_sessions"]:
        measure_enrichment(root, selected, OfflineClient())
    samples = validated_study_measurements(DetailedPaths(root.parent, override=root))
    projection = project_measured_study(root, samples, free_bytes=10**12)
    assert projection["completed_sessions"] == 4
    assert projection["measurement_count"] == 2
    assert (root / "study/sample-plan.json").read_bytes() == plan_bytes
    assert (root / "study/acquisition-journal.json").read_bytes() == journal_bytes
    import downforce_core.detailed.proofs as proofs

    real = proofs.validated_study_measurements

    def raced(paths):
        measured = real(paths)
        catalog = root / "reconciled-inventory.json"
        catalog.write_bytes(catalog.read_bytes() + b" ")
        return measured

    monkeypatch.setattr(proofs, "validated_study_measurements", raced)
    with pytest.raises(ValueError, match="changed during"):
        project_measured_study(root, samples, free_bytes=10**12)


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "corrupt",
        "past-row",
        "past-alias",
        "seed",
        "summary-alias",
        "old-source",
        "proof-removal",
    ],
)
def test_growth_rejects_unbound_snapshot_or_changed_frozen_context(grown, problem):
    root, _, _, snapshot = grown
    if problem == "missing":
        snapshot.unlink()
    elif problem == "corrupt":
        snapshot.write_bytes(snapshot.read_bytes() + b" ")
    elif problem == "seed":
        plan = read(root, "study/sample-plan.json")
        plan["seed"] = 2216.0
        save(root / "study/sample-plan.json", plan)
    elif problem == "old-source":
        identity = read(root, "study/sample-plan.json")["selected_sessions"][0]["session_id"]
        path = root / f"completion/{identity}.json"
        path.write_bytes(path.read_bytes() + b" ")
        catalog = read(root, "reconciled-inventory.json")
        catalog["completion_evidence_sha256"][identity] = file_sha256(path)
        save(root / "reconciled-inventory.json", catalog)
    elif problem == "proof-removal":
        (root / "completion-temporal/2026-01-practice-1.json").unlink()
    else:
        catalog = read(root, "reconciled-inventory.json")
        past = next(row for row in catalog["sessions"] if row["round_number"] == 2)
        if problem == "past-row":
            past["source_annotation"] = "added"
        elif problem == "past-alias":
            past["fastf1_slot"] = float(past["fastf1_slot"])
        else:
            catalog["summary"]["completed"] = 4.0
        save(root / "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError):
        validated_study_measurements(DetailedPaths(root.parent, override=root))


def test_fresh_plan_publishes_exact_initial_snapshot_before_measurements(tmp_path):
    from test_detailed_study_runner import inventory

    inventory(tmp_path)
    initial_bytes = (tmp_path / "reconciled-inventory.json").read_bytes()
    plan = publish_sample_plan(tmp_path)
    snapshot = tmp_path / f"study/inventory-snapshots/{plan['inventory_sha256']}.json"
    assert snapshot.read_bytes() == initial_bytes
    plan_bytes = (tmp_path / "study/sample-plan.json").read_bytes()
    snapshot.write_bytes(initial_bytes + b" ")
    assert publish_sample_plan(tmp_path) == plan
    assert (tmp_path / "study/sample-plan.json").read_bytes() == plan_bytes
    assert snapshot.read_bytes() == initial_bytes + b" "


def test_fresh_plan_capacity_guard_stops_before_any_publication(tmp_path, monkeypatch):
    from downforce_core.detailed import status
    from test_detailed_study_runner import inventory

    inventory(tmp_path)
    initial_bytes = (tmp_path / "reconciled-inventory.json").read_bytes()
    monkeypatch.setattr(status.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(status.CapacityStop, match="headroom"):
        publish_sample_plan(tmp_path)
    assert not (tmp_path / "study").exists()
    assert (tmp_path / "reconciled-inventory.json").read_bytes() == initial_bytes


def test_lineage_rejects_catalog_mutation_during_real_readiness(grown, monkeypatch):
    root, _, _, _ = grown
    real_ready = runner().ready_inventory

    def changed(root):
        result = real_ready(root)
        path = root / "reconciled-inventory.json"
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(runner(), "ready_inventory", changed)
    with pytest.raises(ValueError, match="changed during"):
        validated_frozen_plan(root)


def test_native_boundary_rejects_changed_old_selected_row(grown):
    root, _, _, _ = grown
    _, originals = runner().ready_inventory(root)
    selected = read(root, "study/sample-plan.json")["selected_sessions"][0]
    catalog = read(root, "reconciled-inventory.json")
    row = next(row for row in catalog["sessions"] if row["session_id"] == selected["session_id"])
    row["unexpected_annotation"] = True
    save(root / "reconciled-inventory.json", catalog)
    with pytest.raises(ValueError, match="frozen selection"):
        runner()._finality_binding(root, originals[selected["session_id"]])


def test_lineage_rejects_plan_rewrite_against_original_journal_sha(grown):
    root, _, _, _ = grown
    plan = read(root, "study/sample-plan.json")
    plan["selected_at"] = "2026-01-01T12:00:01+00:00"
    save(root / "study/sample-plan.json", plan)
    with pytest.raises(ValueError, match="journal"):
        validated_frozen_plan(root)


@pytest.mark.parametrize("grown", ["new-stratum"], indirect=True)
def test_fully_replayed_new_stratum_is_rejected_without_reselecting(grown):
    root, plan_bytes, journal_bytes, _ = grown
    runner().ready_inventory(root)
    with pytest.raises(ValueError, match="stratum lacks frozen sample coverage"):
        validated_frozen_plan(root)
    assert (root / "study/sample-plan.json").read_bytes() == plan_bytes
    assert (root / "study/acquisition-journal.json").read_bytes() == journal_bytes
