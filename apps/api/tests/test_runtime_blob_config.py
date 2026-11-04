"""Server-only runtime settings fail closed without touching providers."""

from pathlib import Path
from tempfile import gettempdir
from typing import Any

import pytest
from app.core.config import Settings
from pydantic import SecretStr, ValidationError


def isolated_settings(**values: Any) -> Settings:
    return Settings(**{"_env_file": None, **values})


def cloud_values() -> dict[str, Any]:
    return {
        "storage_backend": "vercel_blob",
        "blob_origin": "https://fixture.private.blob.vercel-storage.com",
        "blob_release_prefix": "releases/frozen-2026-10",
        "blob_release_sha256": "a" * 64,
        "blob_token": "offline-test-secret",
    }


def test_local_storage_remains_default() -> None:
    settings = isolated_settings()
    assert settings.storage_backend == "local"
    assert settings.project_root == Path(".")


def test_cloud_settings_are_server_only_bounded_and_temp_dedicated() -> None:
    settings = isolated_settings(**cloud_values())
    assert isinstance(settings.blob_token, SecretStr)
    assert "offline-test-secret" not in repr(settings)
    assert settings.blob_cache_root.is_absolute()
    assert settings.blob_cache_root.parent == Path(gettempdir())
    assert 0 < settings.blob_cache_max_bytes <= 512 * 1024**2
    assert 0 < settings.blob_cache_max_files <= 100_002


@pytest.mark.parametrize(
    "missing", ["blob_origin", "blob_release_prefix", "blob_release_sha256", "blob_token"]
)
def test_incomplete_cloud_settings_rejected_secret_safely(missing: str) -> None:
    values = cloud_values()
    values.pop(missing)
    with pytest.raises(ValidationError) as caught:
        isolated_settings(**values)
    assert "offline-test-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("storage_backend", "unknown"),
        ("blob_origin", "https://fixture.public.blob.vercel-storage.com"),
        ("blob_origin", "https://fixture.private.blob.vercel-storage.com/path"),
        ("blob_origin", "https://offline-test-secret@fixture.private.blob.vercel-storage.com"),
        ("blob_release_prefix", "../release"),
        ("blob_release_prefix", "releases/latest"),
        ("blob_release_sha256", "not-a-pin"),
        ("blob_token", "offline-test-secret\n"),
        ("blob_cache_max_bytes", 0),
        ("blob_cache_max_bytes", 512 * 1024**2 + 1),
        ("blob_cache_max_files", 0),
        ("blob_cache_max_files", 100_003),
        ("blob_cache_lock_timeout", 0),
        ("blob_cache_lock_timeout", float("inf")),
        ("blob_cache_root", Path(gettempdir())),
        ("blob_cache_root", Path("relative-cache")),
    ],
)
def test_invalid_cloud_settings_rejected_secret_safely(field: str, value: object) -> None:
    values = {**cloud_values(), field: value}
    with pytest.raises(ValidationError) as caught:
        isolated_settings(**values)
    assert "offline-test-secret" not in str(caught.value)


def test_vercel_process_cannot_silently_default_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL", "1")
    assert isolated_settings().storage_backend == "bundled"
    with pytest.raises(ValidationError, match="storage"):
        isolated_settings(storage_backend="local")


def test_bundled_settings_need_no_credentials_or_existing_bundle(tmp_path: Path) -> None:
    settings = isolated_settings(storage_backend="bundled", bundle_root=tmp_path / "absent")
    assert settings.bundle_root == tmp_path / "absent"
    assert not settings.bundle_root.exists()
    assert settings.bundle_release_sha256 is None
    assert settings.blob_token is None
    assert settings.bundle_cache_root == Path(gettempdir()) / "downforce-runtime-bundle"
    assert settings.bundle_cache_max_bytes == 128 * 1024**2
    assert 0 < settings.bundle_cache_max_files <= 100_002
    assert not (tmp_path / "absent").exists()


def test_bundled_default_root_is_code_repository_not_working_directory() -> None:
    root = Path(__file__).resolve().parents[3]
    assert isolated_settings(storage_backend="bundled").bundle_root == root / "product-data"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bundle_release_sha256", ""),
        ("bundle_release_sha256", "A" * 64),
        ("bundle_release_sha256", "g" * 64),
        ("bundle_release_sha256", "a" * 63),
        ("bundle_cache_max_bytes", 0),
        ("bundle_cache_max_bytes", 512 * 1024**2 + 1),
        ("bundle_cache_max_files", 0),
        ("bundle_cache_max_files", 100_003),
        ("bundle_cache_lock_timeout", 0),
        ("bundle_cache_lock_timeout", float("inf")),
        ("bundle_cache_root", Path(gettempdir())),
        ("bundle_cache_root", Path("relative-cache")),
    ],
)
def test_bundled_settings_reject_invalid_pin_and_cache(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        isolated_settings(storage_backend="bundled", **{field: value})


def test_bundle_cache_rejects_checkout_and_unsafe_ancestry(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="dedicated"):
        isolated_settings(
            storage_backend="bundled", project_root=tmp_path, bundle_cache_root=tmp_path / "cache"
        )
    with pytest.raises(ValidationError, match="unsafe"):
        isolated_settings(storage_backend="bundled", bundle_cache_root=tmp_path / "x/../cache")


def test_bundled_accepts_explicit_pin_and_dedicated_cache(tmp_path: Path) -> None:
    settings = isolated_settings(
        storage_backend="bundled",
        bundle_release_sha256="a" * 64,
        bundle_cache_root=tmp_path / "runtime",
    )
    assert settings.bundle_release_sha256 == "a" * 64
    assert settings.bundle_cache_root == tmp_path / "runtime"


@pytest.mark.parametrize("relation", ["equal", "cache-inside-bundle", "bundle-inside-cache"])
def test_bundle_and_mutable_cache_roots_must_be_disjoint(tmp_path: Path, relation: str) -> None:
    bundle = tmp_path / "bundle"
    cache = bundle
    if relation == "cache-inside-bundle":
        cache = bundle / "runtime"
    elif relation == "bundle-inside-cache":
        bundle = cache / "product-data"
    with pytest.raises(ValidationError, match="disjoint"):
        isolated_settings(storage_backend="bundled", bundle_root=bundle, bundle_cache_root=cache)


def test_cloud_cache_accepts_long_form_windows_temporary_path(tmp_path: Path) -> None:
    settings = isolated_settings(blob_cache_root=tmp_path / "runtime", **cloud_values())
    assert settings.blob_cache_root == tmp_path / "runtime"


def test_cloud_cache_rejects_resolved_broad_temp_root() -> None:
    with pytest.raises(ValidationError, match="dedicated"):
        isolated_settings(blob_cache_root=Path(gettempdir()).resolve(), **cloud_values())


def test_cloud_cache_rejects_checkout_through_temp_alias() -> None:
    with pytest.raises(ValidationError, match="dedicated"):
        isolated_settings(
            project_root=Path(gettempdir()),
            blob_cache_root=Path(gettempdir()).resolve() / "runtime",
            **cloud_values(),
        )
