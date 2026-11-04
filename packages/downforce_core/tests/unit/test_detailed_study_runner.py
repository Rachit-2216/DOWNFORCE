import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from downforce_core.detailed.sampling import capture_native_tables
from downforce_core.storage.parquet import file_sha256


def runner():
    return importlib.import_module("downforce_core.detailed.study_runner")


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def inventory(root: Path, count: int = 3) -> list[dict]:
    discovered = [
        {
            "session_id": f"2018-{number:02d}-race",
            "season": 2018,
            "round_number": number,
            "fastf1_slot": 5,
            "event_name": f"Event {number}",
            "session_type": "race",
            "scheduled_at": "2018-01-01T00:00:00+00:00",
            "completion": "past-unverified",
            "fastf1_supported": True,
        }
        for number in range(1, count + 1)
    ]
    save(root / "inventory.json", {"complete_inventory": True, "sessions": discovered})
    rows = [
        {
            **entry,
            "state": "completed",
            "statuses": ["Started", "Finalised"],
            "classification_version": "formal-completion-v2",
        }
        for entry in discovered
    ]
    for row in rows:
        save(
            root / "completion" / f"{row['session_id']}.json",
            {
                "session_id": row["session_id"],
                "state": "completed",
                "statuses": row["statuses"],
                "times": ["0:00:01", "0:00:02"],
                "timing_path": f"2018/event-{row['round_number']}/race/",
                "error": None,
            },
        )
    save(root / "openf1-inventory.json", {"sessions": [], "failures": []})
    save(
        root / "reconciled-inventory.json",
        {
            "complete": True,
            "classification_version": "formal-completion-v2",
            "sessions": rows,
            "provider_entries": [],
            "unmatched_unverified": [],
            "discovery_sha256": file_sha256(root / "inventory.json"),
            "completion_evidence_sha256": {
                row["session_id"]: file_sha256(root / "completion" / f"{row['session_id']}.json")
                for row in rows
            },
            "openf1_inventory_sha256": file_sha256(root / "openf1-inventory.json"),
        },
    )
    return discovered


def native_session(*, invalid: bool = False) -> SimpleNamespace:
    car = pd.DataFrame({"Speed": [123.125, 125.5], "nGear": [4, 5]})
    if invalid:
        car["bad"] = [object(), object()]
    return SimpleNamespace(
        results=pd.DataFrame({"DriverNumber": ["44"]}),
        laps=pd.DataFrame({"LapNumber": [1.0], "DriverNumber": ["44"]}),
        session_status=pd.DataFrame(
            {"Status": ["Started", "Finalised"], "Time": pd.to_timedelta([1, 2], unit="s")}
        ),
        track_status=pd.DataFrame({"Status": ["1"]}),
        weather_data=pd.DataFrame({"AirTemp": [23.125]}),
        race_control_messages=pd.DataFrame({"Message": ["TRACK CLEAR"]}),
        car_data={"44": car},
        pos_data={},
    )


def mutate_catalog(root: Path, change) -> None:
    path = root / "reconciled-inventory.json"
    value = json.loads(path.read_text())
    change(value)
    save(path, value)


def run(root: Path, load=None, **kwargs):
    return runner().run_study(
        root.parent,
        detailed_root=root,
        load_session=load,
        free_bytes=lambda _: 10**12,
        report=lambda _: None,
        **kwargs,
    )


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda c: c.update(complete=False), "complete"),
        (lambda c: c.pop("classification_version"), "formal"),
        (lambda c: c["sessions"][0].update(state="unverified"), "unverified"),
        (lambda c: c["sessions"].append(c["sessions"][0]), "duplicate"),
        (lambda c: c["sessions"][0].update(statuses=["Started", "Finalised", "Aborted"]), "formal"),
        (lambda c: c["sessions"][0].update(statuses=["Started", "Suspended", "Ends"]), "formal"),
        (lambda c: c["sessions"][0].update(scheduled_at="2099-01-01T00:00:00+00:00"), "identity"),
        (lambda c: c.update(unmatched_unverified=[99]), "provider"),
        (
            lambda c: c.update(
                provider_entries=[
                    {
                        "session_key": 99,
                        "reconciliation_state": "completed",
                        "canonical_session_id": None,
                    }
                ]
            ),
            "provider",
        ),
    ],
)
def test_fail_closed_before_plan_or_provider(tmp_path: Path, change, message: str) -> None:
    root = tmp_path / "detailed"
    inventory(root)
    mutate_catalog(root, change)
    with pytest.raises(ValueError, match=message):
        run(root, lambda *_: pytest.fail("provider must not run"), prepare_only=True)
    assert not (root / "study/sample-plan.json").exists()


