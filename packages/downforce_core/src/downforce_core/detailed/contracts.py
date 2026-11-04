"""Small semantic contracts shared by detailed ingestion and telemetry consumers."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from downforce_core.detailed.catalog import Completion
from downforce_core.detailed.reconciliation import classify_completion


@dataclass(frozen=True)
class Interval:
    kind: Literal["seconds", "laps-down", "unknown", "unavailable"]
    value: float | None


def parse_interval(raw: object) -> Interval:
    if raw is None:
        return Interval("unavailable", None)
    if isinstance(raw, str):
        matched = re.fullmatch(r"\+?(\d+)\s+LAPS?", raw.strip(), re.IGNORECASE)
        if matched:
            return Interval("laps-down", int(matched.group(1)))
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return Interval("unknown", None)
    try:
        number = float(raw)
    except ValueError:
        return Interval("unknown", None)
    return (
        Interval("seconds", number)
        if math.isfinite(number) and number >= 0
        else Interval("unknown", None)
    )


def session_completion(scheduled_state: Completion, statuses: Iterable[str]) -> Completion:
    if scheduled_state in ("future", "cancelled"):
        return scheduled_state
    state = classify_completion(scheduled_state, statuses, supported=None)
    return "completed" if state == "completed" else scheduled_state


@dataclass(frozen=True)
class ChannelQuality:
    status: Literal["verified", "partial", "degraded", "unavailable"]
    samples: int
    present: int
    missing: int
    invalid: int


def channel_quality(channel: str, values: Iterable[object]) -> ChannelQuality:
    """Report violations without modifying source values or throwing away outliers.

    Range limits are broad physical/plausibility flags, not cleaning rules.
    DRS codes outside the public FastF1 codes remain unknown, never forced closed.
    """
    samples = present = missing = invalid = 0
    for value in values:
        samples += 1
        if value is None:
            missing += 1
            continue
        try:
            number = float(str(value)) if not isinstance(value, bool) else float(value)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not math.isfinite(number):
            missing += 1
            continue
        present += 1
        valid = True
        if channel == "gear":
            valid = number.is_integer() and 0 <= number <= 8
        elif channel == "brake":
            valid = number in (0, 1)
        elif channel == "throttle":
            valid = 0 <= number <= 100
        elif channel == "speed":
            valid = 0 <= number <= 450
        elif channel == "rpm":
            valid = 0 <= number <= 25000
        elif channel == "drs":
            valid = number in (0, 1, 2, 3, 8, 10, 12, 14)
        elif channel in ("distance", "relative_distance"):
            valid = number >= 0 and (channel != "relative_distance" or number <= 1)
        invalid += not valid
    status: Literal["verified", "partial", "degraded", "unavailable"]
    if invalid:
        status = "degraded"
    elif not present:
        status = "unavailable"
    else:
        status = "partial" if missing else "verified"
    return ChannelQuality(status, samples, present, missing, invalid)
