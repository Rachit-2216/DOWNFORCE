"""Production enrichment: real authority/store proofs, fake external HTTP only."""

import gzip
import importlib
import importlib.util
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from downforce_core.detailed import openf1_transport
from downforce_core.detailed.cli import project_study
from downforce_core.detailed.paths import DetailedPaths
from downforce_core.detailed.rate_budget import RequestBudget
from downforce_core.detailed.raw_evidence import RawEvidenceStore, source_manifest
from downforce_core.detailed.storage import DetailedStore
from downforce_core.storage.parquet import file_sha256


def module() -> Any:
    name = "downforce_core.detailed.corpus_openf1"
    assert importlib.util.find_spec(name) is not None, "production OpenF1 enrichment is missing"
    return importlib.import_module(name)


def save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False))


def ready(tmp_path: Path, *, additional: int = 0) -> DetailedPaths:
    fixtures = importlib.import_module("test_detailed_proofs")
    paths, _ = fixtures.study(tmp_path, modern=True)
    assert isinstance(paths, DetailedPaths)
    root = paths.root
    if additional:
        discovery = json.loads((root / "inventory.json").read_text())
        catalog = json.loads((root / "reconciled-inventory.json").read_text())
        provider = json.loads((root / "openf1-inventory.json").read_text())
        for n in range(2, additional + 2):
            identity = f"2024-{n:02d}-race"
            original = {**discovery["sessions"][0], "session_id": identity, "round_number": n}
            row = {**catalog["sessions"][0], **original, "openf1_session_key": 122 + n}
            discovery["sessions"].append(original)
            catalog["sessions"].append(row)
            source = json.loads((root / "completion/2024-01-race.json").read_text())
            save(root / f"completion/{identity}.json", {**source, "session_id": identity})
            catalog["completion_evidence_sha256"][identity] = file_sha256(
                root / f"completion/{identity}.json"
            )
            native_provider = {**provider["sessions"][0], "session_key": 122 + n}
            provider["sessions"].append(native_provider)
            catalog["provider_entries"].append(
                {
                    **native_provider,
                    "canonical_session_id": identity,
                    "reconciliation_state": "completed",
                }
            )
        save(root / "inventory.json", discovery)
        save(root / "openf1-inventory.json", provider)
        catalog["discovery_sha256"] = file_sha256(root / "inventory.json")
        catalog["openf1_inventory_sha256"] = file_sha256(root / "openf1-inventory.json")
        save(root / "reconciled-inventory.json", catalog)
        plan = json.loads((root / "study/sample-plan.json").read_text())
        plan["inventory_sha256"] = file_sha256(root / "reconciled-inventory.json")
        save(root / "study/sample-plan.json", plan)
    fixtures.enrichment(paths)  # existing measurement fake only, authentic immutable proof
    project_study(paths)
    store = DetailedStore(tmp_path, detailed_root=paths.root)
    for row in json.loads((root / "reconciled-inventory.json").read_text())["sessions"]:
        raw = tmp_path / "fixture-native" / row["session_id"]
        raw.mkdir(parents=True)
        (raw / "source.bin").write_bytes(b"native provider fixture")
        bundle = RawEvidenceStore(paths.raw).pack(
            raw, session_id=row["session_id"], provider="fastf1", provider_version="test"
        )
        store.publish(
            row["session_id"],
            {
                "laps": pa.table({"driver_number": ["44"], "lap_number": [1.0]}),
                "results": pa.table({"driver_number": ["44"]}),
            },
            {
                "session": row,
                "provider": "fastf1",
                "provider_version": "test",
                "raw_bundle_sha256": bundle,
            },
        )
    return paths


@dataclass
class Reply:
    status: int = 200
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "application/json"),)
    actual_url: str | None = None
    close_failure: bool = False