def test_duplicate_canonical_rows_are_not_collapsed(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root)
    mutate_catalog(root, lambda c: c["sessions"].append(dict(c["sessions"][0])))
    with pytest.raises(ValueError, match="duplicate"):
        runner().ready_inventory(root)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-completion",
        "changed-completion",
        "missing-map",
        "missing-map-entry",
        "unknown-map-entry",
        "missing-openf1",
        "changed-openf1",
        "missing-openf1-sha",
        "openf1-failure-with-refreshed-sha",
        "wrong-source-id-with-refreshed-sha",
    ],
)
def test_ready_requires_all_original_completion_and_openf1_source_bindings(
    tmp_path: Path, problem: str
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root)
    identity = entries[0]["session_id"]
    completion = root / "completion" / (identity + ".json")
    provider = root / "openf1-inventory.json"
    if problem == "missing-completion":
        completion.unlink()
    elif problem == "changed-completion":
        value = json.loads(completion.read_text())
        value["statuses"] = ["Started", "Aborted"]
        save(completion, value)
    elif problem == "missing-map":
        mutate_catalog(root, lambda c: c.pop("completion_evidence_sha256"))
    elif problem == "missing-map-entry":
        mutate_catalog(root, lambda c: c["completion_evidence_sha256"].pop(identity))
    elif problem == "unknown-map-entry":
        mutate_catalog(
            root, lambda c: c["completion_evidence_sha256"].update({"2018-99-race": "unknown"})
        )
    elif problem == "missing-openf1":
        provider.unlink()
    elif problem in {"changed-openf1", "openf1-failure-with-refreshed-sha"}:
        save(provider, {"sessions": [], "failures": [{"error": "offline failure"}]})
        if problem == "openf1-failure-with-refreshed-sha":
            mutate_catalog(root, lambda c: c.update(openf1_inventory_sha256=file_sha256(provider)))
    elif problem == "missing-openf1-sha":
        mutate_catalog(root, lambda c: c.pop("openf1_inventory_sha256"))
    else:
        value = json.loads(completion.read_text())
        value["session_id"] = "2018-99-race"
        save(completion, value)
        mutate_catalog(
            root,
            lambda c: c["completion_evidence_sha256"].update({identity: file_sha256(completion)}),
        )
    with pytest.raises(ValueError, match="source|completion|OpenF1"):
        run(root, lambda *_: pytest.fail("unbound source must block provider"), prepare_only=True)
    assert not (root / "study/sample-plan.json").exists()


def native_measurement(directory: Path, entry: dict) -> dict:
    measured = capture_native_tables(native_session(), directory)
    return {
        "session": entry,
        "completion_confirmed": True,
        "session_statuses": ["Started", "Finalised"],
        **measured,
    }


def test_existing_optional_corners_unavailability_is_not_native_conversion_failure(
    tmp_path: Path,
) -> None:
    entry = inventory(tmp_path, count=1)[0]
    directory = tmp_path / "sample"
    value = native_measurement(directory, entry)
    value["failures"] = [{"channel": "corners", "error_type": "KeyError"}]
    runner()._validate_measurement(directory, value, entry)
    value["failures"] = [{"channel": "corners", "error_type": "ValueError"}]
    with pytest.raises(ValueError, match="native measurement"):
        runner()._validate_measurement(directory, value, entry)


