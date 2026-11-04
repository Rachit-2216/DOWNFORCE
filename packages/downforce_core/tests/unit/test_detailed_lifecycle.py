from pathlib import Path

import pytest
from downforce_core.detailed.lifecycle import Lifecycle
from downforce_core.detailed.paths import DetailedPaths


def test_crash_resume_keeps_last_successful_checkpoint(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, environ={})
    job = Lifecycle(paths, "2024-01-race")
    job.advance("DOWNLOADING")
    job.advance("SOURCE_CACHED")
    job.fail("temporary-provider-error")
    restarted = Lifecycle(paths, "2024-01-race")
    assert restarted.read()["state"] == "FAILED"
    assert restarted.resume() == "SOURCE_CACHED"
    assert restarted.read()["state"] == "SOURCE_CACHED"


def test_complete_requires_raw_canonical_and_validation_checkpoints(tmp_path: Path) -> None:
    job = Lifecycle(DetailedPaths(tmp_path, environ={}), "2024-01-race")
    with pytest.raises(ValueError):
        job.advance("COMPLETE")
    for state in ["DOWNLOADING", "SOURCE_CACHED"]:
        job.advance(state)
    with pytest.raises(ValueError, match="identity"):
        job.advance("RAW_EVIDENCE_PUBLISHED")
    job.advance("RAW_EVIDENCE_PUBLISHED", identity="a" * 64)
    job.advance("NORMALIZING")
    job.advance("CANONICAL_PUBLISHED", identity="b" * 64)
    with pytest.raises(ValueError, match="verified"):
        job.advance("VALIDATED")
    job.advance("VALIDATED", verified=True)
    job.advance("COMPLETE")
    state = Lifecycle(job.paths, "2024-01-race").read()
    assert state["state"] == "COMPLETE"
    assert state["raw_identity"] == "a" * 64
    assert state["canonical_identity"] == "b" * 64


def test_same_checkpoint_is_idempotent_but_cannot_change_identity(tmp_path: Path) -> None:
    job = Lifecycle(DetailedPaths(tmp_path, environ={}), "2024-01-race")
    job.advance("DOWNLOADING")
    job.advance("SOURCE_CACHED")
    job.advance("RAW_EVIDENCE_PUBLISHED", identity="a" * 64)
    original = job.read()
    job.advance("RAW_EVIDENCE_PUBLISHED", identity="a" * 64)
    assert job.read() == original
    with pytest.raises(ValueError):
        job.advance("RAW_EVIDENCE_PUBLISHED", identity="b" * 64)


def test_failed_publication_does_not_advance_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Lifecycle(DetailedPaths(tmp_path, environ={}), "2024-01-race")
    job.advance("DOWNLOADING")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted atomic publication")

    monkeypatch.setattr("downforce_core.detailed.lifecycle.write_text_atomic", broken)
    with pytest.raises(OSError):
        job.advance("SOURCE_CACHED")
    assert job.read()["state"] == "DOWNLOADING"
