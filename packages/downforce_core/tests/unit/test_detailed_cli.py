import json
from pathlib import Path

import pytest
from downforce_core.cli import main


def test_detailed_storage_status_reads_only_and_uses_configured_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alternate = tmp_path / "alternate"
    assert (
        main(["--root", str(tmp_path), "detailed-storage", "status", "--data-root", str(alternate)])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["configured_root"] == str(alternate)
    assert not alternate.exists()
    assert not (tmp_path / ".downforce").exists()


def test_detailed_projection_does_not_fall_back_to_unverified_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "alternate"
    root.mkdir()
    assert (
        main(["--root", str(tmp_path), "detailed-storage", "project", "--data-root", str(root)])
        == 1
    )
    assert "reconciled" in capsys.readouterr().err.lower()