@pytest.mark.parametrize("problem", ["aborted", "metadata-mismatch", "missing-laps", "empty-car"])
def test_native_reuse_checks_actual_completion_and_required_channels(
    tmp_path: Path, problem: str
) -> None:
    entry = inventory(tmp_path, count=1)[0]
    directory = tmp_path / "sample"
    value = native_measurement(directory, entry)
    if problem == "missing-laps":
        value["tables"].pop("laps")
    elif problem == "empty-car":
        for name, table in value["tables"].items():
            if name.startswith("car_data"):
                table["rows"] = 0
    elif problem == "metadata-mismatch":
        value["session_statuses"] = ["Started", "Aborted"]
    else:
        path = directory / "session_status.parquet"
        pd.DataFrame({"Status": ["Started", "Aborted"]}).to_parquet(path, index=False)
        value["tables"]["session_status"].update(
            sha256=file_sha256(path), bytes=path.stat().st_size
        )
        value["session_statuses"] = ["Started", "Aborted"]
    with pytest.raises(ValueError, match="native|completion|required"):
        runner()._validate_measurement(directory, value, entry)


def test_before_load_headroom_uses_known_measurement_peak(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, count=2)
    directory = root / "preflight" / entries[0]["session_id"] / "original"
    value = native_measurement(directory, entries[0])
    value["raw_bytes"] = 4 * runner().GIB
    (directory / "raw-cache").mkdir()
    save(directory / "measurement.json", value)
    save(directory.parent / "measurement.json", value)
    with pytest.raises(ValueError, match="headroom"):
        runner().run_study(
            root.parent,
            detailed_root=root,
            free_bytes=lambda _: runner().HEADROOM_BYTES + 3 * runner().GIB,
            load_session=lambda *_: pytest.fail("known peak must block before provider load"),
            report=lambda _: None,
        )


@pytest.mark.parametrize("times", [[2, 1], [1, 1], [-1, 2]])
def test_native_reuse_rejects_ambiguous_or_invalid_elapsed_times(tmp_path: Path, times) -> None:
    entry = inventory(tmp_path, count=1)[0]
    directory = tmp_path / "sample"
    value = native_measurement(directory, entry)
    path = directory / "session_status.parquet"
    pd.DataFrame(
        {
            "source_index": [0, 1],
            "Status": ["Started", "Finalised"],
            "Time": pd.to_timedelta(times, unit="s"),
        }
    ).to_parquet(path, index=False)
    value["tables"]["session_status"].update(sha256=file_sha256(path), bytes=path.stat().st_size)
    with pytest.raises(ValueError, match="native.*timestamp|native.*ambiguous"):
        runner()._validate_measurement(directory, value, entry)


def test_native_reuse_requires_time_and_published_row_counts(tmp_path: Path) -> None:
    entry = inventory(tmp_path, count=1)[0]
    directory = tmp_path / "sample"
    value = native_measurement(directory, entry)
    value["tables"]["car_data/44"]["rows"] = 999
    with pytest.raises(ValueError, match="native.*row"):
        runner()._validate_measurement(directory, value, entry)


def test_loaded_native_status_ambiguity_fails_before_measurement(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, count=1)

    def load(*_):
        session = native_session()
        session.session_status["Time"] = pd.to_timedelta([1, 1], unit="s")
        return session

    assert run(root, load)["failed"] == 1
    assert not list((root / "preflight").glob("*/measurement.json"))