def transport(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[Reply],
    *,
    before_send: Callable[[], None] | None = None,
) -> tuple[list[tuple[str, float]], list[float]]:
    clock = [datetime.now(UTC).timestamp()]
    calls: list[tuple[str, float]] = []
    waits: list[float] = []

    class Body(BytesIO):
        def __init__(self, body: bytes, *, close_failure: bool = False) -> None:
            super().__init__(body)
            self.close_failure = close_failure

        def close(self) -> None:
            # Release the synthetic resource, then expose one genuine cleanup
            # failure. Later finalizers can close cleanly without unraisable errors.
            super().close()
            if self.close_failure:
                self.close_failure = False
                raise OSError("synthetic response close failure")

    class Response(Body):
        def __init__(
            self, body: bytes, url: str, headers: Message, *, close_failure: bool = False
        ) -> None:
            super().__init__(body, close_failure=close_failure)
            self.url, self.status, self.headers = url, 200, headers

        def geturl(self) -> str:
            return self.url

    class Opener:
        def open(self, request: Request, timeout: int) -> Response:
            if before_send is not None:
                before_send()
            calls.append((request.full_url, clock[0]))
            reply = replies.pop(0) if replies else Reply()
            key = int(request.full_url.split("session_key=")[1])
            body = (
                reply.body
                if reply.body is not None
                else json.dumps([{"session_key": key, "native_extra": [1, 2]}]).encode()
            )
            headers = Message()
            for name, value in reply.headers:
                headers[name] = value
            url = reply.actual_url or request.full_url
            if reply.status != 200:
                raise HTTPError(
                    url,
                    reply.status,
                    "synthetic",
                    headers,
                    Body(body, close_failure=reply.close_failure),
                )
            return Response(body, url, headers, close_failure=reply.close_failure)

    def pause(seconds: float) -> None:
        assert 0 < seconds <= 60
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(openf1_transport, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(openf1_transport, "time", lambda: clock[0])
    monkeypatch.setattr(openf1_transport, "monotonic", lambda: clock[0])
    monkeypatch.setattr(openf1_transport, "sleep", pause)
    return calls, waits


def test_production_enrichment_is_separate_and_resumes_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    frozen = source_manifest(paths.root / "study")
    calls, _ = transport(monkeypatch, [])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert result["enrichment_completed"] == 1 and result["native_completed"] == 1
    assert result["mapped_subset_complete"] is True and result["corpus_complete"] is False
    assert len(calls) == 12
    leaf = paths.root / "openf1/2024-01-race"
    assert (leaf / "manifest.json").is_file()
    assert source_manifest(paths.root / "study") == frozen
    before = {path: (file_sha256(path), path.stat().st_mtime_ns) for path in leaf.iterdir()}
    again = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert again["reused"] == 1 and len(calls) == 12
    assert before == {path: (file_sha256(path), path.stat().st_mtime_ns) for path in leaf.iterdir()}


def test_all_mapped_completed_sessions_not_only_frozen_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path, additional=1)
    calls, _ = transport(monkeypatch, [])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert result["selected"] == 2 and result["enrichment_completed"] == 2
    assert len(calls) == 24
    assert (paths.root / "openf1/2024-02-race/manifest.json").is_file()


def test_404_empty_and_team_radio_are_honest_native_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    transport(monkeypatch, [Reply(404, b'{"detail":"not found"}'), Reply(body=b"[]")])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    endpoints = result["sessions"][0]["endpoints"]
    assert endpoints["drivers"]["availability"] == "unavailable"
    assert endpoints["drivers"]["http_status"] == 404
    assert endpoints["laps"]["availability"] == "empty-provider-response"
    assert result["radio_audio_downloaded"] is False
    assert result["overlapping_car_position_duplicated"] is False
    leaf = paths.root / "openf1/2024-01-race"
    assert (
        gzip.decompress((leaf / endpoints["drivers"]["raw_file"]).read_bytes())
        == b'{"detail":"not found"}'
    )


@pytest.mark.parametrize("code", [401, 403])
def test_permission_denial_is_persisted_and_stops_all_following_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    paths = ready(tmp_path)
    body = b' {"detail":"denied", "exact":1.00000000001}\n'
    calls, _ = transport(monkeypatch, [Reply(code, body, (("X-Reason", "denied"),))])
    with pytest.raises(PermissionError):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 1
    leaf = paths.root / "openf1/2024-01-race"
    attempt = json.loads(next(leaf.glob("*-attempt-*.json")).read_text())
    assert attempt["http_status"] == code and attempt["headers"]["X-Reason"] == "denied"
    assert gzip.decompress((leaf / attempt["raw_file"]).read_bytes()) == body
    with pytest.raises(PermissionError):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 1


