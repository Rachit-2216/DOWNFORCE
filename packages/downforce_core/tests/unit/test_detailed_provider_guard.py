from pathlib import Path
from typing import Any

import pytest
from downforce_core.detailed.rate_budget import RequestBudget


class Session:
    calls = 0

    def send(self, request: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return request


class Response:
    status_code = 200
    url = "https://example.invalid/source"
    content = b""


def test_transport_adds_budget_without_replacing_native_send(tmp_path: Path) -> None:
    from downforce_core.detailed.provider_guard import guarded_transport

    budget = RequestBudget(tmp_path / "budget.json")
    original = Session.send
    session = Session()
    with guarded_transport(Session, budget) as check_permission:
        assert callable(check_permission)
        assert session.send(Response()).status_code == 200
        check_permission()
    assert session.calls == 1
    assert Session.send is original
    assert len(budget._read()["timestamps"]) == 1


def test_transport_stops_on_permission_failure_even_if_provider_swallows_it(tmp_path: Path) -> None:
    from downforce_core.detailed.provider_guard import guarded_transport

    budget = RequestBudget(tmp_path / "budget.json")
    response = Response()
    response.status_code = 403
    checkpoint_failed = False
    with pytest.raises(PermissionError, match="403"):
        with guarded_transport(Session, budget) as check_permission:
            try:
                Session().send(response)
            except Exception:
                pass  # third-party loading can swallow exceptions and return partial data
            try:
                check_permission()
            except PermissionError:
                checkpoint_failed = True
    assert checkpoint_failed  # fail before any successful measurement is published


def test_transport_restores_original_after_load_error(tmp_path: Path) -> None:
    from downforce_core.detailed.provider_guard import guarded_transport

    original = Session.send
    with pytest.raises(ValueError):
        with guarded_transport(Session, RequestBudget(tmp_path / "budget.json")):
            raise ValueError("failed")
    assert Session.send is original


def test_single_network_worker_is_enforced(tmp_path: Path) -> None:
    from downforce_core.detailed.provider_guard import network_worker

    lock = tmp_path / "worker.lock"
    with network_worker(lock):
        with pytest.raises(ValueError, match="worker"):
            with network_worker(lock):
                raise AssertionError("concurrent worker entered")
    with network_worker(lock):
        pass


@pytest.mark.parametrize("linked_name", ["runtime", "fastf1-worker.lock", "fastf1-budget.json"])
def test_fastf1_guard_rejects_linked_runtime_paths_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_name: str
) -> None:
    from downforce_core.detailed.provider_guard import fastf1_guard

    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == linked_name)
    with pytest.raises(ValueError, match="linked"):
        with fastf1_guard(tmp_path):
            raise AssertionError("unsafe runtime entered")
    assert not (tmp_path / "runtime").exists()


def test_network_worker_rejects_linked_parent_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downforce_core.detailed.provider_guard import network_worker

    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "runtime")
    with pytest.raises(ValueError, match="linked"):
        with network_worker(tmp_path / "runtime" / "worker.lock"):
            raise AssertionError("unsafe runtime entered")
    assert not (tmp_path / "runtime").exists()


def test_native_rate_rejection_persists_cooldown_instead_of_retrying_immediately(
    tmp_path: Path,
) -> None:
    from time import time

    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.exceptions import RateLimitExceededError

    class LimitedSession:
        def send(self, request: Any, **kwargs: Any) -> Any:
            raise RateLimitExceededError("native quota reached")

    budget = RequestBudget(tmp_path / "budget.json")
    before = time()
    with pytest.raises(RateLimitExceededError):
        with guarded_transport(LimitedSession, budget):
            LimitedSession().send(Response())
    assert RequestBudget(budget.path).reserve(now=before + 1) >= 3599


def test_cached_http_hits_do_not_consume_network_budget(tmp_path: Path) -> None:
    from io import BytesIO

    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _CachedSessionWithRateLimiting, _SessionWithRateLimiting
    from urllib3.response import HTTPResponse

    class OfflineAdapter(requests.adapters.BaseAdapter):
        def send(self, request: Any, **kwargs: Any) -> requests.Response:
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers["Content-Type"] = "application/json"
            response.headers["Cache-Control"] = "max-age=3600"
            response._content = b'{"observed":1}'
            response.raw = HTTPResponse(
                body=BytesIO(response._content),
                request_url=request.url,
                status=200,
                headers=response.headers,
                preload_content=False,
            )
            return response

        def close(self) -> None:
            pass

    budget = RequestBudget(tmp_path / "budget.json")
    native_limits = _SessionWithRateLimiting._RATE_LIMITS
    with _CachedSessionWithRateLimiting(backend="memory") as session:
        session.mount("https://", OfflineAdapter())
        with guarded_transport(_SessionWithRateLimiting, budget):
            first = session.get("https://example.invalid/source")
            second = session.get("https://example.invalid/source")
    assert not first.from_cache
    assert second.from_cache
    assert len(budget._read()["timestamps"]) == 1
    assert _SessionWithRateLimiting._RATE_LIMITS is native_limits


