"""Server-only, fixed-origin private Blob reads; no redirects or retries."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterator
from typing import Any, Protocol, cast

import requests

from downforce_core.exceptions import StorageIntegrityError
from downforce_core.runtime_blob.manifest import validate_relative_path


class BlobTransportError(StorageIntegrityError):
    """Safe transport diagnostic retaining type, never provider exception text."""

    def __init__(self, message: str, *, exception_type: type[Exception] | None = None) -> None:
        super().__init__(message)
        self.exception_type = exception_type


class ObjectReader(Protocol):
    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]: ...


class _Response(Protocol):
    status_code: int

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class _Request(Protocol):
    def __call__(self, url: str, **kwargs: Any) -> _Response: ...


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = "Bearer " + self._token
        return request


class PrivateBlobReader:
    """Explicit server credential, configured origin, immutable release namespace."""

    def __init__(
        self,
        origin: str,
        release_prefix: str,
        token: str,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        request: _Request | None = None,
    ) -> None:
        if not re.fullmatch(
            r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.private\.blob\.vercel-storage\.com", origin
        ):
            raise StorageIntegrityError("private Blob origin is invalid")
        validate_relative_path(release_prefix)
        if (
            not isinstance(token, str)
            or not token
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise StorageIntegrityError("private Blob server credential is invalid")
        if any(not math.isfinite(value) or value <= 0 for value in (connect_timeout, read_timeout)):
            raise StorageIntegrityError("private Blob timeouts are invalid")
        self._base = origin + "/" + release_prefix + "/"
        self._token = token
        self._timeout = (connect_timeout, read_timeout)
        if request is None:
            session = requests.Session()
            # Explicit auth prevents netrc overriding the bearer. Preserve configured
            # proxy and CA routing: transport must not bypass environment boundaries.
            session.auth = _BearerAuth(token)
            self._request = cast(_Request, session.get)
        else:
            self._request = request
        self._authorization_stopped = False
        self._lock = threading.Lock()

    def iter_bytes(self, relative_path: str, *, max_bytes: int) -> Iterator[bytes]:
        validate_relative_path(relative_path)
        if type(max_bytes) is not int or max_bytes < 0:
            raise StorageIntegrityError("private Blob body limit is invalid")
        # A denial stops subsequent calls on this transport, including concurrent callers.
        with self._lock:
            if self._authorization_stopped:
                raise BlobTransportError("private Blob authorization stopped")
            response: _Response | None = None
            failure: BlobTransportError | None = None
            try:
                response = self._request(
                    self._base + relative_path,
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Accept-Encoding": "identity",
                    },
                    timeout=self._timeout,
                    stream=True,
                    allow_redirects=False,
                )
                if response.status_code in {401, 403}:
                    self._authorization_stopped = True
                    raise BlobTransportError("private Blob authorization stopped")
                if response.status_code != 200:
                    raise BlobTransportError("private Blob response status is rejected")
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise BlobTransportError("private Blob stream is malformed")
                    total += len(chunk)
                    if total > max_bytes:
                        raise BlobTransportError("private Blob stream exceeds body limit")
                    if chunk:
                        yield chunk
            except BlobTransportError as exc:
                failure = exc
            except Exception as exc:
                failure = BlobTransportError(
                    "private Blob transport failed", exception_type=type(exc)
                )
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception as exc:
                        if failure is None:
                            failure = BlobTransportError(
                                "private Blob response close failed", exception_type=type(exc)
                            )
            # Raise outside the exception handler: provider text is not even retained as context.
            if failure is not None:
                raise failure from None