def test_new_and_mixed_fields_and_missing_null_presence_survive_exact_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    original = (
        b' [{"session_key":123,"interval":1.125,"mixed":1},'
        b'{"session_key":123,"interval":"+1 LAP","mixed":"text","later":null,'
        b'"segments":[1,2]}]\n'
    )
    transport(monkeypatch, [Reply(body=original)])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    record = result["sessions"][0]["endpoints"]["drivers"]
    leaf = paths.root / "openf1/2024-01-race"
    with pq.ParquetFile(leaf / record["canonical_file"]) as reader:
        table = reader.read()
    assert set(("session_key", "interval", "mixed", "later", "segments")).issubset(
        table.column_names
    )
    assert module().restore_rows(table) == json.loads(original)
    assert gzip.decompress((leaf / record["raw_file"]).read_bytes()) == original


def test_scope_limit_counts_do_not_claim_full_native_or_step9_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path, additional=1)
    transport(monkeypatch, [])
    result = module().run_openf1_corpus(
        tmp_path, detailed_root=paths.root, session_ids={"2024-02-race"}, limit=1
    )
    assert result["selected"] == 1 and result["catalog_mapped_completed"] == 2
    assert result["scope_complete"] is True and result["mapped_subset_complete"] is False
    assert result["native_completed"] == 2 and result["enrichment_completed"] == 1