def test_captured_journal_reserve_is_carried_to_next_provider_load(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "detailed"
    inventory(root, count=2)
    module = runner()
    publish = module._publish_once
    monkeypatch.setattr(
        module, "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda *_: native_session())
    monkeypatch.setattr(module, "_publish_once", publish)
    monkeypatch.setattr(module, "NEXT_PEAK_BYTES", 1)
    with pytest.raises(ValueError, match="headroom"):
        module.run_study(
            root.parent,
            detailed_root=root,
            free_bytes=lambda _: module.HEADROOM_BYTES + 3,
            load_session=lambda *_: pytest.fail(
                "captured native size must reserve before next load"
            ),
            report=lambda _: None,
        )


def test_later_captured_journal_reserve_blocks_an_earlier_retry_before_load(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, count=2)
    module = runner()
    publish = module._publish_once
    monkeypatch.setattr(
        module, "_publish_once", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    def initial(entry, _cache):
        if entry == entries[0]:
            raise RuntimeError("persisted ordinary failure")
        return native_session()

    with pytest.raises(KeyboardInterrupt):
        run(root, initial)
    monkeypatch.setattr(module, "_publish_once", publish)
    monkeypatch.setattr(module, "NEXT_PEAK_BYTES", 1)
    with pytest.raises(ValueError, match="headroom"):
        module.run_study(
            root.parent,
            detailed_root=root,
            free_bytes=lambda _: module.HEADROOM_BYTES + 3,
            load_session=lambda *_: pytest.fail(
                "later captured sample must reserve before earlier retry"
            ),
            report=lambda _: None,
        )


def test_prepare_only_publishes_reports_plan_without_network(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root)
    reports = []
    result = runner().run_study(
        tmp_path,
        detailed_root=root,
        prepare_only=True,
        load_session=lambda *_: pytest.fail("prepare must not load"),
        report=reports.append,
    )
    assert result["status"] == "prepared"
    assert result["attempted"] == result["new_writes"] == 0
    assert (root / "study/sample-plan.json").exists()
    assert all(
        any(
            entry["session_id"] in text and entry["event_name"] in text and "race" in text
            for text in reports
        )
        for entry in entries
    )
    assert not (root / "runtime/fastf1-worker.lock").exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["selected_sessions"].append(p["selected_sessions"][0]),
        lambda p: p["selected_sessions"][0].update(session_id="2018-99-race"),
        lambda p: p.update(inventory_sha256="changed"),
        lambda p: p["selected_sessions"][0].update(state="future"),
    ],
)
def test_invalid_frozen_plan_cannot_trigger_requests(tmp_path: Path, change) -> None:
    root = tmp_path / "detailed"
    inventory(root)
    run(root, prepare_only=True)
    plan_path = root / "study/sample-plan.json"
    plan = json.loads(plan_path.read_text())
    change(plan)
    save(plan_path, plan)
    with pytest.raises(ValueError):
        run(root, lambda *_: pytest.fail("invalid plan cannot load"))


def test_capture_real_tables_and_reuse_existing_without_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root)
    existing = root / "preflight" / entries[0]["session_id"]
    directory = existing / "original-attempt"
    capture = capture_native_tables(native_session(), directory)
    (directory / "raw-cache").mkdir()
    (directory / "raw-cache/source.bin").write_bytes(b"original")
    measurement = {"session": entries[0], "completion_confirmed": True, **capture}
    save(directory / "measurement.json", measurement)
    save(existing / "measurement.json", measurement)
    before = {path: path.read_bytes() for path in existing.rglob("*") if path.is_file()}
    loaded = []

    def load(entry, cache):
        assert (root / "study/sample-plan.json").exists()
        journal = json.loads((root / "study/acquisition-journal.json").read_text())
        assert journal["sessions"][entry["session_id"]]["state"] == "loading"
        assert cache.name == "raw-cache"
        loaded.append(entry["session_id"])
        (cache / "source.bin").write_bytes(b"new provider evidence")
        return native_session()

    result = run(root, load)
    assert result["status"] == "complete"
    assert result["attempted"] == result["new_writes"] == 2
    assert result["reused"] == 1
    assert entries[0]["session_id"] not in loaded
    assert before == {path: path.read_bytes() for path in existing.rglob("*") if path.is_file()}
    for entry in entries[1:]:
        saved = json.loads(
            (root / "preflight" / entry["session_id"] / "measurement.json").read_text()
        )
        assert saved["session"] == entry
        assert saved["completion_confirmed"] is True
        assert saved["tables"]["car_data/44"]["rows"] == 2
        assert saved["session_statuses"] == ["Started", "Finalised"]


def test_capture_failure_continues_selected_sessions_and_resumes_owned_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root)
    attempts = []

    def load(entry, cache):
        attempts.append(cache)
        (cache / "source.bin").write_bytes(b"provider evidence")
        return native_session(invalid=entry == entries[0])

    first = run(root, load)
    assert first["failed"] == 1
    assert first["new_writes"] == 2
    assert first["status"] == "failed"
    assert not (root / "preflight" / entries[0]["session_id"] / "measurement.json").exists()
    second_paths = []

    def resume(entry, cache):
        assert entry == entries[0]
        assert (cache / "source.bin").read_bytes() == b"provider evidence"
        second_paths.append(cache)
        return native_session()

    second = run(root, resume)
    assert second["status"] == "complete"
    assert second["reused"] == 2
    assert second_paths == attempts[:1]