@pytest.mark.parametrize("status", [200, 404, 401, 403])
def test_observer_sees_exact_response_before_denial_close(tmp_path: Path, status: int) -> None:
    import requests
    from downforce_core.detailed.provider_guard import guarded_transport

    response = requests.Response()
    response.status_code = status
    response._content = b"exact denied or available body\r\n"
    response._content_consumed = True
    observed = []
    response.close = lambda: observed.append("closed")

    def observer(value: Any) -> None:
        assert value is response
        observed.append(value.content)

    def operation() -> None:
        with guarded_transport(
            Session, RequestBudget(tmp_path / "budget.json"), response_observer=observer
        ):
            Session().send(response)

    if status in (401, 403):
        with pytest.raises(PermissionError):
            operation()
        assert observed == [response.content, "closed"]
    else:
        operation()
        assert observed == [response.content]


@pytest.mark.parametrize("status", [200, 403])
def test_observer_failure_is_sticky_and_denial_remains_fail_closed(tmp_path: Path, status: int):
    from downforce_core.detailed.provider_guard import guarded_transport

    response = Response()
    response.status_code = status
    original = Session.send
    session = Session()

    def observer(_response: Any) -> None:
        raise OSError("disk full")

    expected = PermissionError if status == 403 else OSError
    with pytest.raises(expected):
        with guarded_transport(
            Session, RequestBudget(tmp_path / "budget.json"), response_observer=observer
        ):
            with pytest.raises(expected):
                session.send(response)
            with pytest.raises(expected):
                session.send(Response())
    assert session.calls == 1
    assert Session.send is original


@pytest.mark.parametrize("status", [200, 302, 401, 403])
@pytest.mark.parametrize("with_observer", [False, True])
def test_truncated_native_body_is_sticky_even_when_loader_swallows_failure(
    tmp_path, status, with_observer
):
    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _SessionWithRateLimiting
    from urllib3.exceptions import ProtocolError

    calls = []
    observed = []
    closed = []

    class BrokenRaw:
        def stream(self, _amount, decode_content=True):
            yield b"partial body is not a complete response"
            raise ProtocolError("truncated response body")

        def read(self, *args, **kwargs):
            raise ProtocolError("truncated response body")

        def close(self):
            closed.append("close")

        def release_conn(self):
            closed.append("release")

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request.url)
            response = requests.Response()
            response.status_code = status if len(calls) == 1 else 200
            response.url = request.url
            response.request = request
            if response.status_code == 302:
                response.headers["Location"] = "https://other.invalid/must-not-follow"
            if len(calls) == 1:
                response.raw = BrokenRaw()
            else:
                response._content = b"second request must never be sent"
                response._content_consumed = True
            return response

        def close(self):
            pass

    expected = PermissionError if status in (401, 403) else requests.exceptions.ChunkedEncodingError
    original = _SessionWithRateLimiting.send
    with _SessionWithRateLimiting() as session:
        session.mount("https://", Adapter())
        with pytest.raises(expected):
            with guarded_transport(
                _SessionWithRateLimiting,
                RequestBudget(tmp_path / "budget.json"),
                response_observer=(lambda r: observed.append(r.status_code))
                if with_observer
                else None,
            ):
                try:
                    session.get("https://example.invalid/first", allow_redirects=False)
                except Exception:
                    pass  # Native loader can swallow body-decoding failures.
                with pytest.raises(expected):
                    session.get("https://example.invalid/second", allow_redirects=False)
    assert calls == ["https://example.invalid/first"]
    assert observed == ([status] if with_observer else [])
    assert closed == ["close", "release"]
    assert _SessionWithRateLimiting.send is original
    assert len(RequestBudget(tmp_path / "budget.json")._read()["timestamps"]) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_guard_preserves_native_streaming_semantics(tmp_path, stream):
    from io import BytesIO

    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _SessionWithRateLimiting
    from urllib3.response import HTTPResponse

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.raw = HTTPResponse(body=BytesIO(b"exact body"), preload_content=False)
            return response

        def close(self):
            pass

    with _SessionWithRateLimiting() as session:
        session.mount("https://", Adapter())
        with guarded_transport(_SessionWithRateLimiting, RequestBudget(tmp_path / "budget.json")):
            response = session.get("https://example.invalid/source", stream=stream)
            assert response._content_consumed is not stream
            assert response.content == b"exact body"
            response.close()


def original_response_hook(response, **kwargs):
    """Module-level custom hook remains serializable independently of the guard."""
    return response