def test_changed_canonical_native_during_request_stops_before_second_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    count = [0]

    def mutate() -> None:
        count[0] += 1
        if count[0] == 1:
            pointer = paths.root / "canonical/2024-01-race/active.json"
            pointer.write_text(pointer.read_text() + " ")

    calls, _ = transport(monkeypatch, [], before_send=mutate)
    with pytest.raises(ValueError, match="native|authority|changed"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 1


def test_rejects_forged_storage_gate_before_any_output(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, override=tmp_path / "detailed")
    save(paths.root / "stratified-projection.json", {"safe_to_sync": True})
    with pytest.raises(ValueError, match="verified storage"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert not (paths.root / "openf1").exists()


def test_hourly_exhaustion_is_fatal_before_http_and_keeps_existing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(paths.root)
    stamp = openf1_transport.time() - 1  # type: ignore[attr-defined]
    client.hour._write(
        {"limit": 650, "interval": 3600.0, "timestamps": [stamp] * 650, "not_before": 0.0}
    )
    with pytest.raises(module().HourlyBudgetStop):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root, client=client)
    assert calls == []
    assert client.hour._read()["timestamps"] == [stamp] * 650
    with pytest.raises(module().HourlyBudgetStop):
        client.check()


def test_legacy_budget_migration_uses_latest_source_mtime_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    now = openf1_transport.time()  # type: ignore[attr-defined]
    client.minute._write(
        {"limit": 25, "interval": 60.0, "timestamps": [now - 300], "not_before": 0.0}
    )
    client.spacing._write(
        {"limit": 1, "interval": 2.1, "timestamps": [now - 200], "not_before": 0.0}
    )
    os.utime(client.minute.path, (now - 120, now - 120))
    os.utime(client.spacing.path, (now - 60, now - 60))
    before = client.minute.path.read_bytes(), client.spacing.path.read_bytes()
    # Quota failure is latched for the whole worker, including its exit boundary.
    with pytest.raises(module().HourlyBudgetStop):
        with client:
            assert client.hour._read()["not_before"] == now - 60 + 3600
            with client._historical_scope(123, lambda: None, lambda *_args: None):
                client.fetch("drivers", 123)
    assert calls == []
    # Existing reservation rules may conservatively consume a minute slot;
    # they must never erase its original request or rewrite its policy.
    assert json.loads(before[0])["timestamps"][0] in client.minute._read()[
        "timestamps"
    ] or stamp_is_expired(now - 300, now, 60)


def stamp_is_expired(stamp: float, now: float, interval: float) -> bool:
    return stamp <= now - interval


def test_old_legacy_history_needs_no_hour_long_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, waits = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    now = openf1_transport.time()  # type: ignore[attr-defined]
    client.minute._write(
        {"limit": 25, "interval": 60.0, "timestamps": [now - 7200], "not_before": 0.0}
    )
    os.utime(client.minute.path, (now - 7200, now - 7200))
    with client, client._historical_scope(123, lambda: None, lambda *_args: None):
        client.fetch("drivers", 123)
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize("broken", ["future", "malformed", "boolean"])
def test_hourly_history_rejects_future_or_malformed_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    now = openf1_transport.time()  # type: ignore[attr-defined]
    stamp: Any = (
        now + 1 if broken == "future" else True if broken == "boolean" else "not-a-timestamp"
    )
    client.hour._write({"limit": 650, "interval": 3600.0, "timestamps": [stamp], "not_before": 0.0})
    before = client.hour.path.read_bytes()
    with pytest.raises(ValueError):
        with client:
            pass
    assert calls == [] and client.hour.path.read_bytes() == before


def test_crash_pending_hour_defers_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    now = openf1_transport.time()  # type: ignore[attr-defined]
    stamp = now - 10
    client.hour._write({"limit": 650, "interval": 3600.0, "timestamps": [stamp], "not_before": 0.0})
    save(client.hour_pending, {"reserved_at": stamp, "endpoint": "drivers", "session_key": 123})
    with client:
        assert client.hour._read()["timestamps"] == [stamp]
        assert client.hour._read()["not_before"] == now + 3600
    assert calls == []


def test_actual_send_hour_is_anchored_before_base_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    original = openf1_transport.OpenF1Client._finish_attempt
    observations: list[float] = []

    def inspect(
        self: Any, reserved_at: float, sent_at: float, primary: BaseException | None
    ) -> None:
        assert self.hour._read()["timestamps"][-1] == sent_at
        assert self.hour_pending.exists() and self._pending_path.exists()
        observations.append(sent_at)
        original(self, reserved_at, sent_at, primary)

    monkeypatch.setattr(openf1_transport.OpenF1Client, "_finish_attempt", inspect)
    with client, client._historical_scope(123, lambda: None, lambda *_args: None):
        client.fetch("drivers", 123)
    assert observations == [calls[0][1]]


def test_optional_500_does_not_poison_following_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = transport(monkeypatch, [Reply(500, b'"unavailable"'), Reply()])
    client = module().CorpusOpenF1Client(tmp_path)
    with client, client._historical_scope(123, lambda: None, lambda *_args: None):
        with pytest.raises(HTTPError):
            client.fetch("drivers", 123)
        client.fetch("laps", 123)
    assert len(calls) == 2


def test_lossless_json_fallback_preserves_arbitrary_nested_and_huge_values() -> None:
    rows = [
        {"a": 2**128, "nested": {"b": [True, 1, "x", None]}, "absent": None},
        {"a": -0.0, "nested": [1, {"b": "x"}]},
    ]
    table = module().lossless_table(rows)
    assert module().restore_rows(table) == rows
    assert module().restore_rows(module().lossless_table([])) == []


def test_conflicting_provider_row_identity_stops_whole_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path, additional=1)
    calls, _ = transport(monkeypatch, [Reply(body=b'[{"session_key":999}]')])
    with pytest.raises(ValueError, match="identity|authority"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 1


def test_malformed_optional_response_is_recorded_and_next_endpoint_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [Reply(body=b'{"detail":"provider unavailable"}')])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    assert result["sessions"][0]["failures"]["drivers"]["state"] == "failed"
    assert "session_result" in result["sessions"][0]["endpoints"]
    assert result["enrichment_completed"] == 0 and result["scope_complete"] is False


