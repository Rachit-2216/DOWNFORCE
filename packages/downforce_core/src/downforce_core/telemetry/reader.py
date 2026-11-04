"""Read one immutable driver/lap revision without provider calls or clock guesses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.detailed.storage import DetailedStore
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.telemetry.alignment import align_positions
from downforce_core.telemetry.clock import _duration_ns, extract_session_clock
from downforce_core.telemetry.types import (
    CornerMarker,
    LapBounds,
    LapMetadata,
    PositionSamples,
    RawTelemetry,
)

CHANNELS = {
    "speed_kmh": "speed",
    "rpm": "rpm",
    "throttle_percent": "throttle",
    "brake": "brake",
    "gear": "gear",
    "drs_raw": "drs",
}


@dataclass(frozen=True)
class StoredLap:
    source: RawTelemetry
    bounds: LapBounds
    metadata: LapMetadata
    context: dict[str, Any]
    clock_source: str
    channel_availability: dict[str, str]
    source_sensors: tuple[str | None, ...] = ()


def _driver(value: object) -> str:
    try:
        token = validate_safe_identifier(str(value))
    except ValueError as exc:
        raise ValueError("driver must be a positive provider driver number") from exc
    if not token.isdigit() or int(token) < 1:
        raise ValueError("driver must be a positive provider driver number")
    return token


def _lap_index(laps: pa.Table, driver: str, lap_number: int) -> int:
    if type(lap_number) is not int or not 1 <= lap_number <= 1000:
        raise ValueError("lap number must be a bounded positive integer")
    if not {"driver_number", "lap_number"}.issubset(laps.column_names):
        raise ValueError("lap identity columns are unavailable")
    matches = [
        i
        for i, (number, lap) in enumerate(
            zip(laps["driver_number"].to_pylist(), laps["lap_number"].to_pylist(), strict=True)
        )
        if str(number) == driver and lap == lap_number
    ]
    if not matches:
        raise ValueError("requested lap is missing")
    if len(matches) > 1:
        raise ValueError("requested lap is ambiguous")
    return matches[0]


def _value(table: pa.Table, name: str, index: int) -> Any:
    return table[name][index].as_py() if name in table.column_names else None


class TelemetryReader:
    def __init__(self, store: DetailedStore) -> None:
        self.store = store

    def sessions(self) -> list[dict[str, Any]]:
        """Discover active canonical revisions only, never candidate/study directories."""
        if not self.store.root.exists():
            return []
        result = []
        for path in sorted(self.store.root.iterdir()):
            if not path.is_dir() or not (path / "active.json").is_file():
                continue
            manifest = self.store.manifest(path.name)
            session = manifest["metadata"].get("session", {})
            names = sorted(manifest["tables"])
            drivers = sorted(
                {name.removeprefix("car-data-") for name in names if name.startswith("car-data-")}
            )
            if "laps" in names:
                laps = self.store.read_table(path.name, "laps", revision=manifest["revision"])
                if "driver_number" in laps.column_names:
                    drivers = sorted(
                        set(drivers)
                        | {
                            str(value)
                            for value in laps["driver_number"].to_pylist()
                            if value is not None
                        }
                    )
            result.append(
                {
                    "session_id": path.name,
                    "revision": manifest["revision"],
                    "season": session.get("season"),
                    "session_type": session.get("session_type"),
                    "event": session.get("event_name"),
                    "circuit": session.get("circuit", session.get("circuit_name")),
                    "drivers": drivers,
                    "available_tables": names,
                    "channel_availability": {
                        "car": "available"
                        if any(n.startswith("car-data-") for n in names)
                        else "unavailable",
                        "position": "available"
                        if any(n.startswith("pos-data-") for n in names)
                        else "unavailable",
                        "weather": "available" if "weather-data" in names else "unavailable",
                        "corners": "available" if "corners" in names else "unavailable",
                    },
                }
            )
        return result

    def laps(
        self, session_id: str, driver: str, *, revision: str | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        driver = _driver(driver)
        manifest = self.store.manifest(session_id, revision=revision)
        pinned = manifest["revision"]
        laps = self.store.read_table(session_id, "laps", revision=pinned)
        if not {"driver_number", "lap_number"}.issubset(laps.column_names):
            raise ValueError("lap identity columns are unavailable")
        result = []
        for index, number in enumerate(laps["driver_number"].to_pylist()):
            if str(number) != driver:
                continue
            lap = _value(laps, "lap_number", index)
            if lap is None or isinstance(lap, bool) or lap != int(lap) or not 1 <= lap <= 1000:
                raise ValueError("invalid native lap identity")
            duration = (
                _duration_ns(laps, "lap_time")[index] if "lap_time" in laps.column_names else None
            )
            start = (
                _duration_ns(laps, "lap_start_time")[index]
                if "lap_start_time" in laps.column_names
                else None
            )
            row = {
                "driver": driver,
                "lap_number": int(lap),
                "lap_time_seconds": None if duration is None else duration / 1e9,
                "compound": _value(laps, "compound", index),
                "stint": _value(laps, "stint", index),
                "tyre_life": _value(laps, "tyre_life", index),
                "track_status": _value(laps, "track_status", index),
                "reliable": _value(laps, "is_accurate", index) is True,
                "deleted": _value(laps, "deleted", index) is True,
                "provider_generated": _value(laps, "provider_generated", index) is True,
                "pit_in": _value(laps, "pit_in_time", index) is not None,
                "pit_out": _value(laps, "pit_out_time", index) is not None,
                "timing_available": start is not None
                and duration is not None
                and duration > 0
                and start >= 0,
                "car_available": f"car-data-{driver}" in manifest["tables"],
                "position_available": f"pos-data-{driver}" in manifest["tables"],
                "weather_available": "weather-data" in manifest["tables"],
                # Never infer Q1/Q2/Q3 from lap numbers or timing gaps.
                "qualifying_phase": _value(laps, "qualifying_phase", index),
            }
            for sector in (1, 2, 3):
                field = f"sector_{sector}_time"
                value = _duration_ns(laps, field)[index] if field in laps.column_names else None
                row[f"sector_{sector}_seconds"] = None if value is None else value / 1e9
            result.append(row)
        if len({row["lap_number"] for row in result}) != len(result):
            raise ValueError("requested lap is ambiguous")
        return pinned, result

    def corners(self, session_id: str, *, revision: str) -> tuple[CornerMarker, ...]:
        manifest = self.store.manifest(session_id, revision=revision)
        if "corners" not in manifest["tables"]:
            return ()
        table = self.store.read_table(session_id, "corners", revision=revision)
        if table.num_rows > 1000:
            raise ValueError("corner marker limit exceeded")
        markers = []
        for index in range(table.num_rows):

            def value(native: str, normalized: str, row_index: int = index) -> Any:
                return _value(
                    table, normalized if normalized in table.column_names else native, row_index
                )

            number, distance = value("Number", "number"), value("Distance", "distance")
            if number is None or distance is None:
                raise ValueError("native corner marker identity unavailable")
            markers.append(
                CornerMarker(
                    int(number),
                    str(value("Letter", "letter") or ""),
                    float(distance),
                    value("X", "x"),
                    value("Y", "y"),
                    value("Angle", "angle"),
                    "provider-native-corners",
                )
            )
        return tuple(markers)

    def load_lap(
        self,
        session_id: str,
        driver: str,
        lap_number: int,
        *,
        revision: str | None = None,
        window_seconds: float | None = None,
    ) -> StoredLap:
        validate_safe_identifier(session_id)
        driver = _driver(driver)
        manifest = self.store.manifest(session_id, revision=revision)
        pinned = manifest["revision"]
        laps = self.store.read_table(session_id, "laps", revision=pinned)
        index = _lap_index(laps, driver, lap_number)
        if not {"lap_start_time", "lap_time"}.issubset(laps.column_names):
            raise ValueError("observed lap timing is unavailable")
        start = _duration_ns(laps, "lap_start_time")[index]
        duration = _duration_ns(laps, "lap_time")[index]
        if start is None or duration is None or start < 0 or duration <= 0:
            raise ValueError("lap has no usable observed timing boundaries")
        table_name = f"car-data-{driver}"
        if table_name not in manifest["tables"]:
            raise ValueError("car telemetry is unavailable for the selected driver")
        time_range = None
        if window_seconds is not None:
            import math

            if not math.isfinite(window_seconds) or not 0 < window_seconds <= 10:
                raise ValueError("invalid telemetry window")
            # This derived ms field is present only for a verified native clock.
            # Include bounded edge support; do not prune Date using provider Time.
            schema = manifest["tables"][table_name]["schema"]
            if "session_time_ms:" in schema:
                time_range = (
                    start / 1e6 - window_seconds * 1000,
                    (start + duration) / 1e6 + window_seconds * 1000,
                )
        car = self.store.read_table(session_id, table_name, revision=pinned, time_range=time_range)
        clock = extract_session_clock(car, laps)
        if any(value is None for value in clock.session_time_ns):
            raise ValueError("telemetry contains missing timestamps; raw observations are retained")
        times = tuple(int(value) for value in clock.session_time_ns if value is not None)
        channels: dict[str, tuple[float | int | bool | None, ...]] = {}
        availability = {}
        season = manifest["metadata"].get("session", {}).get("season")
        for channel, field in CHANNELS.items():
            if channel == "drs_raw" and type(season) is int and season >= 2026:
                # FastF1's native parser supplies 0 when absent Channel 45 is
                # omitted in 2026. Retain canonical values; do not call them OFF.
                channels[channel] = (None,) * car.num_rows
                availability[channel] = "not-recorded-provider-parser-default"
            elif field in car.column_names:
                channels[channel] = tuple(car[field].to_pylist())
                availability[channel] = (
                    "available" if car[field].null_count < car.num_rows else "empty"
                )
            else:
                channels[channel] = (None,) * car.num_rows
                availability[channel] = "unavailable"
        distance = tuple(car["distance"].to_pylist()) if "distance" in car.column_names else None
        position_name = f"pos-data-{driver}"
        for axis in ("x", "y", "z"):
            channels[axis] = (None,) * car.num_rows
            availability[axis] = "unavailable"
        if position_name in manifest["tables"]:
            position = self.store.read_table(session_id, position_name, revision=pinned)
            if {"x", "y"}.issubset(position.column_names):
                try:
                    position_clock = extract_session_clock(position, laps)
                    if any(value is None for value in position_clock.session_time_ns):
                        raise ValueError("position clock missing")
                    aligned = align_positions(
                        PositionSamples(
                            tuple(int(t) for t in position_clock.session_time_ns if t is not None),
                            tuple(position["x"].to_pylist()),
                            tuple(position["y"].to_pylist()),
                            tuple(position["z"].to_pylist())
                            if "z" in position.column_names
                            else None,
                        ),
                        times,
                    )
                    for i, axis in enumerate(("x", "y", "z")):
                        channels[axis] = tuple(row[i] for row in aligned)
                        availability[axis] = (
                            "derived-time-aligned"
                            if axis in position.column_names
                            else "unavailable"
                        )
                except ValueError:
                    for axis in ("x", "y", "z"):
                        availability[axis] = "unsupported-position-clock"
        sensors = (
            tuple(car["provider_source"].to_pylist())
            if "provider_source" in car.column_names
            else (None,) * car.num_rows
        )
        context = {
            name: _value(laps, name, index)
            for name in ("compound", "stint", "tyre_life", "track_status", "is_accurate")
        }
        for sector in (1, 2, 3):
            field = f"sector_{sector}_time"
            value = _duration_ns(laps, field)[index] if field in laps.column_names else None
            context[f"sector_{sector}_seconds"] = None if value is None else value / 1e9
        return StoredLap(
            RawTelemetry(times, channels, distance),
            LapBounds(start, start + duration),
            LapMetadata(
                driver,
                lap_number,
                pinned,
                duration / 1e9,
                pit_in=_value(laps, "pit_in_time", index) is not None,
                pit_out=_value(laps, "pit_out_time", index) is not None,
                deleted=_value(laps, "deleted", index) is True,
                provider_generated=_value(laps, "provider_generated", index) is True,
                reliable=_value(laps, "is_accurate", index) is True,
            ),
            context,
            clock.source,
            availability,
            sensors,
        )