@pytest.mark.parametrize("stream", [False, True])
def test_request_local_safety_hook_precedes_existing_hooks_and_restores_identity(tmp_path, stream):
    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _SessionWithRateLimiting

    events = []
    requests_seen = []

    def custom(response, **kwargs):
        events.append(("custom", kwargs["stream"]))
        assert response.request.hooks is original_hooks
        return response

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            requests_seen.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content = b"exact bytes"
            response._content_consumed = True
            return response

        def close(self):
            pass

    with _SessionWithRateLimiting() as session:
        session.mount("https://", Adapter())
        prepared = session.prepare_request(
            requests.Request("GET", "https://example.invalid/source", hooks={"response": [custom]})
        )
        original_hooks = prepared.hooks

        def observe(response):
            events.append(("observe", stream))
            assert response.request.hooks is original_hooks

        with guarded_transport(
            _SessionWithRateLimiting,
            RequestBudget(tmp_path / "budget.json"),
            response_observer=observe,
        ):
            response = session.send(prepared, stream=stream, allow_redirects=False)
            response.close()
    assert requests_seen == [prepared]
    assert events == [("observe", stream), ("custom", stream)]
    assert prepared.hooks is original_hooks
    assert prepared.hooks["response"] == [custom]


def test_request_hooks_restore_after_transport_failure_before_response(tmp_path):
    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _SessionWithRateLimiting

    hook_seen = []

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            hook_seen.extend(request.hooks["response"])
            raise requests.Timeout("no response received")

        def close(self):
            pass

    with _SessionWithRateLimiting() as session:
        session.mount("https://", Adapter())
        request = session.prepare_request(
            requests.Request(
                "GET",
                "https://example.invalid/source",
                hooks={"response": [original_response_hook]},
            )
        )
        original_hooks = request.hooks
        with pytest.raises(requests.Timeout):
            with guarded_transport(
                _SessionWithRateLimiting, RequestBudget(tmp_path / "budget.json")
            ):
                session.send(request, allow_redirects=False)
        assert request.hooks is original_hooks
        assert hook_seen[-1] is original_response_hook
        assert len(hook_seen) == 2


def test_local_hook_is_not_serialized_and_cached_hits_keep_custom_hooks(tmp_path, monkeypatch):
    import pickle
    from io import BytesIO

    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _CachedSessionWithRateLimiting, _SessionWithRateLimiting
    from urllib3.response import HTTPResponse

    observed = []
    native_calls = []
    serialized_hooks = []

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            native_calls.append(request.url)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers.update({"Cache-Control": "max-age=3600"})
            response.raw = HTTPResponse(body=BytesIO(b"cached exact bytes"), preload_content=False)
            return response

        def close(self):
            pass

    with _CachedSessionWithRateLimiting(backend="memory") as session:
        session.mount("https://", Adapter())
        original_save = session.cache.save_response

        def save(response, *args, **kwargs):
            serialized_hooks.append(response.request.hooks["response"][:])
            pickle.dumps(response.request)  # A nested safety callback is not picklable.
            return original_save(response, *args, **kwargs)

        monkeypatch.setattr(session.cache, "save_response", save)
        with guarded_transport(
            _SessionWithRateLimiting,
            RequestBudget(tmp_path / "budget.json"),
            response_observer=lambda r: observed.append(r.status_code),
        ):
            first = session.get(
                "https://example.invalid/source", hooks={"response": [original_response_hook]}
            )
            second = session.get(
                "https://example.invalid/source", hooks={"response": [original_response_hook]}
            )
            assert not first.from_cache
            assert second.from_cache
            assert first.content == second.content == b"cached exact bytes"
    assert native_calls == ["https://example.invalid/source"]
    assert observed == [200]
    assert serialized_hooks == [[original_response_hook]]


@pytest.mark.parametrize("with_observer", [False, True])
def test_streaming_redirect_failure_after_safety_hook_stays_sticky(tmp_path, with_observer):
    import requests
    from downforce_core.detailed.provider_guard import guarded_transport
    from fastf1.req import _SessionWithRateLimiting
    from urllib3.exceptions import ProtocolError

    calls = []
    observed = []
    closed = []

    class BrokenRaw:
        def stream(self, _amount, decode_content=True):
            yield b"partial redirect body"
            raise ProtocolError("truncated streaming redirect")

        def read(self, *args, **kwargs):
            raise ProtocolError("truncated streaming redirect")

        def close(self):
            closed.append("close")

        def release_conn(self):
            closed.append("release")

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request.url)
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "https://other.invalid/must-not-follow"
            response.url = request.url
            response.request = request
            response.raw = BrokenRaw()
            return response

        def close(self):
            pass

    with _SessionWithRateLimiting() as session:
        session.mount("https://", Adapter())
        request = session.prepare_request(requests.Request("GET", "https://example.invalid/source"))
        original_hooks = request.hooks
        with pytest.raises(ProtocolError, match="truncated streaming redirect"):
            with guarded_transport(
                _SessionWithRateLimiting,
                RequestBudget(tmp_path / "budget.json"),
                response_observer=(lambda r: observed.append(r.status_code))
                if with_observer
                else None,
            ):
                with pytest.raises(ProtocolError):
                    session.send(request, stream=True, allow_redirects=False)
                with pytest.raises(ProtocolError):
                    session.get("https://example.invalid/second", allow_redirects=False)
        assert request.hooks is original_hooks
    assert calls == ["https://example.invalid/source"]
    assert observed == ([302] if with_observer else [])
    assert closed == ["close", "release"]