def test_corrupt_completed_raw_resume_never_refreshes_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    record = result["sessions"][0]["endpoints"]["drivers"]
    raw = paths.root / "openf1/2024-01-race" / record["raw_file"]
    raw.write_bytes(raw.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="corrupt|authority"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12


def test_completed_manifest_semantics_cannot_be_forged_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [])
    module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    manifest_path = paths.root / "openf1/2024-01-race/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["state"] = "failed"
    save(manifest_path, manifest)
    with pytest.raises(ValueError, match="manifest|authority"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12


def test_corrupt_manifest_references_stop_before_new_session_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path, additional=1)
    calls, _ = transport(monkeypatch, [])
    module().run_openf1_corpus(tmp_path, detailed_root=paths.root, session_ids={"2024-01-race"})
    manifest_path = paths.root / "openf1/2024-01-race/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    name = next(iter(manifest["attempts"]))
    manifest["attempts"][name] = "0" * 64
    save(manifest_path, manifest)
    with pytest.raises(ValueError):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12


def test_hour_send_anchor_precedes_base_pending_clear_and_retry_slots_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [Reply(429, b'"slow"'), Reply()])
    client = module().CorpusOpenF1Client(paths.root)
    module().run_openf1_corpus(tmp_path, detailed_root=paths.root, client=client)
    assert len(calls) == 13
    assert client.hour._read()["timestamps"] == [sent for _, sent in calls]
    assert not (paths.root / "runtime/corpus-openf1-hour-pending.json").exists()


@pytest.mark.parametrize("boundary", ["admission", "global-count"])
@pytest.mark.parametrize("alias", ["boolean-round", "float-season"])
def test_native_canonical_metadata_rejects_equal_but_wrong_json_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, alias: str
) -> None:
    paths = ready(tmp_path, additional=1 if boundary == "global-count" else 0)
    store = DetailedStore(tmp_path, detailed_root=paths.root)
    identity = "2024-01-race"
    manifest = store.validate(identity)
    metadata = manifest["metadata"]
    metadata["session"]["round_number" if alias == "boolean-round" else "season"] = (
        True if alias == "boolean-round" else 2024.0
    )
    tables = {name: store.read_table(identity, name) for name in manifest["tables"]}
    store.publish(identity, tables, metadata)
    assert store.validate(identity)["metadata"] == metadata  # Genuine, correctly hashed revision.
    calls, _ = transport(monkeypatch, [])
    with pytest.raises(ValueError, match="native|canonical|authority"):
        module().run_openf1_corpus(
            tmp_path,
            detailed_root=paths.root,
            session_ids={"2024-02-race"} if boundary == "global-count" else None,
        )
    assert len(calls) == (12 if boundary == "global-count" else 0)


@pytest.mark.parametrize("budget_name", ["minute", "spacing"])
@pytest.mark.parametrize("field", ["timestamps", "not_before"])
@pytest.mark.parametrize("boundary", ["startup", "reservation"])
def test_existing_hour_never_hides_boolean_legacy_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    field: str,
    boundary: str,
) -> None:
    calls, _ = transport(monkeypatch, [])
    client = module().CorpusOpenF1Client(tmp_path)
    client.hour._write({"limit": 650, "interval": 3600.0, "timestamps": [], "not_before": 0.0})
    budget: RequestBudget = getattr(client, budget_name)
    value = {
        "limit": budget.limit,
        "interval": budget.interval,
        "timestamps": [],
        "not_before": 0.0,
    }

    def corrupt() -> bytes:
        value[field] = [True] if field == "timestamps" else True
        budget._write(value)
        return budget.path.read_bytes()

    before = corrupt() if boundary == "startup" else b""
    with pytest.raises(ValueError, match="malformed|history"):
        with client:
            if boundary == "reservation":
                before = corrupt()
            with client._historical_scope(123, lambda: None, lambda *_args: None):
                client.fetch("drivers", 123)
    assert calls == [] and budget.path.read_bytes() == before
    with pytest.raises(ValueError):
        client.check()


