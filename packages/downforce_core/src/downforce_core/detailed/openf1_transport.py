"""Owned transport for validated free historical OpenF1 enrichment only."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic, sleep, time
from types import TracebackType
from typing import Any, Self
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from downforce_core.detailed.provider_guard import _unlinked_path, network_worker
from downforce_core.detailed.rate_budget import RequestBudget
from downforce_core.detailed.status import guard_operation
from downforce_core.storage.layout import write_text_atomic

ENDPOINTS = (
    "drivers",
    "laps",
    "position",
    "intervals",
    "pit",
    "stints",
    "race_control",
    "weather",
    "starting_grid",
    "overtakes",
    "team_radio",
    "session_result",
)
MAX_RESPONSE_BYTES = 64 * 1024**2
MAX_HEADER_BYTES = 64 * 1024
MAX_OPERATION_WAIT = 300.0


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None  # urllib raises the original redirect response, without following it.


def historical_url(endpoint: str, session_key: int) -> str:
    if endpoint not in ENDPOINTS or type(session_key) is not int or session_key <= 0:
        raise ValueError("invalid historical enrichment query")
    return f"https://api.openf1.org/v1/{endpoint}?session_key={session_key}"


def _retry_delay(value: str | None, now: float) -> float:
    try:
        seconds = float(value or "60")
        if not math.isfinite(seconds):
            raise ValueError("non-finite retry delay")
    except ValueError:
        try:
            date = parsedate_to_datetime(value or "")
            if date.tzinfo is None:
                raise ValueError("naive retry date")
            seconds = date.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            seconds = 60.0
    return max(60.0, seconds)


def _budget_delay(budget: RequestBudget, now: float) -> float:
    """Probe owned durable history without consuming a slot in either quota."""
    if not math.isfinite(now):
        raise ValueError("request timestamp must be finite")
    value = budget._read()
    timestamps = value["timestamps"]
    if not isinstance(timestamps, list) or not all(
        isinstance(timestamp, (int, float)) and math.isfinite(timestamp) for timestamp in timestamps
    ):
        raise ValueError("invalid persisted request history")
    retained = sorted(
        float(timestamp) for timestamp in timestamps if timestamp > now - budget.interval
    )
    not_before = value["not_before"]
    if not isinstance(not_before, (int, float)) or not math.isfinite(not_before):
        raise ValueError("invalid persisted cooldown")
    if retained and retained[-1] > now:
        raise ValueError("clock moved backwards relative to persisted requests")
    delay = max(0.0, not_before - now)
    if len(retained) >= budget.limit:
        delay = max(delay, 0.001, retained[len(retained) - budget.limit] + budget.interval - now)
    return delay


class OpenF1Client:
    """One shared worker owns durable OpenF1 budgets for an entire operation.

    Entering a context owns resources, not request authority. Only the local
    measurement boundary binds a genuinely validated historical session. Native
    FastF1 limiters and budget histories are never changed by this client.
    """

    def __init__(self, root: Path) -> None:
        self.root = _unlinked_path(root)
        self._worker: AbstractContextManager[None] | None = None
        self._scope: (
            tuple[int, Callable[[], None], Callable[[bytes, dict[str, Any]], None]] | None
        ) = None
        self._denied: PermissionError | None = None
        self._failure: BaseException | None = None
        self.last_request = float("-inf")
        self._pending_path = _unlinked_path(root / "runtime/openf1-pending-attempt.json")
        self.minute = RequestBudget(
            _unlinked_path(root / "runtime/openf1-minute-budget.json"),
            limit=25,
            interval_seconds=60,
        )
        self.spacing = RequestBudget(
            _unlinked_path(root / "runtime/openf1-spacing-budget.json"),
            limit=1,
            interval_seconds=2.1,
        )

    @property
    def active(self) -> bool:
        return self._worker is not None

    def check(self) -> None:
        if self._denied is not None:
            raise self._denied
        if self._failure is not None:
            raise self._failure

    def __enter__(self) -> Self:
        if self.active:
            raise ValueError("OpenF1 worker context is already active")
        self.check()
        guard_operation(
            self.root / "runtime", next_peak_bytes=65536, operation="OpenF1 worker ownership"
        )
        worker = network_worker(_unlinked_path(self.root / "runtime/fastf1-worker.lock"))
        worker.__enter__()
        self._worker = worker
        try:
            self._recover_pending()
        except BaseException as exc:
            self._failure = exc
            self._worker = None
            try:
                worker.__exit__(type(exc), exc, exc.__traceback__)
            except Exception as cleanup:
                exc.add_note(f"OpenF1 worker release failed: {cleanup!r}")
            raise
        return self

    def _read_pending(self) -> dict[str, Any] | None:
        path = _unlinked_path(self._pending_path)
        if not path.exists():
            return None
        if path.stat().st_size > 65536:
            raise ValueError("oversized OpenF1 pending attempt")
        value: Any = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "reserved_at", "endpoint", "session_key"}
            or value["schema_version"] != "openf1-pending-attempt-v1"
            or type(value["reserved_at"]) not in (int, float)
            or not math.isfinite(value["reserved_at"])
        ):
            raise ValueError("invalid OpenF1 pending attempt")
        historical_url(value["endpoint"], value["session_key"])
        return value

    def _guard_runtime_write(self, path: Path) -> None:
        _unlinked_path(path)
        guard_operation(path, next_peak_bytes=65536, operation="OpenF1 durable attempt history")

    def _clear_pending(self) -> None:
        self._guard_runtime_write(self._pending_path)
        self._pending_path.unlink()

    def _recover_pending(self) -> None:
        pending = self._read_pending()
        if pending is None:
            return
        now = time()
        if not math.isfinite(now) or pending["reserved_at"] > now:
            raise ValueError("clock moved backwards relative to pending OpenF1 attempt")
        # A crash may leave either or both slots already anchored. Validate both
        # before any recovery write; never replace or discard their histories.
        for budget in (self.minute, self.spacing):
            _unlinked_path(budget.path)
            _budget_delay(budget, now)
            timestamps = budget._read()["timestamps"]
            assert isinstance(timestamps, list)
            if not timestamps or max(timestamps) < pending["reserved_at"]:
                raise ValueError("pending OpenF1 attempt lacks its durable reservation")
        for budget in (self.minute, self.spacing):
            self._guard_runtime_write(budget.path)
            budget.defer_until(now + 60)
        # Partial recovery retains intent. A later owned worker conservatively
        # starts another full window and preserves any longer provider cooldown.
        self._clear_pending()

    def _begin_attempt(self, reserved_at: float, endpoint: str, session_key: int) -> None:
        if self._read_pending() is not None:
            raise ValueError("another OpenF1 attempt is pending")
        self._guard_runtime_write(self._pending_path)
        write_text_atomic(
            self._pending_path,
            json.dumps(
                {
                    "schema_version": "openf1-pending-attempt-v1",
                    "reserved_at": reserved_at,
                    "endpoint": endpoint,
                    "session_key": session_key,
                },
                sort_keys=True,
                allow_nan=False,
            ),
        )

    def _finish_attempt(
        self, reserved_at: float, sent_at: float, primary: BaseException | None
    ) -> None:
        try:
            pending = self._read_pending()
            if pending is None or pending["reserved_at"] != reserved_at:
                raise ValueError("OpenF1 pending attempt identity changed")
            if not math.isfinite(sent_at) or sent_at < reserved_at:
                raise ValueError("clock moved backwards at OpenF1 transport boundary")
            for budget in (self.minute, self.spacing):
                _unlinked_path(budget.path)
                _budget_delay(budget, sent_at)
                value = budget._read()
                timestamps = value["timestamps"]
                assert isinstance(timestamps, list)
                if not timestamps or timestamps[-1] not in (reserved_at, sent_at):
                    raise ValueError("OpenF1 attempt reservation changed before anchoring")
                value["timestamps"] = [*timestamps[:-1], sent_at]
                self._guard_runtime_write(budget.path)
                budget._write(value)
            self._clear_pending()
        except Exception as cleanup:
            if primary is not None:
                primary.add_note(f"OpenF1 attempt anchoring failed: {cleanup!r}")
                self._failure = primary
                return
            self._failure = cleanup
            raise
        except BaseException as cancellation:
            self._failure = cancellation
            if primary is not None:
                cancellation.add_note(f"OpenF1 attempt primary failure: {primary!r}")
            raise

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        worker, self._worker = self._worker, None
        self._scope = None
        assert worker is not None
        try:
            worker.__exit__(kind, error, traceback)
        except Exception as cleanup:
            if error is None:
                raise
            error.add_note(f"OpenF1 worker release failed: {cleanup!r}")
        if error is None:
            self.check()

    @contextmanager
    def _historical_scope(
        self,
        session_key: int,
        check: Callable[[], None],
        observer: Callable[[bytes, dict[str, Any]], None],
    ) -> Iterator[None]:
        self.check()
        if not self.active or self._scope is not None:
            raise ValueError("historical enrichment requires one active worker context")
        check()
        self._scope = session_key, check, observer
        try:
            yield
        finally:
            self._scope = None

    def _pause(self, delay: float, waited: float) -> float:
        if delay > MAX_OPERATION_WAIT - waited:
            raise ValueError(
                "OpenF1 cooldown exceeds bounded operation wait; persisted cooldown retained"
            )
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, 60.0)
            sleep(chunk)
            waited += chunk
            remaining -= chunk
            self.check()
            assert self._scope is not None
            self._scope[1]()
            guard_operation(
                self.root,
                next_peak_bytes=2 * MAX_RESPONSE_BYTES + 1024**2,
                operation="OpenF1 provider response",
            )
        return waited

    def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
        self.check()
        if not self.active:
            raise ValueError("OpenF1 transport requires an owned worker context")
        if self._scope is None or session_key != self._scope[0]:
            raise ValueError("OpenF1 transport requires validated historical scope")
        url = historical_url(endpoint, session_key)
        opener = build_opener(_NoRedirect())
        request = Request(url, headers={"Accept": "application/json"})
        _, check, observe = self._scope
        waited = 0.0
        for attempt in range(3):
            while True:
                check()
                guard_operation(
                    self.root,
                    next_peak_bytes=2 * MAX_RESPONSE_BYTES + 1024**2,
                    operation="OpenF1 provider response",
                )
                for budget in (self.minute, self.spacing):
                    _unlinked_path(budget.path)
                    guard_operation(
                        budget.path,
                        next_peak_bytes=65536,
                        operation="OpenF1 durable request reservation",
                    )
                local_delay = max(0.0, 2.1 - (monotonic() - self.last_request))
                now = time()
                delay = max(
                    local_delay, _budget_delay(self.minute, now), _budget_delay(self.spacing, now)
                )
                if delay == 0:
                    break
                waited = self._pause(max(delay, 0.001), waited)
            # Owned writes can themselves delay transport. Intent makes any
            # crash before the actual send time is durably anchored fail closed.
            try:
                for budget in (self.minute, self.spacing):
                    if budget.reserve(now=now) != 0:
                        raise ValueError("OpenF1 owned budget changed before transport")
                self._begin_attempt(now, endpoint, session_key)
            except BaseException as exc:
                self._failure = exc
                raise
            self.last_request = monotonic()
            response: Any
            http_error: HTTPError | None = None
            sent_at = time()
            try:
                response = opener.open(request, timeout=90)
            except HTTPError as exc:
                response, http_error = exc, exc
            except BaseException as exc:
                self._failure = exc
                self._finish_attempt(now, sent_at, exc)
                raise
            status = int(response.code if http_error is not None else response.status)
            if status in (401, 403):
                self._denied = PermissionError(f"OpenF1 access denied: HTTP {status}")
            primary: BaseException | None = None
            unavailable: HTTPError | None = None
            try:
                cooldown = None
                if status == 429:
                    delays = [
                        _retry_delay(str(value), time())
                        for key, value in response.headers.items()
                        if str(key).lower() == "retry-after"
                    ]
                    cooldown = time() + max(delays, default=60.0)
                    # Persist the provider's full cooldown even if body capture fails.
                    for budget in (self.minute, self.spacing):
                        _unlinked_path(budget.path)
                        guard_operation(
                            budget.path, next_peak_bytes=65536, operation="OpenF1 provider cooldown"
                        )
                        budget.defer_until(cooldown)
                actual = response.geturl()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                truncated = len(body) > MAX_RESPONSE_BYTES
                body = body[:MAX_RESPONSE_BYTES]
                fields = [[str(key), str(value)] for key, value in response.headers.items()]
                header_truncated = False
                if len(json.dumps(fields, ensure_ascii=False).encode()) > MAX_HEADER_BYTES:
                    retained: list[list[str]] = []
                    for field in fields:
                        if (
                            len(json.dumps([*retained, field], ensure_ascii=False).encode())
                            > MAX_HEADER_BYTES
                        ):
                            break
                        retained.append(field)
                    fields = retained
                    header_truncated = True
                headers = dict(fields)
                provenance = {
                    "url": url,
                    "actual_url": actual,
                    "http_status": status,
                    "headers": headers,
                    "headers_truncated": header_truncated,
                    "header_fields": fields,
                    "header_fields_sha256": hashlib.sha256(
                        json.dumps(fields, ensure_ascii=False).encode()
                    ).hexdigest(),
                    "observed_at": datetime.fromtimestamp(time(), UTC).isoformat(),
                    "body_truncated": truncated,
                }
                observe(body, provenance)
                if self._denied is not None:
                    raise self._denied
                if actual != url or 300 <= status < 400:
                    raise ValueError("OpenF1 redirect or actual URL change is not permitted")
                if truncated:
                    raise ValueError("enrichment response exceeded bounded storage budget")
                if status == 200:
                    return body, url
                if status == 429:
                    assert cooldown is not None
                    if attempt < 2:
                        waited = self._pause(max(0.0, cooldown - time()), waited)
                        continue
                # Preserve the available 404 body for the existing measurement API.
                from io import BytesIO

                result_error = HTTPError(
                    url, status, "OpenF1 historical response", response.headers, BytesIO(body)
                )
                if status == 404:
                    unavailable = result_error
                raise result_error
            except BaseException as exc:
                if (
                    isinstance(exc, Exception)
                    and self._denied is not None
                    and exc is not self._denied
                ):
                    self._denied.add_note(f"secondary provenance failure: {exc!r}")
                    primary = self._denied
                    raise self._denied from exc
                primary = exc
                if exc is not unavailable:
                    self._failure = exc
                raise
            finally:
                try:
                    response.close()
                except Exception as cleanup:
                    if primary is None:
                        primary = cleanup
                        self._failure = cleanup
                        raise
                    primary.add_note(f"OpenF1 response close failed: {cleanup!r}")
                    self._failure = primary
                except BaseException as cancellation:
                    primary = cancellation
                    self._failure = cancellation
                    raise
                finally:
                    self._finish_attempt(now, sent_at, primary)
        raise AssertionError("bounded retry loop exhausted")
