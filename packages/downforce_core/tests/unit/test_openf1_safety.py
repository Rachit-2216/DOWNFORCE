"""Synthetic-only regression tests for owned public historical enrichment."""

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from downforce_core.detailed import openf1, openf1_transport
from downforce_core.detailed.provider_guard import network_worker
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.parquet import file_sha256
from test_detailed_proofs import save, study


def ready(tmp_path: Path):
    paths, _ = study(tmp_path, modern=True)
    return paths.root, json.loads((paths.root / "reconciled-inventory.json").read_text())[
        "sessions"
    ][0]


def transport(monkeypatch, responses):
    clock = [datetime(2026, 10, 9, tzinfo=UTC).timestamp()]
    calls = []
    waits = []

    def pause(seconds):
        assert 0 < seconds <= 60
        waits.append(seconds)
        clock[0] += seconds

    class Response(BytesIO):
        def __init__(self, body, url, status=200, headers=None):
            super().__init__(body)
            self.url, self.status, self.headers = url, status, headers or {}

        def geturl(self):
            return self.url

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, clock[0]))
            status, body, headers, actual = responses.pop(0)
            url = actual or request.full_url
            if status != 200:
                stream = body if isinstance(body, BytesIO) else BytesIO(body)
                raise HTTPError(url, status, "synthetic", headers, stream)
            return Response(body, url, headers=headers)

    # This is the external transport seam, never a readiness bypass.
    monkeypatch.setattr(openf1_transport, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(openf1_transport, "time", lambda: clock[0])
    monkeypatch.setattr(openf1_transport, "monotonic", lambda: clock[0])
    monkeypatch.setattr(openf1_transport, "sleep", pause)
    return clock, calls, waits


def response(status=200, body=b'[{"session_key":123,"driver_number":44}]', headers=None, url=None):
    return status, body, headers or {}, url


def test_client_requires_owned_historical_scope_before_transport(tmp_path, monkeypatch):
    _, calls, _ = transport(monkeypatch, [response()])
    client = openf1.OpenF1Client(tmp_path)
    with pytest.raises(ValueError, match="context|worker"):
        client.fetch("drivers", 123)
    with client:
        with pytest.raises(ValueError, match="historical|scope"):
            client.fetch("drivers", 123)
    assert not calls


def test_openf1_context_excludes_fastf1_worker_and_releases_on_exit(tmp_path):
    lock = tmp_path / "runtime" / "fastf1-worker.lock"
    with openf1.OpenF1Client(tmp_path):
        with pytest.raises(ValueError, match="another detailed network worker"):
            with network_worker(lock):
                pytest.fail("shared worker ownership was not enforced")
    with network_worker(lock):
        pass


@pytest.mark.parametrize("code", [401, 403])
def test_denial_retains_original_provenance_and_stops_whole_context(tmp_path, monkeypatch, code):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    original = b' {"detail":"denied", "exact": 1.00000000001}\n'
    _, calls, _ = transport(monkeypatch, [response(code, original, {"X-Reason": "denied"})])
    with pytest.raises(PermissionError), openf1.OpenF1Client(root) as client:
        with pytest.raises(PermissionError):
            openf1.measure_enrichment(root, session, client)
        with pytest.raises(PermissionError):
            openf1.measure_enrichment(root, session, client)
    assert len(calls) == 1
    directory = root / "study" / "openf1" / session["session_id"]
    attempts = list(directory.glob("*-attempt-*.json"))
    assert len(attempts) == 1
    record = json.loads(attempts[0].read_text())
    assert record["http_status"] == code
    assert record["headers"] == {"X-Reason": "denied"}
    assert datetime.fromisoformat(record["observed_at"]).tzinfo is not None
    assert record["url"] == calls[0][0]
    import gzip
    import hashlib

    assert gzip.decompress((directory / record["raw_file"]).read_bytes()) == original
    assert record["response_sha256"] == hashlib.sha256(original).hexdigest()
    assert record["source_body_bytes"] == len(original)
    assert record["raw_file_sha256"] == file_sha256(directory / record["raw_file"])


def test_long_retry_after_is_persisted_without_early_retry(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, waits = transport(
        monkeypatch, [response(429, b'{"detail":"slow"}', {"Retry-After": "601"})]
    )
    with pytest.raises(ValueError, match="cooldown|wait"):
        with openf1.OpenF1Client(root) as client:
            openf1.measure_enrichment(root, session, client)
    assert len(calls) == 1 and waits == []
    minute = json.loads((root / "runtime" / "openf1-minute-budget.json").read_text())
    spacing = json.loads((root / "runtime" / "openf1-spacing-budget.json").read_text())
    assert minute["not_before"] == spacing["not_before"] == clock[0] + 601
    with pytest.raises(ValueError, match="cooldown|wait"):
        with openf1.OpenF1Client(root) as client:
            openf1.measure_enrichment(root, session, client)
    assert len(calls) == 1


def test_429_is_bounded_to_three_attempts_and_preserved(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, waits = transport(monkeypatch, [response(429, b'"slow"')] * 3)
    with pytest.raises(HTTPError):
        with openf1.OpenF1Client(root) as client:
            openf1.measure_enrichment(root, session, client)
    assert len(calls) == 3 and sum(waits) >= 120
    assert len(list((root / "study/openf1" / session["session_id"]).glob("*-attempt-*.json"))) == 3


def test_actual_url_change_is_rejected_without_following(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response(url="https://other.example/drivers")])
    with pytest.raises(ValueError, match="URL|redirect"):
        with openf1.OpenF1Client(root) as client:
            openf1.measure_enrichment(root, session, client)
    assert len(calls) == 1


class FakeClient:
    def fetch(self, endpoint, session_key):
        pytest.fail("invalid source mapping must stop before transport")


@pytest.mark.parametrize(
    "damage",
    [
        "key",
        "state",
        "round",
        "stale-source",
        "missing-plan",
        "native",
        "dates",
        "missing-dates",
        "live",
    ],
)
def test_measurement_rejects_unbound_or_stale_mapping_before_writes(tmp_path, damage):
    root, session = ready(tmp_path)
    if damage in {"key", "state", "round"}:
        session[
            {"key": "openf1_session_key", "state": "state", "round": "round_number"}[damage]
        ] = {"key": 999, "state": "future", "round": 2}[damage]
    elif damage == "stale-source":
        source = root / "openf1-inventory.json"
        source.write_text(source.read_text() + " ")
    elif damage == "missing-plan":
        (root / "study/sample-plan.json").unlink()
    elif damage == "native":
        (root / "preflight" / session["session_id"] / "attempt/laps.parquet").write_bytes(
            b"corrupt"
        )
    else:
        source = json.loads((root / "openf1-inventory.json").read_text())
        provider = source["sessions"][0]
        if damage == "dates":
            provider["date_end"] = "2023-01-01T00:00:00+00:00"
        elif damage == "missing-dates":
            provider.pop("date_end")
        else:
            provider["date_start"] = datetime.now(UTC).isoformat()
            provider["date_end"] = datetime.now(UTC).isoformat()
        save(root / "openf1-inventory.json", source)
        catalog = json.loads((root / "reconciled-inventory.json").read_text())
        catalog["openf1_inventory_sha256"] = file_sha256(root / "openf1-inventory.json")
        catalog["provider_entries"][0] = {
            **provider,
            "canonical_session_id": session["session_id"],
            "reconciliation_state": "completed",
        }
        save(root / "reconciled-inventory.json", catalog)
        plan = json.loads((root / "study/sample-plan.json").read_text())
        plan["inventory_sha256"] = file_sha256(root / "reconciled-inventory.json")
        save(root / "study/sample-plan.json", plan)
    with pytest.raises((ValueError, OSError, StorageIntegrityError)):
        openf1.measure_enrichment(root, session, FakeClient())
    assert not (root / "study/openf1").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "report-key",
        "source-url",
        "source-status",
        "body-size",
        "rows",
        "canonical-size",
        "attempt-header",
        "extra-file",
    ],
)
def test_exact_resume_rejects_mutated_metadata_without_network(tmp_path, monkeypatch, damage):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    directory = root / "study/openf1" / session["session_id"]
    if damage == "extra-file":
        (directory / "unmanifested.json").write_text("{}")
    elif damage == "report-key":
        report["session_key"] = 999
        save(directory / "measurement.json", report)
    elif damage.startswith("source"):
        source = json.loads((directory / "drivers-source.json").read_text())
        source["url" if damage == "source-url" else "http_status"] = (
            "https://other.example" if damage == "source-url" else 404
        )
        save(directory / "drivers-source.json", source)
    elif damage == "attempt-header":
        path = next(directory.glob("*-attempt-*.json"))
        attempt = json.loads(path.read_text())
        attempt["headers"] = {"changed": "undetected"}
        save(path, attempt)
    else:
        record = report["endpoints"]["drivers"]
        field = {
            "body-size": "source_body_bytes",
            "rows": "rows",
            "canonical-size": "canonical_bytes",
        }[damage]
        record[field] += 1
        save(directory / "drivers.json", record)
        save(directory / "measurement.json", report)
    with pytest.raises((ValueError, StorageIntegrityError)):
        openf1.measure_enrichment(root, session, FakeClient())
    assert len(calls) == 1