@pytest.mark.parametrize("budget_name", ["minute", "spacing"])
@pytest.mark.parametrize("field", ["timestamps", "not_before"])
def test_legacy_history_is_revalidated_before_any_send_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget_name: str, field: str
) -> None:
    client = module().CorpusOpenF1Client(tmp_path)
    base_finish = openf1_transport.OpenF1Client._finish_attempt
    base_calls: list[bool] = []
    preserved: list[bytes] = []
    budget: RequestBudget = getattr(client, budget_name)

    def mutate() -> None:
        value = budget._read()
        value[field] = [True] if field == "timestamps" else True
        budget._write(value)
        preserved.append(budget.path.read_bytes())

    def inspect(self: Any, reserved: float, sent: float, primary: BaseException | None) -> None:
        base_calls.append(True)
        base_finish(self, reserved, sent, primary)

    calls, _ = transport(monkeypatch, [], before_send=mutate)
    monkeypatch.setattr(openf1_transport.OpenF1Client, "_finish_attempt", inspect)
    with pytest.raises(ValueError, match="malformed|history"):
        with client, client._historical_scope(123, lambda: None, lambda *_args: None):
            client.fetch("drivers", 123)
    assert len(calls) == 1 and base_calls == []
    assert budget.path.read_bytes() == preserved[0]
    assert client.hour_pending.exists() and client._pending_path.exists()


def test_truncated_live_headers_preserve_first_attempt_and_stop_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    body = b' [{"session_key":123}]\n'
    calls, _ = transport(monkeypatch, [Reply(body=body, headers=(("X-Large", "x" * 65536),))])
    with pytest.raises(ValueError, match="bounded|header|capture"):
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    leaf = paths.root / "openf1/2024-01-race"
    assert len(calls) == 1 and not (leaf / "manifest.json").exists()
    attempt = json.loads(next(leaf.glob("*-attempt-*.json")).read_text())
    assert attempt["http_status"] == 200 and attempt["headers_truncated"] is True
    assert gzip.decompress((leaf / attempt["raw_file"]).read_bytes()) == body


def test_invalid_utf8_optional_body_retains_original_and_continues_other_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    body = b'[{"session_key":123,"invalid":"\xff"}]'
    calls, _ = transport(monkeypatch, [Reply(body=body)])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    row = result["sessions"][0]
    assert row["failures"]["drivers"]["state"] == "failed"
    assert len(row["endpoints"]) == 11 and "session_result" in row["endpoints"]
    assert result["enrichment_completed"] == 0 and result["scope_complete"] is False
    leaf = paths.root / "openf1/2024-01-race"
    assert not (leaf / "manifest.json").exists()
    attempt = json.loads(next(leaf.glob("drivers-attempt-*.json")).read_text())
    assert attempt["http_status"] == 200
    assert gzip.decompress((leaf / attempt["raw_file"]).read_bytes()) == body


@pytest.mark.parametrize("code", [404, 500])
def test_response_close_failure_remains_fatal_after_optional_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    paths = ready(tmp_path)
    body = b' {"detail":"optional endpoint unavailable"}\n'
    calls, _ = transport(monkeypatch, [Reply(code, body, close_failure=True)])
    client = module().CorpusOpenF1Client(paths.root)
    with pytest.raises((HTTPError, OSError)) as stopped:
        module().run_openf1_corpus(tmp_path, detailed_root=paths.root, client=client)
    assert len(calls) == 1
    notes = tuple(getattr(stopped.value, "__notes__", ()))
    assert notes
    with pytest.raises((HTTPError, OSError)) as latched:
        client.check()
    assert latched.value is stopped.value
    assert tuple(getattr(latched.value, "__notes__", ())) == notes
    leaf = paths.root / "openf1/2024-01-race"
    assert not (leaf / "manifest.json").exists()
    attempt = json.loads(next(leaf.glob("drivers-attempt-*.json")).read_text())
    assert attempt["http_status"] == code
    assert gzip.decompress((leaf / attempt["raw_file"]).read_bytes()) == body


def test_unclassified_secondary_transport_notes_are_conservatively_latched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = transport(monkeypatch, [Reply(500, b'{"detail":"optional"}')])
    original = openf1_transport.OpenF1Client._finish_attempt

    def annotate(self: Any, reserved: float, sent: float, primary: BaseException | None) -> None:
        if isinstance(primary, HTTPError):
            primary.add_note("opaque secondary failure without a recognized cleanup phrase")
        original(self, reserved, sent, primary)

    monkeypatch.setattr(openf1_transport.OpenF1Client, "_finish_attempt", annotate)
    client = module().CorpusOpenF1Client(tmp_path)
    with pytest.raises(HTTPError) as stopped:
        with client, client._historical_scope(123, lambda: None, lambda *_args: None):
            with pytest.raises(HTTPError):
                client.fetch("drivers", 123)
            client.check()
            client.fetch("laps", 123)
    assert len(calls) == 1
    assert tuple(stopped.value.__notes__) == (
        "opaque secondary failure without a recognized cleanup phrase",
    )
    with pytest.raises(HTTPError) as latched:
        client.check()
    assert latched.value is stopped.value


