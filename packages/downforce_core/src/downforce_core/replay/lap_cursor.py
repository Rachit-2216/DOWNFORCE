"""Explicit leader/reference-lap start and end cursor semantics."""

from __future__ import annotations

from dataclasses import dataclass

from downforce_core.domain.identifiers import DriverId
from downforce_core.normalization.models import NormalizedSession


@dataclass(frozen=True, slots=True)
class LapCursor:
    lap_number: int
    start_time_ms: int
    end_time_ms: int
    reference_driver_id: DriverId

    def __post_init__(self) -> None:
        if self.lap_number < 1:
            raise ValueError("lap_number must be positive")
        if self.start_time_ms < 0 or self.end_time_ms < self.start_time_ms:
            raise ValueError("lap cursor range is invalid")


def build_lap_cursors(session: NormalizedSession) -> tuple[LapCursor, ...]:
    """Map lap N to the P1 lap-end observation and that driver's lap start.

    A lap is omitted when canonical race-position history cannot identify a P1 finisher or
    the corresponding lap has no defensible start timestamp. Callers must fail clearly for
    omitted laps instead of guessing from another driver's progress.
    """

    leaders: dict[int, tuple[int, DriverId]] = {}
    for position in session.race_positions:
        if position.position != 1 or position.lap_number is None:
            continue
        candidate = (position.session_time_ms, position.driver_id)
        existing = leaders.get(position.lap_number)
        if existing is None or (candidate[0], str(candidate[1])) < (
            existing[0],
            str(existing[1]),
        ):
            leaders[position.lap_number] = candidate
    laps = {(lap.driver_id, lap.lap_number): lap for lap in session.laps}
    cursors: list[LapCursor] = []
    for lap_number, (end_time, driver_id) in sorted(leaders.items()):
        lap = laps.get((driver_id, lap_number))
        if lap is None or lap.lap_start_time_ms is None:
            continue
        if end_time < lap.lap_start_time_ms:
            continue
        cursors.append(
            LapCursor(
                lap_number=lap_number,
                start_time_ms=lap.lap_start_time_ms,
                end_time_ms=end_time,
                reference_driver_id=driver_id,
            )
        )
    return tuple(cursors)


__all__ = ["LapCursor", "build_lap_cursors"]
