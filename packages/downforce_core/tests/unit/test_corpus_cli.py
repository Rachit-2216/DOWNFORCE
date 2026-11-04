import json
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from downforce_core.cli import main
from downforce_core.detailed import corpus_ingest, corpus_openf1, study_runner
from downforce_core.detailed.raw_evidence import RawEvidenceStore
from downforce_core.detailed.storage import DetailedStore


def test_sync_dispatches_exact_scope_and_streams_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def run(project: Path, **kwargs: Any) -> dict[str, Any]:
        captured.update(project=project, **kwargs)
        kwargs["report"]({"session_id": "2024-01-race", "state": "complete"})
        return {"complete": False, "scope_complete": True, "selected": 1}

    monkeypatch.setattr(corpus_ingest, "run_corpus", run)
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "detailed",
                "sync",
                "--session",
                "2024-01-race",
                "--session-type",
                "race",
                "--season",
                "2024",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["scope_complete"] is True
    assert json.loads(output.err)["session_id"] == "2024-01-race"
    assert captured["project"] == tmp_path
    assert captured["session_ids"] == {"2024-01-race"}
    assert captured["session_types"] == {"race"}
    assert captured["season"] == 2024
    assert captured["limit"] == 1


def test_sync_requires_an_explicit_scope(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--root", str(tmp_path), "detailed", "sync"])
    assert error.value.code == 2


def test_enrichment_sync_is_separate_from_primary_capture_and_cache_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def enrich(project: Path, **kwargs: Any) -> dict[str, Any]:
        captured.update(project=project, **kwargs)
        kwargs["report"]({"session_id": "2024-01-race", "state": "complete"})
        return {"mapped_subset_complete": False, "step9_complete": False}

    monkeypatch.setattr(corpus_openf1, "run_openf1_corpus", enrich)
    monkeypatch.setattr(
        corpus_ingest,
        "run_corpus",
        lambda *_args, **_kwargs: pytest.fail("primary must not be called"),
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "detailed",
                "sync",
                "--provider",
                "openf1",
                "--session",
                "2024-01-race",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["step9_complete"] is False
    assert json.loads(output.err)["state"] == "complete"
    assert captured["session_ids"] == {"2024-01-race"}
    assert "release_cache" not in captured
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "detailed",
                "sync",
                "--provider",
                "openf1",
                "--session",
                "2024-01-race",
                "--release-cache",
            ]
        )
        == 1
    )
    assert "primary" in json.loads(capsys.readouterr().err)["message"]


def test_owned_cache_release_requires_an_explicit_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    options: list[bool] = []

    def run(_project: Path, **kwargs: Any) -> dict[str, Any]:
        options.append(kwargs["release_cache"])
        return {"scope_complete": True}

    monkeypatch.setattr(corpus_ingest, "run_corpus", run)
    for flag in ([], ["--release-cache"]):
        assert (
            main(["--root", str(tmp_path), "detailed", "sync", "--session", "2024-01-race", *flag])
            == 0
        )
        capsys.readouterr()
    assert options == [False, True]


def test_provider_denial_is_a_single_operation_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def run(_project: Path, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise PermissionError("provider access denied; operation stopped")

    monkeypatch.setattr(corpus_ingest, "run_corpus", run)
    assert main(["--root", str(tmp_path), "detailed", "sync", "--all-completed"]) == 1
    assert calls == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"] == "PermissionError"


@pytest.mark.parametrize("action", ["status", "validate"])
def test_empty_product_status_is_read_only_and_not_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    assert main(["--root", str(tmp_path), "detailed", action]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["published_sessions"] == 0
    assert value["complete"] is False
    assert not (tmp_path / ".downforce").exists()


def test_discover_reuses_verified_source_identity_without_telemetry_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = {
        "sessions": [
            {"session_id": "2024-01-race", "state": "completed"},
            {"session_id": "2026-24-race", "state": "future"},
        ]
    }
    monkeypatch.setattr(study_runner, "ready_inventory", lambda _root: (catalog, {}))
    assert main(["--root", str(tmp_path), "detailed", "discover"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["discovered"] == 2
    assert value["states"] == {"completed": 1, "future": 1}
    assert value["provider_called"] is False
    assert not (tmp_path / ".downforce").exists()


def test_repair_resumes_one_explicit_session_without_a_force_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected: list[set[str] | None] = []

    def run(_project: Path, **kwargs: Any) -> dict[str, Any]:
        selected.append(kwargs["session_ids"])
        return {"scope_complete": True}

    monkeypatch.setattr(corpus_ingest, "run_corpus", run)
    assert main(["--root", str(tmp_path), "detailed", "repair", "--session", "2024-01-race"]) == 0
    assert json.loads(capsys.readouterr().out)["scope_complete"] is True
    assert selected == [{"2024-01-race"}]
    with pytest.raises(SystemExit) as error:
        main(["--root", str(tmp_path), "detailed", "repair"])
    assert error.value.code == 2


def test_catalog_rebuild_does_not_publish_when_authority_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "detailed", "catalog", "rebuild"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / ".downforce").exists()


def test_validate_checks_raw_and_checkpoint_not_only_the_canonical_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".downforce" / "detailed"
    source = tmp_path / "source"
    source.mkdir()
    (source / "observed.bin").write_bytes(b"original provider observations")
    identity = "2024-01-race"
    bundle = RawEvidenceStore(root / "raw").pack(
        source, session_id=identity, provider="fastf1", provider_version="test"
    )
    store = DetailedStore(tmp_path)
    revision = store.publish(
        identity,
        {"laps": pa.table({"lap_number": [1]})},
        {
            "quality": "partial",
            "raw_bundle_sha256": bundle,
            "session": {"session_id": identity, "state": "completed"},
            "channels": {},
            "channel_availability": {},
        },
    )
    (root / "reconciled-inventory.json").write_text(
        json.dumps(
            {
                "sessions": [{"session_id": identity, "state": "completed"}],
            }
        )
    )
    job = root / "corpus" / "jobs" / f"{identity}.json"
    job.parent.mkdir(parents=True)
    job.write_text(
        json.dumps({"state": "COMPLETE", "canonical_identity": revision, "raw_identity": bundle})
    )
    assert main(["--root", str(tmp_path), "detailed", "validate"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["native_corpus_complete"] is True
    assert output["complete"] is False  # Additive enrichment is a separate requirement.
    catalog = json.loads((root / "reconciled-inventory.json").read_text())
    monkeypatch.setattr(study_runner, "ready_inventory", lambda _root: (catalog, {}))
    assert main(["--root", str(tmp_path), "detailed", "catalog", "rebuild"]) == 0
    capsys.readouterr()
    published = root / "corpus-catalog.json"
    before = (published.read_bytes(), published.stat().st_mtime_ns)
    assert main(["--root", str(tmp_path), "detailed", "catalog", "rebuild"]) == 0
    capsys.readouterr()
    assert (published.read_bytes(), published.stat().st_mtime_ns) == before
    job.write_text(
        json.dumps({"state": "FAILED", "canonical_identity": revision, "raw_identity": bundle})
    )
    assert main(["--root", str(tmp_path), "detailed", "validate"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["verified"] is False
    assert output["native_corpus_complete"] is False
    assert len(output["failures"]) == 1
    assert main(["--root", str(tmp_path), "detailed", "catalog", "rebuild"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "ValueError"
    assert (published.read_bytes(), published.stat().st_mtime_ns) == before