@pytest.mark.parametrize("code", [404, 500])
def test_clean_optional_http_error_still_allows_remaining_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [Reply(code, b'{"detail":"unavailable"}')])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    row = result["sessions"][0]
    assert "session_result" in row["endpoints"]
    if code == 404:
        assert row["endpoints"]["drivers"]["availability"] == "unavailable"
        assert row["endpoints"]["drivers"]["http_status"] == 404
        assert result["enrichment_completed"] == 1
    else:
        assert row["failures"]["drivers"]["state"] == "failed"
        assert result["enrichment_completed"] == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'[{"session_key":123,"value":NaN}]', id="nan"),
        pytest.param(b'[{"session_key":123,"value":Infinity}]', id="infinity"),
        pytest.param(b'[{"session_key":123,"value":-Infinity}]', id="minus-infinity"),
        pytest.param(b'[{"session_key":123,"value":1e400}]', id="overflow"),
        pytest.param(b'[{"session_key":123,"nested":[{"value":NaN}]}]', id="nested-nan"),
        pytest.param(b'[{"session_key":123,"value":"\\ud800"}]', id="surrogate-value"),
        pytest.param(b'[{"session_key":123,"value":"\\udfff"}]', id="low-surrogate-value"),
        pytest.param(b'[{"session_key":123,"\\ud800":1}]', id="surrogate-key"),
        pytest.param(b'[{"session_key":123,"nested":{"\\ud800":1}}]', id="nested-surrogate-key"),
    ],
)
def test_nonfinite_or_illformed_unicode_is_an_authenticated_optional_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [Reply(body=body)])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    row = result["sessions"][0]
    assert row["failures"]["drivers"]["state"] == "failed"
    assert len(row["endpoints"]) == 11 and "session_result" in row["endpoints"]
    assert result["enrichment_completed"] == 0 and result["scope_complete"] is False
    assert result["mapped_subset_complete"] is False and result["corpus_complete"] is False
    leaf = paths.root / "openf1/2024-01-race"
    assert (leaf / "drivers-failure.json").is_file()
    assert not (leaf / "manifest.json").exists()
    attempt = json.loads(next(leaf.glob("drivers-attempt-*.json")).read_text())
    assert attempt["http_status"] == 200
    offset = datetime.fromisoformat(attempt["observed_at"]).utcoffset()
    assert offset is not None and offset.total_seconds() == 0
    assert gzip.decompress((leaf / attempt["raw_file"]).read_bytes()) == body


def test_valid_escaped_surrogate_pair_remains_lossless_provider_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    body = b'[{"session_key":123,"value":"\\ud83d\\ude80"}]'
    calls, _ = transport(monkeypatch, [Reply(body=body)])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12 and result["enrichment_completed"] == 1
    record = result["sessions"][0]["endpoints"]["drivers"]
    leaf = paths.root / "openf1/2024-01-race"
    with pq.ParquetFile(leaf / record["canonical_file"]) as reader:
        restored = module().restore_rows(reader.read())
    assert restored == json.loads(body)
    assert gzip.decompress((leaf / record["raw_file"]).read_bytes()) == body


def test_product_coverage_reports_only_generic_completion_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ready(tmp_path)
    calls, _ = transport(monkeypatch, [])
    result = module().run_openf1_corpus(tmp_path, detailed_root=paths.root)
    assert len(calls) == 12
    progress = json.loads((paths.root / "runtime/corpus-openf1-progress.json").read_text())
    for value in (result, progress):
        assert "step9_complete" not in value
        assert value["corpus_complete"] is False
        assert value["mapped_subset_complete"] is True