def test_interrupt_persists_owned_attempt_before_load(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, 1)
    paths = []

    def interrupt(entry, cache):
        paths.append(cache)
        (cache / "source.bin").write_bytes(b"saved partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(root, interrupt)

    def resume(entry, cache):
        assert cache == paths[0]
        assert (cache / "source.bin").exists()
        return native_session()

    assert run(root, resume)["status"] == "complete"


def test_permission_denial_stops_whole_worker(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root)
    calls = []

    def denied(entry, cache):
        calls.append(entry)
        raise PermissionError("HTTP 403")

    with pytest.raises(PermissionError, match="403"):
        run(root, denied)
    assert len(calls) == 1
    journal = json.loads((root / "study/acquisition-journal.json").read_text())
    assert next(iter(journal["sessions"].values()))["state"] == "permission-denied"


def test_native_permission_denial_survives_secondary_journal_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runner()
    root = tmp_path / "detailed"
    inventory(root)
    denial = PermissionError(13, "native sharing violation", "native-original.tmp")
    denial.winerror = 32
    journal_denial = PermissionError(13, "journal write denied", "acquisition-journal.json")
    journal_denial.winerror = 5
    original_save = module._save
    calls = []
    denied_saves = []

    def load(entry, cache):
        calls.append(entry)
        return native_session()

    def denied_write(*args, **kwargs):
        raise denial

    def save(path, value):
        if any(record["state"] == "permission-denied" for record in value["sessions"].values()):
            denied_saves.append(path)
            raise journal_denial
        original_save(path, value)

    monkeypatch.setattr("downforce_core.detailed.sampling.write_sample_table", denied_write)
    monkeypatch.setattr(module, "_save", save)
    with pytest.raises(PermissionError) as caught:
        run(root, load)
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert caught.value.filename == "native-original.tmp"
    assert len(calls) == 1
    assert denied_saves == [root / "study/acquisition-journal.json"]
    assert any(
        "journal" in note and "winerror=5" in note and "acquisition-journal.json" in note
        for note in getattr(caught.value, "__notes__", [])
    )
    assert not (root / "study/acquisition-summary.json").exists()


def test_sticky_permission_denial_survives_secondary_journal_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    module = runner()
    root = tmp_path / "detailed"
    inventory(root)
    denial = PermissionError(13, "sticky provider denial", "native-original.tmp")
    denial.winerror = 32
    journal_denial = PermissionError(13, "journal write denied", "acquisition-journal.json")
    journal_denial.winerror = 5
    original_save = module._save
    calls = []
    denied_saves = []
    sticky = False

    @contextmanager
    def guard(_):
        def permission_check():
            if sticky:
                raise denial

        yield permission_check

    def load(entry, cache):
        calls.append(entry)
        return native_session()

    def failed_capture(*args):
        nonlocal sticky
        sticky = True
        raise ValueError("native serialization failed after swallowed transport denial")

    def save(path, value):
        if any(record["state"] == "permission-denied" for record in value["sessions"].values()):
            denied_saves.append(path)
            raise journal_denial
        original_save(path, value)

    monkeypatch.setattr(module, "fastf1_guard", guard)
    monkeypatch.setattr(module, "capture_native_tables", failed_capture)
    monkeypatch.setattr(module, "_save", save)
    with pytest.raises(PermissionError) as caught:
        run(root, load)
    assert caught.value is denial
    assert caught.value.winerror == 32
    assert caught.value.filename == "native-original.tmp"
    assert len(calls) == 1
    assert denied_saves == [root / "study/acquisition-journal.json"]
    assert any(
        "journal" in note and "winerror=5" in note and "acquisition-journal.json" in note
        for note in getattr(caught.value, "__notes__", [])
    )
    assert not (root / "study/acquisition-summary.json").exists()


def test_space_checked_before_load_and_before_capture(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, 1)
    calls = []
    # Initial plan guard, pre-load guard, then post-load/pre-capture guard.
    free = iter([10**12, 10**12, 50 * 1024**3])

    def load(entry, cache):
        calls.append(entry)
        (cache / "source.bin").write_bytes(b"evidence")
        return native_session()

    with pytest.raises(ValueError, match="headroom"):
        runner().run_study(
            tmp_path,
            detailed_root=root,
            load_session=load,
            free_bytes=lambda _: next(free),
            report=lambda _: None,
        )
    assert len(calls) == 1
    assert not list((root / "preflight").rglob("*.parquet"))


def test_resume_rejects_attempt_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, 1)
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    path = root / "study/acquisition-journal.json"
    journal = json.loads(path.read_text())
    next(iter(journal["sessions"].values()))["attempt"] = "../source"
    save(path, journal)
    with pytest.raises(ValueError, match="attempt"):
        run(root, lambda *_: pytest.fail("unsafe attempt must not load"))


