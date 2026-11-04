"""Single-worker network safety layered around, never replacing, provider limiters."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any, cast

from fastf1.exceptions import RateLimitExceededError  # type: ignore[import-untyped]

from downforce_core.detailed.rate_budget import RequestBudget


def _unlinked_path(path: Path) -> Path:
    """Reject redirected runtime state before creating either a lock or history."""
    absolute = path.absolute()
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("linked detailed runtime paths are not permitted")
    return absolute


@contextmanager
def network_worker(path: Path) -> Iterator[None]:
    """An OS lock survives neither process exit nor crash; no stale-lock removal."""
    path = _unlinked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        module = cast(Any, __import__("msvcrt" if os.name == "nt" else "fcntl"))
        try:
            if os.name == "nt":
                module.locking(handle.fileno(), module.LK_NBLCK, 1)
            else:
                module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
        except OSError as exc:
            raise ValueError("another detailed network worker owns this data root") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                module.locking(handle.fileno(), module.LK_UNLCK, 1)
            else:
                module.flock(handle.fileno(), module.LOCK_UN)


@contextmanager
def guarded_transport(
    session_class: Any,
    budget: RequestBudget,
    *,
    response_observer: Callable[[Any], None] | None = None,
) -> Iterator[Callable[[], None]]:
    """Wrap native transport *below* the provider's HTTP cache.

    Cache hits do not reach this method. The original method still applies all
    installed native rate limits. Permission failures stop the whole operation,
    even if a third-party loader catches the first exception and returns partial.
    Use only inside network_worker; global transport changes are single-worker.
    """
    original = session_class.send
    denied: int | None = None
    observation_failure: BaseException | None = None

    def check_permission() -> None:
        if denied is not None:
            raise PermissionError(f"provider access denied: HTTP {denied}") from observation_failure
        if observation_failure is not None:
            raise observation_failure

    def send(session: Any, request: Any, **kwargs: Any) -> Any:
        nonlocal denied, observation_failure
        check_permission()
        budget.wait()
        requested_stream = kwargs.get("stream", getattr(session, "stream", False))
        original_hooks = getattr(request, "hooks", None)
        hooked = isinstance(original_hooks, dict)
        observed_response: Any = None

        def restore_hooks() -> None:
            if hooked:
                request.hooks = original_hooks

        def stop_response(response: Any) -> None:
            try:
                if hasattr(response, "close"):
                    response.close()
            finally:
                check_permission()

        def receive(response: Any, **_hook_kwargs: Any) -> Any:
            nonlocal denied, observation_failure, observed_response
            # Restore before user observers/hooks, redirect copies, or HTTP
            # cache serialization can retain this request-local callback.
            restore_hooks()
            observed_response = response
            if response.status_code in (401, 403):
                denied = response.status_code
            try:
                if response_observer is not None:
                    response_observer(response)
                if not requested_stream:
                    _ = response.content
            except BaseException as exc:
                observation_failure = exc
            if denied is not None or observation_failure is not None:
                stop_response(response)
            return response

        try:
            if isinstance(original_hooks, dict):
                custom_hooks = original_hooks.get("response", [])
                if callable(custom_hooks):
                    custom_hooks = [custom_hooks]
                request.hooks = {**original_hooks, "response": [receive, *(custom_hooks or [])]}
            try:
                # Native Requests dispatches response hooks before redirect
                # preparation and body reads, even with allow_redirects=False.
                response = original(session, request, **kwargs)
            except RateLimitExceededError:
                # A native rejection may reflect another previously running process.
                # Persist a full cooldown; never reset the installed limiters.
                budget.defer_until(time() + budget.interval)
                raise
            except BaseException as exc:
                if observed_response is not None and observation_failure is None and denied is None:
                    # Covers errors in existing hooks/native redirect-body reads
                    # after the early safety hook has already seen the response.
                    observation_failure = exc
                    stop_response(observed_response)
                raise
            if observed_response is None:
                receive(response)  # Small test transports may not dispatch hooks.
            check_permission()
            return response
        finally:
            restore_hooks()

    session_class.send = send
    try:
        yield check_permission
    finally:
        session_class.send = original
        check_permission()


@contextmanager
def fastf1_guard(
    root: Path, *, response_observer: Callable[[Any], None] | None = None
) -> Iterator[Callable[[], None]]:
    """Retain request history across worker restarts and cache-directory switches."""
    from fastf1.req import _SessionWithRateLimiting  # type: ignore[import-untyped]

    lock_path = _unlinked_path(root / "runtime" / "fastf1-worker.lock")
    budget_path = _unlinked_path(root / "runtime" / "fastf1-budget.json")
    with network_worker(lock_path):
        budget = RequestBudget(budget_path)
        with guarded_transport(
            _SessionWithRateLimiting, budget, response_observer=response_observer
        ) as check_permission:
            yield check_permission
