"""Offline release-manifest trust-boundary tests."""

import hashlib
import json

import pytest
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.manifest import ManifestLimits, RuntimeManifest

PATH = ".downforce/normalized/2024-01-race/active.json"
DEFAULT_LIMITS = ManifestLimits()


def release(files: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "files": files
            or {PATH: {"size_bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}},
        }
    ).encode()


def parse(body: bytes, *, limits: ManifestLimits = DEFAULT_LIMITS) -> RuntimeManifest:
    return RuntimeManifest.from_bytes(body, hashlib.sha256(body).hexdigest(), limits=limits)


def test_manifest_cannot_bypass_authentication_with_public_constructor() -> None:
    with pytest.raises(TypeError):
        RuntimeManifest("a" * 64, {})  # type: ignore[call-arg]


def test_authenticated_manifest_has_immutable_sorted_index() -> None:
    manifest = parse(release())
    assert manifest.entry(PATH).size_bytes == 3
    assert manifest.paths(".downforce/normalized", suffix=".json") == (PATH,)
    with pytest.raises(TypeError):
        manifest.files[PATH] = manifest.entry(PATH)  # type: ignore[index]
    with pytest.raises(KeyError):
        manifest.entry(".downforce/normalized/absent.json")


def test_manifest_authenticates_original_bytes_before_parsing() -> None:
    with pytest.raises(StorageIntegrityError, match="checksum"):
        RuntimeManifest.from_bytes(b"not json", "0" * 64)


@pytest.mark.parametrize(
    "path",
    [
        "../.downforce/normalized/a.json",
        ".downforce/normalized/../a.json",
        ".downforce\\normalized\\a.json",
        "/.downforce/normalized/a.json",
        ".downforce/normalized/a.json:secret",
        ".downforce/normalized/CON.json",
        ".downforce/normalized/com1/a.json",
        ".downforce/normalized/a./b.json",
        ".downforce/raw/a.json",
        ".downforce/detailed/study/a.json",
        ".downforce/detailed/canonical/a.txt",
        ".downforce/archive/catalog/other.json",
        "artifacts/ml/bundles/other.json",
        "artifacts/ml/docs.json",
        ".downforce/normalized/a%2fb.json",
        ".downforce/normalized/a//b.json",
        ".downforce/normalized/reports/a.json",
        ".downforce/normalized/test.json",
    ],
)
def test_manifest_rejects_unsafe_or_nonproduct_paths(path: str) -> None:
    with pytest.raises(StorageIntegrityError):
        parse(release({path: {"size_bytes": 3, "sha256": "a" * 64}}))


@pytest.mark.parametrize(
    "body",
    [
        b'{"schema_version":1,"schema_version":1,"files":{}}',
        b'{"schema_version":1,"files":{}}',
        b'{"schema_version":true,"files":{}}',
        b'{"schema_version":2,"files":{}}',
        b'{"schema_version":1,"files":{},"url":"https://evil"}',
        b'{"schema_version":1,"files":{},"token":NaN}',
        b'{"schema_version":1,"files":{},"token":1e10000}',
        b"[]",
        b"null",
        b"\xff",
    ],
)
def test_manifest_rejects_malformed_shape_versions_and_numbers(body: bytes) -> None:
    with pytest.raises(StorageIntegrityError):
        parse(body)


@pytest.mark.parametrize(
    "entry",
    [
        {"size_bytes": True, "sha256": "a" * 64},
        {"size_bytes": -1, "sha256": "a" * 64},
        {"size_bytes": 1.5, "sha256": "a" * 64},
        {"size_bytes": 3, "sha256": "A" * 64},
        {"size_bytes": 3, "sha256": "x" * 64},
        {"size_bytes": 3, "sha256": "a" * 64, "url": "https://evil"},
    ],
)
def test_manifest_rejects_invalid_entry(entry: dict[str, object]) -> None:
    with pytest.raises(StorageIntegrityError):
        parse(release({PATH: entry}))


def test_manifest_rejects_case_and_file_directory_collisions() -> None:
    entry = {"size_bytes": 0, "sha256": "a" * 64}
    for extra in (PATH.upper(), PATH + "/extra.json"):
        with pytest.raises(StorageIntegrityError):
            parse(release({PATH: entry, extra: entry}))


def test_manifest_caps_body_count_object_and_total_bytes() -> None:
    body = release()
    for limits in (
        ManifestLimits(max_manifest_bytes=1),
        ManifestLimits(max_files=0),
        ManifestLimits(max_object_bytes=2),
        ManifestLimits(max_total_bytes=2),
    ):
        with pytest.raises(StorageIntegrityError):
            parse(body, limits=limits)


def test_default_manifest_limits_remain_fixed_and_bounded() -> None:
    assert ManifestLimits() == ManifestLimits(
        max_manifest_bytes=16 * 1024 * 1024,
        max_files=100_000,
        max_object_bytes=512 * 1024 * 1024,
        max_total_bytes=1024**4,
    )


def test_manifest_rejects_sixteen_mib_plus_one_before_authentication_or_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("oversized release must not reach JSON parsing")

    monkeypatch.setattr(json, "loads", forbidden)
    body = b"x" * (16 * 1024 * 1024 + 1)
    with pytest.raises(StorageIntegrityError, match="body exceeds manifest limit"):
        RuntimeManifest.from_bytes(body, "0" * 64)


def test_manifest_accepts_only_runtime_categories() -> None:
    entry = {"size_bytes": 0, "sha256": "a" * 64}
    paths = (
        ".downforce/detailed/canonical/2024-01-race/active.json",
        ".downforce/archive/catalog/historical-catalog.json",
        ".downforce/archive/sessions/2024-01-race/revisions/abc/laps.parquet",
        ".downforce/aliases/2024-01-race.json",
        "artifacts/ml/registry.json",
        "artifacts/ml/bundles/ml-bundle-sha256-" + "a" * 64 + ".json",
        ".downforce/analytics/driver-race.parquet",
        ".downforce/analytics/manifest.json",
        ".downforce/analytics/circuit-identities.json",
    )
    assert parse(release(dict.fromkeys(paths, entry))).paths() == tuple(sorted(paths))


@pytest.mark.parametrize(
    "token",
    [
        "provider",
        "providers",
        "workflow",
        "workflows",
        "caches",
        "staging",
        "review",
        "reviews",
        "evidence",
        "evidences",
    ],
)
@pytest.mark.parametrize("prefix", [".downforce/normalized/", ".downforce/archive/sessions/"])
def test_manifest_rejects_forbidden_artifact_tokens_under_product_roots(
    token: str,
    prefix: str,
) -> None:
    with pytest.raises(StorageIntegrityError, match="excluded"):
        parse(
            release(
                {
                    prefix + "race/" + token + "/manifest.json": {
                        "size_bytes": 0,
                        "sha256": "a" * 64,
                    }
                }
            )
        )
