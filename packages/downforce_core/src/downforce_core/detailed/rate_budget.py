"""Durable single-worker request budgets supplementing provider-native limiters."""

from __future__ import annotations

import json
import math
from pathlib import Path
from time import sleep, time

from downforce_core.storage.layout import write_text_atomic


class RequestBudget:
    """A denied reservation does not create another attempted-request timestamp.

    One network worker owns this budget. It persists before the network attempt,
    so a crash may waste a slot but cannot gain one. Provider limiters remain in
    place and are never reset, altered or disabled. A new worker inherits history.
    An existing uninstrumented job can explicitly impose a full cooldown.
    """

    def __init__(self, path: Path, *, limit: int = 450, interval_seconds: float = 3600) -> None:
        if (
            type(limit) is not int
            or limit < 1
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("invalid request budget")
        self.path, self.limit, self.interval = path, limit, interval_seconds

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "limit": self.limit,
                "interval": self.interval,
                "timestamps": [],
                "not_before": 0.0,
            }
        value: dict[str, object] = json.loads(self.path.read_text())
        if value.get("limit") != self.limit or value.get("interval") != self.interval:
            raise ValueError("persisted budget policy cannot be changed during active history")
        return value

    def _write(self, value: dict[str, object]) -> None:
        write_text_atomic(self.path, json.dumps(value, sort_keys=True, allow_nan=False))

    def reserve(self, *, now: float) -> float:
        if not math.isfinite(now):
            raise ValueError("request timestamp must be finite")
        value = self._read()
        timestamps = value["timestamps"]
        if not isinstance(timestamps, list) or not all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in timestamps
        ):
            raise ValueError("invalid persisted request history")
        retained = sorted(float(v) for v in timestamps if float(v) > now - self.interval)
        not_before = value["not_before"]
        if not isinstance(not_before, (int, float)) or not math.isfinite(not_before):
            raise ValueError("invalid persisted cooldown")
        if not_before > now:
            return not_before - now
        if retained and retained[-1] > now:
            raise ValueError("clock moved backwards relative to persisted requests")
        if len(retained) >= self.limit:
            return max(0.001, retained[len(retained) - self.limit] + self.interval - now)
        value["timestamps"] = [*retained, now]
        self._write(value)
        return 0.0

    def defer_until(self, timestamp: float) -> None:
        if not math.isfinite(timestamp):
            raise ValueError("invalid request cooldown")
        value = self._read()
        existing = value["not_before"]
        if not isinstance(existing, (int, float)):
            raise ValueError("invalid persisted cooldown")
        value["not_before"] = max(existing, timestamp)
        self._write(value)

    def wait(self) -> None:
        while delay := self.reserve(now=time()):
            sleep(min(delay, 60))
