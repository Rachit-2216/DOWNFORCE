from pathlib import Path

import pytest
from downforce_core.detailed.paths import DetailedPaths


def test_default_root_stays_separate_from_archive_and_ml(tmp_path: Path) -> None:
    paths = DetailedPaths(tmp_path, environ={})
    assert paths.root == tmp_path / ".downforce" / "detailed"
    assert paths.canonical == paths.root / "canonical"
    assert paths.raw == paths.root / "raw"


def test_configured_root_is_used_without_moving_old_data(tmp_path: Path) -> None:
    alternate = tmp_path / "external" / "detailed"
    paths = DetailedPaths(tmp_path, environ={"DOWNFORCE_DETAILED_DATA_ROOT": str(alternate)})
    assert paths.root == alternate
    assert not alternate.exists()


@pytest.mark.parametrize("bad", ["relative/path", "../escape", "apps", ".downforce/archive", ""])
def test_bad_configuration_is_rejected(tmp_path: Path, bad: str) -> None:
    value = str(tmp_path / bad) if bad in ("apps", ".downforce/archive") else bad
    with pytest.raises(ValueError):
        DetailedPaths(tmp_path, environ={"DOWNFORCE_DETAILED_DATA_ROOT": value})


def test_source_folder_and_project_root_are_not_valid_data_targets(tmp_path: Path) -> None:
    for target in [tmp_path, tmp_path / "packages" / "data", tmp_path / ".git" / "detailed"]:
        with pytest.raises(ValueError):
            DetailedPaths(tmp_path, override=target, environ={})


@pytest.mark.parametrize(
    "source", ["APPS", "node_modules", "Assets", "data/raw", "data/normalized"]
)
def test_other_product_and_case_alias_paths_are_rejected(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError):
        DetailedPaths(tmp_path, override=tmp_path / source, environ={})