def test_native_rate_limit_stops_without_loading_other_sessions(tmp_path: Path) -> None:
    from fastf1.exceptions import RateLimitExceededError

    root = tmp_path / "detailed"
    inventory(root)
    calls = []

    def limited(entry, cache):
        calls.append(entry)
        raise RateLimitExceededError("native rate limit")

    with pytest.raises(RateLimitExceededError):
        run(root, limited)
    assert len(calls) == 1


def test_captured_journal_resumes_publication_without_provider_load(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "detailed"
    inventory(root, 1)
    module = runner()
    publish = module._publish_once

    def interrupt_publish(path, value):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_publish_once", interrupt_publish)
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda *_: native_session())
    record = next(
        iter(json.loads((root / "study/acquisition-journal.json").read_text())["sessions"].values())
    )
    assert record["state"] == "captured"
    monkeypatch.setattr(module, "_publish_once", publish)
    result = run(root, lambda *_: pytest.fail("captured native tables must not redownload"))
    assert result["new_writes"] == 1
    assert result["attempted"] == 0


@pytest.mark.parametrize("stage", ["load", "capture"])
def test_swallowed_transport_denial_prevents_any_measurement(
    tmp_path: Path,
    monkeypatch,
    stage: str,
) -> None:
    import requests
    from fastf1.req import _SessionWithRateLimiting

    root = tmp_path / "detailed"
    inventory(root)
    monkeypatch.setattr(
        _SessionWithRateLimiting, "send", lambda *_args, **_kwargs: SimpleNamespace(status_code=403)
    )

    def swallowed():
        transport = _SessionWithRateLimiting()
        request = requests.Request("GET", "https://provider.invalid/offline-fixture").prepare()
        try:
            transport.send(request)
        except PermissionError:
            pass

    def load(entry, cache):
        session = native_session()
        if stage == "load":
            swallowed()
        else:
            session.get_circuit_info = lambda: (
                swallowed(),
                SimpleNamespace(corners=pd.DataFrame({"Number": [1]})),
            )[1]
        return session

    with pytest.raises(PermissionError, match="403"):
        run(root, load)
    assert not list((root / "preflight").rglob("measurement.json"))
    assert not list((root / "preflight").rglob("capture-failures.json"))


def test_denial_during_failed_serialization_stops_before_next_provider_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import requests
    from fastf1.req import _SessionWithRateLimiting

    root = tmp_path / "detailed"
    inventory(root)
    calls = []
    monkeypatch.setattr(
        _SessionWithRateLimiting, "send", lambda *_args, **_kwargs: SimpleNamespace(status_code=401)
    )

    def load(entry, cache):
        calls.append(entry)
        session = native_session(invalid=True)

        def circuit():
            transport = _SessionWithRateLimiting()
            request = requests.Request("GET", "https://provider.invalid/fixture").prepare()
            try:
                transport.send(request)
            except PermissionError:
                pass
            return SimpleNamespace(corners=pd.DataFrame({"Number": [1]}))

        session.get_circuit_info = circuit
        return session

    with pytest.raises(PermissionError, match="401"):
        run(root, load)
    assert len(calls) == 1
    journal = json.loads((root / "study/acquisition-journal.json").read_text())
    assert next(iter(journal["sessions"].values()))["state"] == "permission-denied"


def test_another_network_worker_blocks_all_acquisition(tmp_path: Path) -> None:
    from downforce_core.detailed.provider_guard import network_worker

    root = tmp_path / "detailed"
    inventory(root)
    with network_worker(root / "runtime/fastf1-worker.lock"):
        with pytest.raises(ValueError, match="another"):
            run(root, lambda *_: pytest.fail("another worker owns the network"))
    assert not (root / "study/acquisition-journal.json").exists()