def test_source_serialization_interruption_resumes_manifest_bound_attempt_without_http(
    tmp_path, monkeypatch
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    writer = openf1.write_text_atomic

    def interrupted(path, text):
        if path.name == "drivers-source.json":
            raise OSError("source metadata interrupted")
        writer(path, text)

    monkeypatch.setattr(openf1, "write_text_atomic", interrupted)
    with pytest.raises(OSError, match="source metadata interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1, "write_text_atomic", writer)
    report = openf1.measure_enrichment(root, session, FakeClient())
    assert report["endpoints"]["drivers"]["rows"] == 1
    assert len(calls) == 1 and len(report["attempts"]) == 1


def test_partial_resume_rejects_well_shaped_unreferenced_attempt_file(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    transport(monkeypatch, [response()])
    writer = openf1.pq.write_table
    monkeypatch.setattr(
        openf1.pq, "write_table", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    directory = root / "study/openf1" / session["session_id"]
    original = next(directory.glob("*-attempt-*.json"))
    (directory / "drivers-attempt-unreferenced.json").write_bytes(original.read_bytes())
    with pytest.raises(ValueError, match="attempt|manifest"):
        openf1.measure_enrichment(root, session, FakeClient())


def test_unmanifested_partial_file_stops_before_any_conversion_write(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    transport(monkeypatch, [response()])
    writer = openf1.pq.write_table
    monkeypatch.setattr(
        openf1.pq, "write_table", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    directory = root / "study/openf1" / session["session_id"]
    (directory / "rogue.txt").write_text("unmanifested")
    with pytest.raises(ValueError, match="unmanifested"):
        openf1.measure_enrichment(root, session, FakeClient())
    assert not list(directory.glob("*.parquet"))


def test_capacity_stop_precedes_request_and_directory_writes(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    import downforce_core.detailed.status as status

    monkeypatch.setattr(status.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 1})())
    with pytest.raises(status.CapacityStop):
        openf1.measure_enrichment(root, session, FakeClient())
    assert not (root / "study/openf1").exists()


def test_capacity_stop_is_sticky_for_an_already_owned_client_context(tmp_path, monkeypatch):
    from downforce_core.detailed import status

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    with pytest.raises(status.CapacityStop), openf1.OpenF1Client(root) as client:
        disk = status.shutil.disk_usage
        monkeypatch.setattr(status.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 1})())
        with pytest.raises(status.CapacityStop):
            openf1.measure_enrichment(root, session, client)
        monkeypatch.setattr(status.shutil, "disk_usage", disk)
        with pytest.raises(status.CapacityStop):
            openf1.measure_enrichment(root, session, client)
    assert not calls


def test_restart_preserves_spacing_after_minute_quota_wait(tmp_path, monkeypatch):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    clock, calls, _ = transport(monkeypatch, [response(), response()])
    minute = RequestBudget(
        root / "runtime/openf1-minute-budget.json", limit=25, interval_seconds=60
    )
    for _ in range(25):
        assert minute.reserve(now=clock[0]) == 0
    writer = openf1.pq.write_table
    monkeypatch.setattr(
        openf1.pq, "write_table", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert len(calls) == 2
    assert calls[0][0].endswith("drivers?session_key=123")
    assert calls[1][0].endswith("laps?session_key=123")
    assert calls[1][1] >= calls[0][1] + 2.1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("delay_source", ["final-check", "reservation"])
def test_minute_quota_tracks_actual_sends_after_delayed_final_check(
    tmp_path, monkeypatch, restart, delay_source
):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    clock, calls, _ = transport(monkeypatch, [response()] * 26)
    started = clock[0]
    guard = openf1_transport.guard_operation
    delayed = False

    def delayed_guard(path, *, next_peak_bytes, operation):
        nonlocal delayed
        result = guard(path, next_peak_bytes=next_peak_bytes, operation=operation)
        if (
            delay_source == "final-check"
            and not delayed
            and operation == "OpenF1 provider response"
        ):
            delayed = True
            clock[0] += 2.1
        return result

    monkeypatch.setattr(openf1_transport, "guard_operation", delayed_guard)
    reserve = RequestBudget.reserve

    def delayed_reserve(budget, *, now):
        nonlocal delayed
        result = reserve(budget, now=now)
        if (
            delay_source == "reservation"
            and not delayed
            and result == 0
            and budget.path.name == "openf1-minute-budget.json"
        ):
            delayed = True
            clock[0] += 2.1
        return result

    monkeypatch.setattr(RequestBudget, "reserve", delayed_reserve)
    directory = root / "study/openf1" / session["session_id"]
    client = openf1.OpenF1Client(root)
    with client:
        for batch, endpoints in enumerate(
            (openf1_transport.ENDPOINTS, openf1_transport.ENDPOINTS, ("drivers", "laps"))
        ):
            monkeypatch.setattr(openf1, "ENDPOINTS", endpoints)
            openf1.measure_enrichment(root, session, client)
            directory.rename(tmp_path / f"completed-{batch}")
            if restart and batch == 0:
                # Release the first worker before opening a replacement.
                break
    if restart:
        for batch, endpoints in enumerate(
            (openf1_transport.ENDPOINTS, ("drivers", "laps")), start=1
        ):
            monkeypatch.setattr(openf1, "ENDPOINTS", endpoints)
            openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
            directory.rename(tmp_path / f"completed-{batch}")
    sends = [timestamp - started for _, timestamp in calls]
    assert delayed and sends[0] == pytest.approx(2.1)
    assert len(sends) == 26
    assert sends[-1] >= sends[0] + 60, sends
    assert all(
        sum(sent > current - 60 and sent <= current for sent in sends) <= 25 for current in sends
    ), sends
    assert all(
        later[1] >= earlier[1] + 2.1 for earlier, later in zip(calls, calls[1:], strict=False)
    )
    minute = json.loads((root / "runtime/openf1-minute-budget.json").read_text())
    spacing = json.loads((root / "runtime/openf1-spacing-budget.json").read_text())
    assert minute["timestamps"][-1] == spacing["timestamps"][-1] == calls[-1][1]


def test_full_response_capacity_is_rechecked_after_final_spacing_wait(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from downforce_core.detailed import status
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, waits = transport(monkeypatch, [response()])
    started = clock[0]
    spacing = RequestBudget(
        root / "runtime/openf1-spacing-budget.json", limit=1, interval_seconds=2.1
    )
    assert spacing.reserve(now=started) == 0
    monkeypatch.setattr(
        status.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(
            free=status.HEADROOM_BYTES + (1024**3 if clock[0] == started else 2 * 1024**2)
        ),
    )
    with pytest.raises(status.CapacityStop) as failure:
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert "headroom" in str(failure.value)
    assert waits and not calls
    assert not (root / "runtime/openf1-minute-budget.json").exists()
    directory = root / "study/openf1" / session["session_id"]
    assert not list(directory.iterdir())


@pytest.mark.parametrize("interruption", ["conversion", "publication"])
def test_partial_source_cannot_reclassify_captured_200_as_unavailable(
    tmp_path, monkeypatch, interruption
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    writer = openf1.pq.write_table
    metadata_writer = openf1.write_text_atomic
    if interruption == "conversion":
        monkeypatch.setattr(
            openf1.pq,
            "write_table",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted")),
        )
    else:

        def interrupt_publication(path, text):
            if path.name == "drivers.json":
                raise OSError("interrupted publication")
            metadata_writer(path, text)

        monkeypatch.setattr(openf1, "write_text_atomic", interrupt_publication)
    with pytest.raises(OSError, match="interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    monkeypatch.setattr(openf1, "write_text_atomic", metadata_writer)
    directory = root / "study/openf1" / session["session_id"]
    source = json.loads((directory / "drivers-source.json").read_text())
    source["http_status"] = 404
    save(directory / "drivers-source.json", source)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    if interruption == "publication":
        assert len(list(directory.glob("*.parquet"))) == 1
    with pytest.raises(ValueError, match="source|attempt"):
        openf1.measure_enrichment(root, session, FakeClient())
    assert len(calls) == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("capture_failure", [False, True])
def test_denial_identity_survives_capture_and_response_close_failures(
    tmp_path, monkeypatch, capture_failure
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response(403, b'"denied"')])
    if capture_failure:
        monkeypatch.setattr(
            openf1, "encode_response", lambda _: (_ for _ in ()).throw(OSError("capture failed"))
        )
    monkeypatch.setattr(
        HTTPError, "close", lambda _: (_ for _ in ()).throw(OSError("close failed")), raising=False
    )
    with pytest.raises(PermissionError) as failure:
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert any("close failed" in note for note in failure.value.__notes__)
    if capture_failure:
        assert any("capture failed" in note for note in failure.value.__notes__)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("fault", ["capture", "read"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_denied_response_cancellation_propagates_identity_and_keeps_denial_sticky(
    tmp_path, monkeypatch, status, fault, kind
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    cancellation = kind(f"{fault} cancelled")

    class CancelledBody(BytesIO):
        def read(self, size=-1):
            if fault == "read":
                raise cancellation
            return super().read(size)

    _, calls, _ = transport(monkeypatch, [response(status, CancelledBody(b'"denied"')), response()])
    if fault == "capture":

        def cancelled_capture(_body):
            raise cancellation

        monkeypatch.setattr(openf1, "encode_response", cancelled_capture)
    client = openf1.OpenF1Client(root)
    client.__enter__()
    try:
        with pytest.raises(kind) as caught:
            openf1.measure_enrichment(root, session, client)
        assert caught.value is cancellation
        with pytest.raises(PermissionError) as sticky:
            client.check()
        assert sticky.value is client._denied
        with pytest.raises(PermissionError) as repeated:
            openf1.measure_enrichment(root, session, client)
        assert repeated.value is sticky.value
    finally:
        with pytest.raises(PermissionError) as released:
            client.__exit__(None, None, None)
    assert released.value is client._denied and len(calls) == 1
    directory = root / "study/openf1" / session["session_id"]
    assert not (directory / "laps-source.json").exists()
    assert not (directory / "measurement.json").exists()


@pytest.mark.parametrize("fault", ["capture", "capture-http404", "read", "close"])
def test_404_capture_read_or_close_failure_stops_measurement_and_stays_sticky(
    tmp_path, monkeypatch, fault
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    failure = (
        HTTPError("https://capture.example", 404, "capture failed", {}, BytesIO(b"failed"))
        if fault == "capture-http404"
        else OSError(f"{fault} failed")
    )

    class BrokenBody(BytesIO):
        def read(self, size=-1):
            if fault == "read":
                raise failure
            return super().read(size)

        def close(self):
            was_closed = self.closed
            super().close()
            if fault == "close" and not was_closed:
                raise failure

    _, calls, _ = transport(monkeypatch, [response(404, BrokenBody(b'"unavailable"')), response()])
    if fault.startswith("capture"):

        def fail_capture(_body):
            raise failure

        monkeypatch.setattr(openf1, "encode_response", fail_capture)
    client = openf1.OpenF1Client(root)
    expected = HTTPError if fault in ("capture-http404", "close") else OSError
    with pytest.raises(expected) as context_failure, client:
        with pytest.raises(expected) as caught:
            openf1.measure_enrichment(root, session, client)
        if fault == "close":
            assert caught.value.code == 404
            assert any("close failed" in note for note in caught.value.__notes__)
        else:
            assert caught.value is failure
        with pytest.raises(expected) as sticky:
            client.check()
        assert sticky.value is caught.value
    assert context_failure.value is caught.value
    assert len(calls) == 1
    directory = root / "study/openf1" / session["session_id"]
    assert not (directory / "drivers.json").exists()
    assert not (directory / "laps-source.json").exists()
    assert not (directory / "measurement.json").exists()


def test_clean_404_remains_available_for_measurement(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    _, calls, _ = transport(monkeypatch, [response(404, b'"unavailable"'), response()])
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert report["endpoints"]["drivers"]["http_status"] == 404
    assert report["endpoints"]["laps"]["rows"] == 1
    assert len(calls) == 2


def pending_attempt(root, reserved_at):
    marker = root / "runtime/openf1-pending-attempt.json"
    save(
        marker,
        {
            "schema_version": "openf1-pending-attempt-v1",
            "reserved_at": reserved_at,
            "endpoint": "drivers",
            "session_key": 123,
        },
    )
    return marker


@pytest.mark.parametrize("partial_anchor", [None, "minute", "spacing"])
def test_pending_attempt_restart_preserves_histories_and_defers_both_quotas(
    tmp_path, monkeypatch, partial_anchor
):
    clock, calls, _ = transport(monkeypatch, [])
    old = openf1.OpenF1Client(tmp_path)
    reserved_at = clock[0]
    for budget in (old.minute, old.spacing):
        assert budget.reserve(now=reserved_at) == 0
    marker = pending_attempt(tmp_path, reserved_at)
    clock[0] += 2.1
    if partial_anchor:
        budget = getattr(old, partial_anchor)
        value = json.loads(budget.path.read_text())
        value["timestamps"][-1] = clock[0]
        save(budget.path, value)
    old.spacing.defer_until(clock[0] + 90)
    before = [
        json.loads(budget.path.read_text())["timestamps"] for budget in (old.minute, old.spacing)
    ]
    with openf1.OpenF1Client(tmp_path) as restarted:
        for index, budget in enumerate((restarted.minute, restarted.spacing)):
            state = json.loads(budget.path.read_text())
            assert state["timestamps"] == before[index]
            assert state["not_before"] >= clock[0] + (90 if index else 60)
        assert not marker.exists()
    assert not calls


@pytest.mark.parametrize("damage", ["extra-field", "missing-field", "nan", "future", "endpoint"])
def test_corrupt_pending_attempt_stops_owned_worker_without_rewriting_state(
    tmp_path, monkeypatch, damage
):
    clock, calls, _ = transport(monkeypatch, [])
    marker = pending_attempt(tmp_path, clock[0])
    value = json.loads(marker.read_text())
    if damage == "extra-field":
        value["unowned"] = True
    elif damage == "missing-field":
        value.pop("reserved_at")
    elif damage in ("nan", "future"):
        value["reserved_at"] = float("nan") if damage == "nan" else clock[0] + 1
    else:
        value["endpoint"] = "sessions"
    save(marker, value)
    before = marker.read_bytes()
    client = openf1.OpenF1Client(tmp_path)
    with pytest.raises(ValueError):
        with client:
            pytest.fail("corrupt pending attempt must stop before worker authority is exposed")
    with pytest.raises(ValueError):
        client.check()
    assert not client.active and marker.read_bytes() == before and not calls
    assert not client.minute.path.exists() and not client.spacing.path.exists()
    with network_worker(tmp_path / "runtime/fastf1-worker.lock"):
        pass


def test_public_measurement_waits_full_window_after_unknown_pending_attempt(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, waits = transport(monkeypatch, [response()])
    old = openf1.OpenF1Client(root)
    for budget in (old.minute, old.spacing):
        assert budget.reserve(now=clock[0]) == 0
    marker = pending_attempt(root, clock[0])
    restarted_at = clock[0]
    openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert calls[0][1] >= restarted_at + 60 and sum(waits) >= 60
    assert not marker.exists()


@pytest.mark.parametrize("status", [200, 403, 404, 429])
def test_failed_attempt_anchor_is_sticky_keeps_pending_and_preserves_denial(
    tmp_path, monkeypatch, status
):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    _, calls, _ = transport(monkeypatch, [response(status), response(), response()])
    writer = RequestBudget._write
    marker = root / "runtime/openf1-pending-attempt.json"
    failure = OSError("anchor failed")
    directory = root / "study/openf1" / session["session_id"]

    def fail_anchor(budget, value):
        if (
            marker.exists()
            and list(directory.glob("*-attempt-*.json"))
            and budget.path.name == "openf1-spacing-budget.json"
        ):
            raise failure
        writer(budget, value)

    monkeypatch.setattr(RequestBudget, "_write", fail_anchor)
    client = openf1.OpenF1Client(root)
    expected = PermissionError if status == 403 else HTTPError if status == 404 else OSError
    with pytest.raises(expected) as caught:
        openf1.measure_enrichment(root, session, client)
    if status == 403:
        assert any("anchor failed" in note for note in getattr(caught.value, "__notes__", []))
    with pytest.raises(expected) as sticky:
        client.check()
    assert sticky.value is caught.value
    assert marker.exists() and len(calls) == 1
    attempts = list(directory.glob("*-attempt-*.json"))
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_text())["http_status"] == status
    assert not (directory / "measurement.json").exists()


def test_partial_pending_recovery_failure_keeps_marker_and_never_lowers_cooldowns(
    tmp_path, monkeypatch
):
    from downforce_core.detailed.rate_budget import RequestBudget

    clock, calls, _ = transport(monkeypatch, [])
    old = openf1.OpenF1Client(tmp_path)
    for budget in (old.minute, old.spacing):
        assert budget.reserve(now=clock[0]) == 0
    marker = pending_attempt(tmp_path, clock[0])
    old.spacing.defer_until(clock[0] + 90)
    writer = RequestBudget._write
    failure = OSError("recovery interrupted")

    def interrupted(budget, value):
        if budget.path.name == "openf1-spacing-budget.json":
            raise failure
        writer(budget, value)

    monkeypatch.setattr(RequestBudget, "_write", interrupted)
    client = openf1.OpenF1Client(tmp_path)
    with pytest.raises(OSError) as caught:
        with client:
            pytest.fail("partial recovery cannot expose worker authority")
    assert caught.value is failure and marker.exists() and not client.active
    with pytest.raises(OSError) as sticky:
        client.check()
    assert sticky.value is failure
    minute = json.loads(old.minute.path.read_text())
    assert minute["not_before"] == clock[0] + 60
    assert minute["timestamps"] == [clock[0]]
    monkeypatch.setattr(RequestBudget, "_write", writer)
    clock[0] += 1
    with openf1.OpenF1Client(tmp_path) as restarted:
        assert not marker.exists()
        minute = json.loads(restarted.minute.path.read_text())
        spacing = json.loads(restarted.spacing.path.read_text())
        assert minute["not_before"] == clock[0] + 60
        assert spacing["not_before"] == clock[0] + 89
        assert minute["timestamps"] == spacing["timestamps"] == [clock[0] - 1]
    assert not calls


def test_fresh_worker_spacing_uses_send_time_after_slow_owned_reservation(tmp_path, monkeypatch):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, _ = transport(monkeypatch, [response(), response()])
    reserve = RequestBudget.reserve
    delayed = False

    def slow_reserve(budget, *, now):
        nonlocal delayed
        result = reserve(budget, now=now)
        if not delayed and result == 0 and budget.path.name == "openf1-minute-budget.json":
            delayed = True
            clock[0] += 2.1
        return result

    monkeypatch.setattr(RequestBudget, "reserve", slow_reserve)
    openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    (root / "study/openf1" / session["session_id"]).rename(tmp_path / "first-measurement")
    openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert delayed and len(calls) == 2
    assert calls[1][1] >= calls[0][1] + 2.1


@pytest.mark.parametrize("anchor_failure", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_non_http_transport_failure_is_anchored_and_preserves_primary_identity(
    tmp_path, monkeypatch, anchor_failure, cancelled
):
    from urllib.error import URLError

    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, _, _ = transport(monkeypatch, [])
    calls = []
    primary = (
        KeyboardInterrupt("transport cancelled") if cancelled else URLError("connection failed")
    )
    marker = root / "runtime/openf1-pending-attempt.json"
    write_marker = openf1_transport.write_text_atomic
    writer = RequestBudget._write

    def slow_marker(path, text):
        write_marker(path, text)
        if path == marker:
            clock[0] += 2.1

    class Opener:
        def open(self, request, timeout):
            assert marker.exists()
            calls.append(clock[0])
            raise primary

    def failed_anchor(budget, value):
        if anchor_failure and calls and budget.path.name == "openf1-spacing-budget.json":
            raise OSError("network anchor failed")
        writer(budget, value)

    monkeypatch.setattr(openf1_transport, "write_text_atomic", slow_marker)
    monkeypatch.setattr(openf1_transport, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(RequestBudget, "_write", failed_anchor)
    client = openf1.OpenF1Client(root)
    with pytest.raises(type(primary)) as caught:
        openf1.measure_enrichment(root, session, client)
    assert caught.value is primary and len(calls) == 1
    with pytest.raises(type(primary)) as sticky:
        client.check()
    assert sticky.value is primary
    minute = json.loads(client.minute.path.read_text())
    spacing = json.loads(client.spacing.path.read_text())
    assert minute["timestamps"][-1] == calls[0]
    if anchor_failure:
        assert any("network anchor failed" in note for note in primary.__notes__)
        assert spacing["timestamps"][-1] < calls[0] and marker.exists()
        monkeypatch.setattr(RequestBudget, "_write", writer)
        with openf1.OpenF1Client(root) as restarted:
            for budget in (restarted.minute, restarted.spacing):
                assert json.loads(budget.path.read_text())["not_before"] >= clock[0] + 60
    else:
        assert spacing["timestamps"][-1] == calls[0] and not marker.exists()


@pytest.mark.parametrize("status", [200, 403, 404])
def test_anchor_cancellation_propagates_even_with_primary_http_error(tmp_path, monkeypatch, status):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers", "laps"))
    _, calls, _ = transport(monkeypatch, [response(status), response()])
    writer = RequestBudget._write
    directory = root / "study/openf1" / session["session_id"]
    cancellation = KeyboardInterrupt("anchor cancelled")

    def cancel_anchor(budget, value):
        if (
            list(directory.glob("*-attempt-*.json"))
            and budget.path.name == "openf1-spacing-budget.json"
        ):
            raise cancellation
        writer(budget, value)

    monkeypatch.setattr(RequestBudget, "_write", cancel_anchor)
    with pytest.raises(KeyboardInterrupt) as caught:
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert caught.value is cancellation and len(calls) == 1
    assert (root / "runtime/openf1-pending-attempt.json").exists()
    assert not (directory / "measurement.json").exists()


def test_response_close_cancellation_survives_ordinary_anchor_failure(tmp_path, monkeypatch):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    transport(monkeypatch, [])
    cancellation = KeyboardInterrupt("close cancelled")

    class Response(BytesIO):
        status = 200
        headers = {}

        def geturl(self):
            return openf1_transport.historical_url("drivers", 123)

        def close(self):
            was_closed = self.closed
            super().close()
            if not was_closed:
                raise cancellation

    body = Response(response()[1])

    class Opener:
        def open(self, request, timeout):
            return body

    writer = RequestBudget._write

    def failed_anchor(budget, value):
        if body.closed and budget.path.name == "openf1-spacing-budget.json":
            raise OSError("close anchor failed")
        writer(budget, value)

    monkeypatch.setattr(openf1_transport, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(RequestBudget, "_write", failed_anchor)
    with pytest.raises(KeyboardInterrupt) as caught:
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert caught.value is cancellation
    assert any("close anchor failed" in note for note in cancellation.__notes__)
    assert (root / "runtime/openf1-pending-attempt.json").exists()


def test_original_duplicate_response_headers_are_preserved_in_order(tmp_path, monkeypatch):
    from email.message import Message

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    headers = Message()
    headers.add_header("X-Trace", "first")
    headers.add_header("X-Trace", "second")
    transport(monkeypatch, [response(headers=headers)])
    openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    record = json.loads(
        next((root / "study/openf1" / session["session_id"]).glob("*-attempt-*.json")).read_text()
    )
    assert record["header_fields"] == [["X-Trace", "first"], ["X-Trace", "second"]]


def test_script_uses_current_canonical_key_without_rematching_or_reclassifying(
    tmp_path, monkeypatch
):
    import importlib.util
    import sys

    root, _ = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    script = Path(__file__).resolve().parents[4] / "scripts/measure_openf1_storage.py"
    spec = importlib.util.spec_from_file_location("test_measure_openf1_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "classify_completion",
        lambda *_a, **_k: pytest.fail("script must not reclassify canonical completion"),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "match_openf1",
        lambda *_a, **_k: pytest.fail("script must not rematch current provider key"),
        raising=False,
    )
    monkeypatch.setattr(sys, "argv", [str(script), "--root", str(root)])
    module.main()
    assert len(calls) == 1 and calls[0][0].endswith("session_key=123")


@pytest.mark.parametrize("policy", ["spacing", "minute", "backwards"])
def test_new_worker_inherits_request_history_before_transport(tmp_path, monkeypatch, policy):
    from downforce_core.detailed.rate_budget import RequestBudget

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, waits = transport(monkeypatch, [response()])
    runtime = root / "runtime"
    if policy == "minute":
        budget = RequestBudget(runtime / "openf1-minute-budget.json", limit=25, interval_seconds=60)
        for _ in range(25):
            assert budget.reserve(now=clock[0]) == 0
    else:
        budget = RequestBudget(
            runtime / "openf1-spacing-budget.json", limit=1, interval_seconds=2.1
        )
        assert budget.reserve(now=clock[0] + (1 if policy == "backwards" else 0)) == 0
    started = clock[0]
    if policy == "backwards":
        with pytest.raises(ValueError, match="clock moved backwards"):
            openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
        assert not calls
    else:
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
        assert calls[0][1] >= started + (60 if policy == "minute" else 2.1)
        assert waits


@pytest.mark.parametrize("header", ["invalid", "0", "2.1", "Fri, 09 Oct 2026 00:01:30 GMT"])
def test_retry_after_numeric_and_http_date_use_full_chunked_cooldown(tmp_path, monkeypatch, header):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, waits = transport(
        monkeypatch, [response(429, b'"slow"', {"Retry-After": header}), response()]
    )
    report = openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    delay = 90 if header.startswith("Fri") else 60
    assert len(calls) == 2 and calls[1][1] - calls[0][1] >= delay
    assert sum(waits) >= delay and all(seconds <= 60 for seconds in waits)
    directory = root / "study/openf1" / session["session_id"]
    assert len(report["attempts"]) == 2
    # Retry evidence and one success body are counted, never duplicated.
    persistent = sum(
        path.stat().st_size
        for path in directory.iterdir()
        if path.suffix != ".parquet" and path.name != "measurement.json"
    )
    assert report["openf1_raw_bytes"] == persistent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert openf1.measure_enrichment(root, session, FakeClient()) == report
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("waiting_on", ["minute", "spacing", "retry-after"])
@pytest.mark.parametrize("fault", ["scope", "capacity"])
def test_every_cooldown_chunk_revalidates_scope_and_full_response_capacity(
    tmp_path, monkeypatch, waiting_on, fault
):
    import errno
    import os
    from types import SimpleNamespace

    from downforce_core.detailed import status

    root, session = ready(tmp_path)
    plan_path = root / "study/sample-plan.json"
    original_plan = plan_path.read_bytes()
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    replies = (
        [response(429, b'"slow"', {"Retry-After": "180"}), response()]
        if waiting_on == "retry-after"
        else [response()]
    )
    clock, calls, waits = transport(monkeypatch, replies)
    client = openf1.OpenF1Client(root)
    if waiting_on != "retry-after":
        getattr(client, waiting_on).defer_until(clock[0] + 180)
    pause = openf1_transport.sleep

    def interrupted_sleep(seconds):
        pause(seconds)
        if len(waits) != 1:
            return
        if fault == "scope":
            path = root / "study/sample-plan.json"
            plan = json.loads(path.read_text())
            plan["selected_before_download"] = False
            save(path, plan)
        else:
            monkeypatch.setattr(
                status.shutil,
                "disk_usage",
                lambda _: SimpleNamespace(free=status.HEADROOM_BYTES + 2 * 1024**2),
            )

    monkeypatch.setattr(openf1_transport, "sleep", interrupted_sleep)
    expected = (
        PermissionError
        if fault == "scope" and os.name == "nt"
        else ValueError
        if fault == "scope"
        else status.CapacityStop
    )
    with pytest.raises(expected) as stopped:
        openf1.measure_enrichment(root, session, client)
    if fault == "scope":
        if os.name == "nt":
            assert stopped.value.errno == errno.EACCES or stopped.value.winerror == 32
            assert plan_path.read_bytes() == original_plan
            # The operation must release its deny-write lease even on interruption.
            with plan_path.open("r+b") as write_handle:
                assert not write_handle.closed  # Acquire write capability, never write.
        else:
            assert json.loads(plan_path.read_bytes())["selected_before_download"] is False
    assert waits == [60], waits
    assert len(calls) == (1 if waiting_on == "retry-after" else 0)
    for budget in (client.minute, client.spacing):
        if budget.path.exists():
            history = json.loads(budget.path.read_text())
            assert len(history["timestamps"]) == len(calls)
    assert not (root / "study/openf1" / session["session_id"] / "measurement.json").exists()


def test_retry_wait_limit_applies_to_total_fetch_not_each_cooldown(tmp_path, monkeypatch):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    clock, calls, waits = transport(
        monkeypatch, [response(429, b'"slow"', {"Retry-After": "180"})] * 2
    )
    with pytest.raises(ValueError, match="bounded operation wait"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    assert len(calls) == 2 and calls[1][1] - calls[0][1] >= 180
    assert waits == [60, 60, 60]
    for name in ("minute", "spacing"):
        history = json.loads((root / f"runtime/openf1-{name}-budget.json").read_text())
        assert history["not_before"] == clock[0] + 180


def test_redirect_handler_never_returns_a_followup_request():
    request = openf1_transport.Request("https://api.openf1.org/v1/drivers?session_key=123")
    assert (
        openf1_transport._NoRedirect().redirect_request(
            request, None, 302, "moved", {}, "https://other.example"
        )
        is None
    )


@pytest.mark.parametrize("status", [200, 404, 500])
def test_bounded_oversize_prefix_is_labeled_not_claimed_as_full_body(tmp_path, monkeypatch, status):
    import gzip
    import hashlib

    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    monkeypatch.setattr(openf1, "MAX_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(openf1_transport, "MAX_RESPONSE_BYTES", 1024)
    original = b"oversized" * 300
    transport(monkeypatch, [response(status, original)])
    with pytest.raises(ValueError, match="exceeded bounded"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    directory = root / "study/openf1" / session["session_id"]
    record = json.loads(next(directory.glob("*-attempt-*.json")).read_text())
    assert record["body_truncated"] is True
    assert record["source_body_bytes"] == 1024
    assert gzip.decompress((directory / record["raw_file"]).read_bytes()) == original[:1024]
    assert record["response_sha256"] == hashlib.sha256(original[:1024]).hexdigest()
    assert not (directory / "drivers-source.json").exists()
    assert not (directory / "measurement.json").exists()


@pytest.mark.parametrize("malformed", ["array", "bad-status", "wrong-name", "size", "session"])
def test_partial_wire_source_metadata_is_checked_before_conversion_or_transport(
    tmp_path, monkeypatch, malformed
):
    root, session = ready(tmp_path)
    monkeypatch.setattr(openf1, "ENDPOINTS", ("drivers",))
    _, calls, _ = transport(monkeypatch, [response()])
    writer = openf1.pq.write_table
    monkeypatch.setattr(
        openf1.pq, "write_table", lambda *_a, **_k: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError, match="interrupted"):
        openf1.measure_enrichment(root, session, openf1.OpenF1Client(root))
    monkeypatch.setattr(openf1.pq, "write_table", writer)
    directory = root / "study/openf1" / session["session_id"]
    path = directory / "drivers-source.json"
    source = json.loads(path.read_text())
    if malformed == "array":
        path.write_text("[]")
    else:
        source[
            {
                "bad-status": "http_status",
                "wrong-name": "raw_file",
                "size": "source_body_bytes",
                "session": "url",
            }[malformed]
        ] = {
            "bad-status": 401,
            "wrong-name": "missing.gz",
            "size": 0,
            "session": "https://api.openf1.org/v1/drivers?session_key=999",
        }[malformed]
        save(path, source)
    with pytest.raises((ValueError, StorageIntegrityError)):
        openf1.measure_enrichment(root, session, FakeClient())
    assert len(calls) == 1
