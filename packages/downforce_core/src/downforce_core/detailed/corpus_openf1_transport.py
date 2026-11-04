"""Additive production hourly quota; the original transport stays unchanged."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from downforce_core.detailed import openf1_transport as base
from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.detailed.rate_budget import RequestBudget
from downforce_core.storage.layout import write_text_atomic


class HourlyBudgetStop(ValueError):
    """Durable hourly exhaustion is an operation stop, never a retry/sleep."""


def _history_delay(budget: RequestBudget, now: float) -> float:
    _unlinked_path(budget.path)
    value = budget._read()
    timestamps = value.get("timestamps")
    if (
        set(value) != {"limit", "interval", "timestamps", "not_before"}
        or not isinstance(timestamps, list)
        or any(type(stamp) not in (int, float) for stamp in timestamps)
        or type(value.get("not_before")) not in (int, float)
    ):
        raise ValueError("malformed production or legacy request history")
    return base._budget_delay(budget, now)


class _ProductionBudget(RequestBudget):
    """Strict production history checks without changing legacy budget policies."""

    def reserve(self, *, now: float) -> float:
        _history_delay(self, now)
        return super().reserve(now=now)

    def defer_until(self, timestamp: float) -> None:
        _history_delay(self, base.time())  # type: ignore[attr-defined]
        super().defer_until(timestamp)


class CorpusOpenF1Client(base.OpenF1Client):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        for name in ("minute", "spacing"):
            original: RequestBudget = getattr(self, name)
            setattr(
                self,
                name,
                _ProductionBudget(
                    original.path,
                    limit=original.limit,
                    interval_seconds=original.interval,
                ),
            )
        self.hour = RequestBudget(
            _unlinked_path(self.root / "runtime/corpus-openf1-hour-budget.json"),
            limit=650,
            interval_seconds=3600,
        )
        self.hour_pending = _unlinked_path(self.root / "runtime/corpus-openf1-hour-pending.json")
        self._cleanup_failure: BaseException | None = None

    def _legacy_history(self, now: float) -> None:
        for budget in (self.minute, self.spacing):
            _history_delay(budget, now)

    def _hour_intent(self) -> dict[str, Any] | None:
        path = _unlinked_path(self.hour_pending)
        if not path.exists():
            return None
        if path.stat().st_size > 65536:
            raise ValueError("oversized production hourly intent")
        value: Any = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or set(value) != {"reserved_at", "endpoint", "session_key"}
            or type(value["reserved_at"]) not in (int, float)
            or not math.isfinite(value["reserved_at"])
        ):
            raise ValueError("malformed production hourly intent")
        base.historical_url(value["endpoint"], value["session_key"])
        return value

    def _recover_pending(self) -> None:
        # Called by the base while holding the SAME native network worker lock.
        now = base.time()  # type: ignore[attr-defined]  # same patchable transport clock
        if not math.isfinite(now):
            raise ValueError("invalid production quota clock")
        self._legacy_history(now)
        pending = self._read_pending()
        intent = self._hour_intent()
        if not self.hour.path.exists():
            latest: list[float] = []
            for budget in (self.minute, self.spacing):
                if budget.path.exists():
                    _history_delay(budget, now)
                    values = budget._read()["timestamps"]
                    assert isinstance(values, list)
                    latest.extend(float(value) for value in values)
                    stamp = budget.path.stat().st_mtime
                    if not math.isfinite(stamp) or stamp > now:
                        raise ValueError("future legacy OpenF1 budget mtime")
                    latest.append(stamp)
            for record in (pending, intent):
                if record is not None:
                    if record["reserved_at"] > now:
                        raise ValueError("future pending production request")
                    latest.append(record["reserved_at"])
            self._guard_runtime_write(self.hour.path)
            self.hour._write(
                {
                    "limit": 650,
                    "interval": 3600.0,
                    "timestamps": [],
                    "not_before": max(latest) + 3600 if latest else 0.0,
                }
            )
        _history_delay(self.hour, now)
        if intent is not None:
            value = self.hour._read()
            timestamps = value["timestamps"]
            if (
                intent["reserved_at"] > now
                or not isinstance(timestamps, list)
                or not timestamps
                or max(timestamps) < intent["reserved_at"]
            ):
                raise ValueError("hourly pending request lacks durable reservation")
        if pending is not None or intent is not None:
            self._guard_runtime_write(self.hour.path)
            self.hour.defer_until(now + 3600)
        super()._recover_pending()
        if intent is not None:
            self._guard_runtime_write(self.hour_pending)
            self.hour_pending.unlink()

    def _begin_attempt(self, reserved_at: float, endpoint: str, session_key: int) -> None:
        self._legacy_history(reserved_at)
        if self._hour_intent() is not None:
            raise ValueError("production hourly request already pending")
        delay = _history_delay(self.hour, reserved_at)
        if delay:
            error = HourlyBudgetStop("production OpenF1 hourly budget/cooldown exhausted")
            self._failure = error
            raise error
        self._guard_runtime_write(self.hour.path)
        if self.hour.reserve(now=reserved_at):
            error = HourlyBudgetStop("production hourly reservation changed")
            self._failure = error
            raise error
        self._guard_runtime_write(self.hour_pending)
        write_text_atomic(
            self.hour_pending,
            json.dumps(
                {
                    "reserved_at": reserved_at,
                    "endpoint": endpoint,
                    "session_key": session_key,
                },
                allow_nan=False,
                sort_keys=True,
            ),
        )
        super()._begin_attempt(reserved_at, endpoint, session_key)

    def _finish_attempt(
        self,
        reserved_at: float,
        sent_at: float,
        primary: BaseException | None,
    ) -> None:
        # Anchor the actual send before the base can erase crash-recovery intent.
        try:
            # The inherited boundary creates a fresh HTTPError for an ordinary
            # response. Any secondary note is therefore conservative failure
            # evidence; its text is never interpreted or stripped.
            if primary is not None and bool(getattr(primary, "__notes__", None)):
                self._cleanup_failure = primary
            self._legacy_history(sent_at)
            intent = self._hour_intent()
            if intent is None or intent["reserved_at"] != reserved_at:
                raise ValueError("production hourly intent changed")
            if not math.isfinite(sent_at) or sent_at < reserved_at:
                raise ValueError("production send clock moved backwards")
            _history_delay(self.hour, sent_at)
            value = self.hour._read()
            timestamps = value["timestamps"]
            if not isinstance(timestamps, list) or not timestamps or timestamps[-1] != reserved_at:
                raise ValueError("production hourly reservation changed")
            value["timestamps"] = [*timestamps[:-1], sent_at]
            self._guard_runtime_write(self.hour.path)
            self.hour._write(value)
            super()._finish_attempt(reserved_at, sent_at, primary)
            if primary is not None and bool(getattr(primary, "__notes__", None)):
                self._cleanup_failure = primary
            if self._pending_path.exists():
                raise self._failure or ValueError("base pending request was not anchored")
            self._guard_runtime_write(self.hour_pending)
            self.hour_pending.unlink()
        except BaseException as exc:
            self._failure = exc
            raise

    def fetch(self, endpoint: str, session_key: int) -> tuple[bytes, str]:
        try:
            self.check()
            self._legacy_history(base.time())  # type: ignore[attr-defined]
            return super().fetch(endpoint, session_key)
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            # Ordinary optional-channel failures are recorded by the workflow.
            # Permission, quota, authority, cancellation and cleanup remain latched.
            ordinary = not isinstance(exc, HTTPError) or exc.code not in (401, 403)
            if (
                ordinary
                and self._denied is None
                and self._cleanup_failure is None
                and not getattr(exc, "__notes__", ())
                and self._failure is exc
                and not self.hour_pending.exists()
                and not self._pending_path.exists()
            ):
                self._failure = None
            raise
        except BaseException as exc:
            self._failure = exc
            raise