def test_only_selected_plan_sessions_are_loaded(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, 8)
    calls = []

    def load(entry, cache):
        calls.append(entry["session_id"])
        return native_session()

    result = run(root, load)
    plan = json.loads((root / "study/sample-plan.json").read_text())
    assert calls == [entry["session_id"] for entry in plan["selected_sessions"]]
    assert result["attempted"] == 3


def test_script_prepare_only_is_provider_free(tmp_path: Path, monkeypatch) -> None:
    import importlib.util

    root = tmp_path / "detailed"
    inventory(root, 1)
    script = Path(__file__).resolve().parents[4] / "scripts/run_detailed_study.py"
    spec = importlib.util.spec_from_file_location("run_detailed_study", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(runner(), "_load_fastf1", lambda *_: pytest.fail("no provider requests"))
    assert module.main(["--root", str(root), "--prepare-only"]) == 0
    assert (root / "study/sample-plan.json").exists()


def test_script_waits_in_bounded_chunks_without_provider_requests(
    tmp_path: Path, monkeypatch
) -> None:
    import importlib.util

    root = tmp_path / "detailed"
    inventory(root, 1)
    mutate_catalog(root, lambda c: c.update(complete=False))
    script = Path(__file__).resolve().parents[4] / "scripts/run_detailed_study.py"
    spec = importlib.util.spec_from_file_location("run_detailed_study_wait", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sleeps = []

    def sleep(seconds):
        assert 0 < seconds <= 60
        sleeps.append(seconds)
        mutate_catalog(root, lambda c: c.update(complete=True))

    monkeypatch.setattr(module, "sleep", sleep, raising=False)
    monkeypatch.setattr(runner(), "_load_fastf1", lambda *_: pytest.fail("wait must not request"))
    assert module.main(["--root", str(root), "--prepare-only", "--wait"]) == 0
    assert sleeps == [60]


def test_measurement_counts_raw_evidence_fetched_during_optional_capture(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, 1)

    def load(entry, cache):
        (cache / "primary.bin").write_bytes(b"123")
        session = native_session()

        def circuit():
            (cache / "corners.bin").write_bytes(b"4567")
            return SimpleNamespace(corners=pd.DataFrame({"Number": [1]}))

        session.get_circuit_info = circuit
        return session

    run(root, load)
    measured = json.loads(
        (root / "preflight" / entries[0]["session_id"] / "measurement.json").read_text()
    )
    assert measured["raw_bytes"] == 7


def test_existing_measurement_is_used_for_largest_known_capture_space(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, 2)
    directory = root / "preflight" / entries[0]["session_id"] / "original-attempt"
    capture = capture_native_tables(native_session(), directory)
    measurement = {"session": entries[0], "completion_confirmed": True, **capture}
    save(directory / "measurement.json", measurement)
    save(directory.parent / "measurement.json", measurement)
    monkeypatch.setattr(runner(), "NEXT_PEAK_BYTES", 1)
    # Initial plan guard, pre-load guard, then post-load measured peak guard.
    free = iter([10**12, 10**12, 50 * 1024**3 + 3])
    with pytest.raises(ValueError, match="headroom"):
        runner().run_study(
            tmp_path,
            detailed_root=root,
            load_session=lambda *_: native_session(),
            free_bytes=lambda _: next(free),
            report=lambda _: None,
        )
    assert not (root / "preflight" / entries[1]["session_id"] / "measurement.json").exists()


@pytest.mark.parametrize(
    "provider",
    [
        {
            "session_key": 1,
            "canonical_session_id": None,
            "reconciliation_state": "future",
            "date_start": "2018-01-01T00:00:00Z",
        },
        {
            "session_key": 1,
            "canonical_session_id": None,
            "reconciliation_state": "cancelled",
            "is_cancelled": False,
        },
        {
            "session_key": 1,
            "canonical_session_id": None,
            "reconciliation_state": "testing",
            "session_name": "Race",
        },
    ],
)
def test_provider_exclusions_need_actual_source_evidence(tmp_path: Path, provider) -> None:
    root = tmp_path / "detailed"
    inventory(root, 1)
    mutate_catalog(root, lambda c: c.update(provider_entries=[provider]))
    with pytest.raises(ValueError, match="provider"):
        run(root, lambda *_: pytest.fail("provider exclusions are unverified"))


@pytest.mark.parametrize("unknown", [False, True])
def test_valid_provider_only_exclusions_and_formal_archive_completion(
    tmp_path: Path, unknown: bool
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, 1)
    identity = entries[0]["session_id"]
    source_path = root / "completion" / (identity + ".json")
    source = {
        "session_id": identity,
        "timing_path": "2018/event/session/",
        "state": "unverified",
        "statuses": ["Started", "Finished"],
        "times": [],
    }
    save(source_path, source)
    from downforce_core.detailed.evidence import sha256, status_url

    body = b'{"Status":"Complete"}'
    archive_path = root / "archive-finalization" / (identity + ".json")
    save(
        archive_path,
        {
            "session_id": identity,
            "url": status_url(entries[0], source).replace(
                "SessionStatus.jsonStream", "ArchiveStatus.json"
            ),
            "body_hex": body.hex(),
            "body_sha256": sha256(body),
            "http_status": 200,
            "observed_at": "2026-01-01T00:00:00+00:00",
            "source_completion_sha256": file_sha256(source_path),
        },
    )

    def formal(c):
        c["completion_evidence_sha256"][identity] = file_sha256(source_path)
        c["archive_evidence_sha256"] = {identity: file_sha256(archive_path)}
        c["sessions"][0].update(
            statuses=["Started", "Finished"], archive_status={"Status": "Complete"}
        )
        c["provider_entries"] = [
            {
                "session_key": 1,
                "canonical_session_id": None,
                "reconciliation_state": "future",
                "date_start": "2099-01-01T00:00:00Z",
            },
            {
                "session_key": 2,
                "canonical_session_id": None,
                "reconciliation_state": "cancelled",
                "is_cancelled": True,
            },
            {
                "session_key": 3,
                "canonical_session_id": None,
                "reconciliation_state": "testing",
                "session_name": "Day 1",
            },
        ]

    mutate_catalog(root, formal)

    def truncated_load(entry, cache):
        session = native_session()
        session.session_status = pd.DataFrame(
            {
                "Status": ["Started", "Suspended" if unknown else "Finished"],
                "Time": pd.to_timedelta([1, 2], unit="s"),
            }
        )
        return session

    result = run(root, truncated_load)
    if unknown:
        assert result["failed"] == 1
        assert not list((root / "preflight").glob("*/measurement.json"))
    else:
        assert result["status"] == "complete"
        assert (
            run(root, lambda *_: pytest.fail("archive-proven native sample must reuse"))["reused"]
            == 1
        )


def test_existing_authoritative_attempt_rejects_linked_directories(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, 1)
    directory = root / "preflight" / entries[0]["session_id"] / "original-attempt"
    capture = capture_native_tables(native_session(), directory)
    measurement = {"session": entries[0], "completion_confirmed": True, **capture}
    save(directory / "measurement.json", measurement)
    save(directory.parent / "measurement.json", measurement)
    junction = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == directory or junction(path))
    with pytest.raises(ValueError, match="linked"):
        run(root, lambda *_: pytest.fail("linked native sources cannot be used"))


def test_past_canonical_session_cannot_hide_as_future(tmp_path: Path) -> None:
    root = tmp_path / "detailed"
    inventory(root, 2)
    mutate_catalog(root, lambda c: c["sessions"][0].update(state="future"))
    with pytest.raises(ValueError, match="future"):
        run(root, lambda *_: pytest.fail("stale future inventory is not ready"))


@pytest.mark.parametrize(
    "statuses", [["Inactive"], ["Started", "Aborted"], ["Started", "Suspended"]]
)
def test_loaded_noncompleted_session_cannot_publish_measurement(tmp_path: Path, statuses) -> None:
    root = tmp_path / "detailed"
    entries = inventory(root, 1)

    def load(entry, cache):
        session = native_session()
        session.session_status = pd.DataFrame(
            {"Status": statuses, "Time": pd.to_timedelta(range(len(statuses)), unit="s")}
        )
        return session

    result = run(root, load)
    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert not (root / "preflight" / entries[0]["session_id"] / "measurement.json").exists()
