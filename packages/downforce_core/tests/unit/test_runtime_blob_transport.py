"""Private Blob GET behavior tested with an entirely offline request injection."""

from collections.abc import Iterator
from io import BytesIO
from typing import Any

import pytest
import requests
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.transport import BlobTransportError, PrivateBlobReader

ORIGIN = "https://example.private.blob.vercel-storage.com"
PATH = ".downforce/normalized/race/active.json"


class Response:
    def __init__(self, status: int = 200, chunks: tuple[bytes, ...] = (b"abc",)) -> None:
        self.status_code = status
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


def test_private_transport_uses_only_fixed_origin_prefix_and_server_bearer() -> None:
    response = Response()
    calls: list[tuple[str, dict[str, Any]]] = []

    def get(url: str, **kwargs: Any) -> Response:
        calls.append((url, kwargs))
        return response

    reader = PrivateBlobReader(ORIGIN, "releases/pinned", "server-secret", request=get)
    assert list(reader.iter_bytes(PATH, max_bytes=3)) == [b"abc"]
    assert calls == [
        (
            ORIGIN + "/releases/pinned/" + PATH,
            {
                "headers": {"Authorization": "Bearer server-secret", "Accept-Encoding": "identity"},
                "timeout": (5.0, 30.0),
                "stream": True,
                "allow_redirects": False,
            },
        )
    ]
    assert response.closed
    assert "server-secret" not in repr(reader)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.private.blob.vercel-storage.com",
        "https://example.public.blob.vercel-storage.com",
        "https://example.private.blob.vercel-storage.com.evil.test",
        ORIGIN + "/",
        ORIGIN + ":443",
        ORIGIN + "?token=x",
        "https://user:secret@" + ORIGIN[8:],
    ],
)
def test_private_transport_rejects_nonexact_origins(origin: str) -> None:
    with pytest.raises(StorageIntegrityError):
        PrivateBlobReader(origin, "releases/pinned", "secret")


@pytest.mark.parametrize("prefix", ["../x", "releases//x", "https://evil", "/x", "x?secret"])
def test_private_transport_rejects_unsafe_prefix(prefix: str) -> None:
    with pytest.raises(StorageIntegrityError):
        PrivateBlobReader(ORIGIN, prefix, "secret")


@pytest.mark.parametrize("status", [301, 302, 404, 429, 500])
def test_private_transport_never_retries_or_follows_redirects(status: int) -> None:
    response = Response(status)
    calls: list[str] = []

    def get(url: str, **kwargs: Any) -> Response:
        calls.append(url)
        return response

    reader = PrivateBlobReader(ORIGIN, "release", "secret", request=get)
    with pytest.raises(BlobTransportError):
        list(reader.iter_bytes(PATH, max_bytes=3))
    assert len(calls) == 1
    assert response.closed


@pytest.mark.parametrize("status", [401, 403])
def test_private_transport_latches_auth_denial(status: int) -> None:
    calls: list[str] = []

    def get(url: str, **kwargs: Any) -> Response:
        calls.append(url)
        return Response(status)

    reader = PrivateBlobReader(ORIGIN, "release", "secret", request=get)
    for _ in range(2):
        with pytest.raises(BlobTransportError, match="authorization"):
            list(reader.iter_bytes(PATH, max_bytes=3))
    assert len(calls) == 1


def test_private_transport_caps_stream_and_closes_on_early_exit() -> None:
    response = Response(chunks=(b"ab", b"cd"))
    reader = PrivateBlobReader(ORIGIN, "release", "secret", request=lambda *a, **k: response)
    with pytest.raises(BlobTransportError, match="limit"):
        list(reader.iter_bytes(PATH, max_bytes=3))
    assert response.closed


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_private_transport_preserves_exception_type_without_sensitive_context(
    error: type[requests.RequestException],
) -> None:
    def get(url: str, **kwargs: Any) -> Response:
        raise error("server-secret at " + url)

    reader = PrivateBlobReader(ORIGIN, "release", "server-secret", request=get)
    with pytest.raises(BlobTransportError) as captured:
        list(reader.iter_bytes(PATH, max_bytes=3))
    assert captured.value.exception_type is error
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__
    assert "server-secret" not in str(captured.value)


def test_private_transport_refuses_crlf_token_and_nonfinite_timeouts() -> None:
    for token, timeout in (("secret\r\nX:x", 5.0), ("secret", float("inf"))):
        with pytest.raises(StorageIntegrityError):
            PrivateBlobReader(ORIGIN, "release", token, read_timeout=timeout)


def test_default_transport_preserves_proxy_routing_and_prevents_netrc_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[requests.Session] = []
    sent: list[tuple[requests.PreparedRequest, dict[str, Any]]] = []
    original = requests.Session

    class OfflineAdapter(requests.adapters.BaseAdapter):
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            sent.append((request, {"proxies": proxies}))
            response = requests.Response()
            response.status_code = 200
            response.raw = BytesIO(b"abc")
            response.request = request
            return response

        def close(self) -> None:
            pass

    def session() -> requests.Session:
        instance = original()
        instance.mount("https://", OfflineAdapter())
        sessions.append(instance)
        return instance

    monkeypatch.setattr(requests, "Session", session)
    monkeypatch.setenv("HTTPS_PROXY", "http://configured-proxy.invalid:8181")
    monkeypatch.setenv("NO_PROXY", "")

    def netrc_auth(url: str, **kwargs: Any) -> tuple[str, str]:
        raise AssertionError("explicit bearer must not consult netrc")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc_auth)
    reader = PrivateBlobReader(ORIGIN, "release", "server-secret")
    assert list(reader.iter_bytes(PATH, max_bytes=3)) == [b"abc"]
    assert len(sessions) == 1
    assert sessions[0].trust_env is True
    assert sent[0][0].headers["Authorization"] == "Bearer server-secret"
    assert sent[0][1]["proxies"]["https"] == "http://configured-proxy.invalid:8181"
    sessions[0].close()
