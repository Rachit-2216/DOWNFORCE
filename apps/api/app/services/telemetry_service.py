"""Immutable revision-pinned, read-only telemetry products with a bounded cache."""

import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict
from math import isfinite
from threading import RLock
from typing import Any, TypeVar, cast

from downforce_core.detailed.storage import DetailedStore
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.telemetry.alignment import align_laps, prepare_lap_trace
from downforce_core.telemetry.analysis import analyze_windows, build_windows, microsectors
from downforce_core.telemetry.reader import CHANNELS, StoredLap, TelemetryReader
from downforce_core.telemetry.types import (
    ALIGNMENT_VERSION,
    CORNER_VERSION,
    AlignmentSettings,
    ChannelValue,
    Comparison,
    LapTrace,
    PrepareSettings,
    TraceRejected,
)
from fastapi import HTTPException

from app.schemas.telemetry import (
    AlignedResponse,
    CompareSettings,
    ComparisonResponse,
    CornersResponse,
    MicrosectorsResponse,
    NativeSamples,
    TelemetryLap,
    TelemetryLapPage,
    TelemetryModel,
    TelemetrySession,
    TelemetrySessionPage,
    TraceResponse,
    TraceSettings,
    WindowComparisonResponse,
)
from app.services.telemetry_context_service import TelemetryContextService

T = TypeVar("T", bound=TelemetryModel)
ALL_CHANNELS = frozenset(CHANNELS) | {"x", "y", "z"}


class TelemetryDataUnavailable(Exception):
    """Safe telemetry-only error reason; canonical observations remain unchanged."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _channels(settings: TraceSettings) -> tuple[str, ...]:
    selected = tuple(settings.channels.split(","))
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(c not in ALL_CHANNELS for c in selected)
    ):
        raise HTTPException(422, "Unknown or duplicate telemetry channel")
    return selected


def _prepare(settings: TraceSettings) -> PrepareSettings:
    return PrepareSettings(
        **{name: getattr(settings, name) for name in PrepareSettings.__dataclass_fields__}
    )


def _alignment(settings: CompareSettings) -> AlignmentSettings:
    return AlignmentSettings(
        **{name: getattr(settings, name) for name in AlignmentSettings.__dataclass_fields__}
    )


def _valid(channel: str, value: ChannelValue) -> bool:
    if value is None:
        return False
    if channel == "brake":
        return isinstance(value, bool)
    if isinstance(value, bool) or not isfinite(value):
        return False
    if channel in {"gear", "drs_raw"}:
        if not isinstance(value, int):
            return False
    limits = {
        "speed_kmh": (0, 450),
        "rpm": (0, 22000),
        "throttle_percent": (0, 100),
        "gear": (0, 8),
    }
    return channel not in limits or limits[channel][0] <= value <= limits[channel][1]


def _wire(
    channels: Any, selected: tuple[str, ...], *, raw: bool = False
) -> tuple[dict[str, list[ChannelValue]], dict[str, list[bool]]]:
    values: dict[str, list[ChannelValue]] = {}
    masks = {}
    for channel in selected:
        observations = channels[channel]
        mask = [_valid(channel, value) for value in observations]
        masks[channel] = mask
        values[channel] = [
            value
            if (raw and (value is None or isinstance(value, bool) or isfinite(value))) or valid
            else None
            for value, valid in zip(observations, mask, strict=True)
        ]
    return values, masks


class TelemetryService:
    cache_limit = 16

    def __init__(
        self, store: DetailedStore, context_service: TelemetryContextService | None = None
    ) -> None:
        self.store = store
        self.context_service = context_service
        self.reader = TelemetryReader(store)
        self._cache: OrderedDict[tuple[str, ...], TelemetryModel] = OrderedDict()
        self._lock = RLock()

    @property
    def cache_entries(self) -> int:
        with self._lock:
            return len(self._cache)

    def _cached(self, key: tuple[str, ...], build: Callable[[], T]) -> T:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return cast(T, self._cache[key].model_copy(deep=True))
        result = build()
        with self._lock:
            self._cache[key] = result.model_copy(deep=True)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_limit:
                self._cache.popitem(last=False)
        return result

    def _pin(self, session_id: str, revision: str | None) -> str:
        try:
            validate_safe_identifier(session_id)
        except ValueError as exc:
            raise HTTPException(422, "Invalid telemetry session identifier") from exc
        if revision is not None and not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise HTTPException(422, "Invalid telemetry revision identifier")
        if not (self.store.root / session_id / "active.json").is_file() and revision is None:
            raise HTTPException(404, "Telemetry session not found")
        if (
            revision is not None
            and not (
                self.store.root / session_id / "revisions" / revision / "manifest.json"
            ).is_file()
        ):
            raise HTTPException(404, "Telemetry revision not found")
        return str(self.store.manifest(session_id, revision=revision)["revision"])

    def sessions(
        self,
        *,
        season: int | None = None,
        session_type: str | None = None,
        event: str | None = None,
        circuit: str | None = None,
        driver: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> TelemetrySessionPage:
        rows = [TelemetrySession.model_validate(row) for row in self.reader.sessions()]
        filtered = [
            row
            for row in rows
            if (season is None or row.season == season)
            and (session_type is None or row.session_type == session_type)
            and (event is None or event.casefold() in (row.event or "").casefold())
            and (circuit is None or circuit.casefold() in (row.circuit or "").casefold())
            and (driver is None or driver in row.drivers)
        ]
        items = filtered[offset : offset + limit]
        if self.context_service is not None:
            items = [
                row.model_copy(
                    update=self.context_service.resolve(
                        self.store, row.session_id, row.revision
                    ).model_dump()
                )
                for row in items
            ]
        return TelemetrySessionPage(items=items, total=len(filtered), offset=offset, limit=limit)

    def laps(
        self,
        session_id: str,
        driver: str,
        *,
        revision: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> TelemetryLapPage:
        pinned = self._pin(session_id, revision)
        _, rows = self.reader.laps(session_id, driver, revision=pinned)
        if not rows:
            raise HTTPException(404, "Telemetry driver not found")
        return TelemetryLapPage(
            session_id=session_id,
            revision=pinned,
            driver=driver,
            items=[TelemetryLap.model_validate(row) for row in rows[offset : offset + limit]],
            total=len(rows),
            offset=offset,
            limit=limit,
        )

    def _load(
        self, session_id: str, driver: str, lap: int, pinned: str, settings: TraceSettings
    ) -> tuple[StoredLap, LapTrace]:
        stored = self.reader.load_lap(
            session_id, driver, lap, revision=pinned, window_seconds=settings.max_edge_gap_seconds
        )
        selected = [
            i
            for i, t in enumerate(stored.source.session_time_ns)
            if stored.bounds.start_ns <= t <= stored.bounds.end_ns
        ]
        if len(selected) > settings.max_points:
            raise HTTPException(413, "Selected native lap exceeds point limit")
        moving_channels = ("speed_kmh", "rpm", "throttle_percent", "gear")
        if len(selected) >= 2 and all(
            all(
                stored.source.channels[channel][i] == 0
                and not isinstance(stored.source.channels[channel][i], bool)
                for i in selected
            )
            for channel in moving_channels
        ):
            raise TelemetryDataUnavailable(
                "allzero_source",
                "Selected lap has constant-zero provider car channels; telemetry is unusable",
            )
        prepared = prepare_lap_trace(
            stored.source,
            bounds=stored.bounds,
            metadata=stored.metadata,
            settings=_prepare(settings),
        )
        if len(prepared.distance_m) > settings.max_points:
            raise HTTPException(
                413, "Selected lap exceeds native point limit; observations were not downsampled"
            )
        if any(not isfinite(d) for d in prepared.distance_m):
            raise TraceRejected("nonfinite_distance")
        return stored, prepared

    def trace(
        self,
        session_id: str,
        driver: str,
        lap: int,
        *,
        revision: str | None = None,
        settings: TraceSettings | None = None,
    ) -> TraceResponse:
        settings = settings or TraceSettings()
        selected = _channels(settings)
        pinned = self._pin(session_id, revision)
        key = (
            "trace",
            session_id,
            pinned,
            driver,
            str(lap),
            settings.model_dump_json(),
            ALIGNMENT_VERSION,
            CORNER_VERSION,
        )

        def build() -> TraceResponse:
            stored, prepared = self._load(session_id, driver, lap, pinned, settings)
            native_indices = [
                i
                for i, t in enumerate(stored.source.session_time_ns)
                if stored.bounds.start_ns <= t <= stored.bounds.end_ns
            ]
            if len(native_indices) > settings.max_points:
                raise HTTPException(413, "Selected native lap exceeds point limit")
            native_selected = tuple(
                channel for channel in selected if channel not in {"x", "y", "z"}
            )
            raw = {
                channel: tuple(stored.source.channels[channel][i] for i in native_indices)
                for channel in native_selected
            }
            native_channels, native_masks = _wire(raw, native_selected, raw=True)
            channels, masks = _wire(prepared.channels, selected)
            _, lap_rows = self.reader.laps(session_id, driver, revision=pinned)
            context = next(
                TelemetryLap.model_validate(row) for row in lap_rows if row["lap_number"] == lap
            )
            flags = list(prepared.flags)
            if any(not valid for mask in masks.values() for valid in mask):
                flags.append("invalid_or_missing_derived_channel_values_masked")
            return TraceResponse(
                session_id=session_id,
                revision=pinned,
                driver=driver,
                lap_number=lap,
                lap_time_seconds=stored.metadata.lap_time_seconds,
                clock_source=stored.clock_source,
                distance_m=list(prepared.distance_m),
                elapsed_seconds=list(prepared.elapsed_seconds),
                channels=channels,
                channel_valid_masks=masks,
                source_samples=NativeSamples(
                    session_time_ns=[stored.source.session_time_ns[i] for i in native_indices],
                    channels=native_channels,
                    channel_valid_masks=native_masks,
                    source_sensors=[stored.source_sensors[i] for i in native_indices],
                ),
                channel_availability={c: stored.channel_availability[c] for c in selected},
                context=context,
                flags=flags,
                start_covered=prepared.start_covered,
                finish_covered=prepared.finish_covered,
                distance_derived=prepared.distance_derived,
            )

        return self._cached(key, build)

    def _pair(
        self,
        session_id: str,
        driver_a: str,
        lap_a: int,
        driver_b: str,
        lap_b: int,
        pinned: str,
        settings: CompareSettings,
    ) -> tuple[Comparison, StoredLap, StoredLap]:
        aa, a = self._load(session_id, driver_a, lap_a, pinned, settings)
        bb, b = self._load(session_id, driver_b, lap_b, pinned, settings)
        return align_laps(a, b, settings=_alignment(settings)), aa, bb

    def compare(
        self,
        session_id: str,
        driver_a: str,
        lap_a: int,
        driver_b: str,
        lap_b: int,
        *,
        revision: str | None = None,
        settings: CompareSettings | None = None,
    ) -> ComparisonResponse:
        settings = settings or CompareSettings()
        selected = _channels(settings)
        pinned = self._pin(session_id, revision)
        key = (
            "compare",
            session_id,
            pinned,
            driver_a,
            str(lap_a),
            driver_b,
            str(lap_b),
            settings.model_dump_json(),
            ALIGNMENT_VERSION,
            CORNER_VERSION,
        )

        def build() -> ComparisonResponse:
            comparison, aa, bb = self._pair(
                session_id, driver_a, lap_a, driver_b, lap_b, pinned, settings
            )

            def side(driver: str, lap: int, aligned: Any) -> AlignedResponse:
                values, masks = _wire(aligned.channels, selected)
                return AlignedResponse(
                    driver=driver,
                    lap_number=lap,
                    elapsed_seconds=list(aligned.elapsed_seconds),
                    channels=values,
                    valid_mask=list(aligned.valid_mask),
                    channel_valid_masks=masks,
                )

            return ComparisonResponse(
                session_id=session_id,
                revision=pinned,
                algorithm_version=ALIGNMENT_VERSION,
                corner_version=CORNER_VERSION,
                settings=settings,
                distance_m=list(comparison.distance_m),
                a=side(driver_a, lap_a, comparison.a),
                b=side(driver_b, lap_b, comparison.b),
                delta_seconds=list(comparison.delta_seconds),
                valid_mask=list(comparison.valid_mask),
                end_delta_seconds=comparison.end_delta_seconds,
                observed_lap_delta_seconds=comparison.observed_lap_delta_seconds,
                finish_line_agreement_applicable=comparison.finish_line_agreement_applicable,
                finish_delta_error_seconds=comparison.finish_delta_error_seconds,
                delta_convention=comparison.delta_convention,
                flags=list(comparison.flags),
                availability_a={c: aa.channel_availability[c] for c in selected},
                availability_b={c: bb.channel_availability[c] for c in selected},
            )

        return self._cached(key, build)

    def corners(
        self,
        session_id: str,
        driver_a: str,
        lap_a: int,
        driver_b: str,
        lap_b: int,
        *,
        revision: str | None = None,
        settings: CompareSettings | None = None,
        half_width_m: float = 100.0,
        fallback_count: int = 10,
    ) -> CornersResponse:
        settings = settings or CompareSettings()
        _channels(settings)
        pinned = self._pin(session_id, revision)
        key = (
            "corners",
            session_id,
            pinned,
            driver_a,
            str(lap_a),
            driver_b,
            str(lap_b),
            settings.model_dump_json(),
            str(half_width_m),
            str(fallback_count),
            ALIGNMENT_VERSION,
            CORNER_VERSION,
        )

        def build() -> CornersResponse:
            comparison, _, _ = self._pair(
                session_id, driver_a, lap_a, driver_b, lap_b, pinned, settings
            )
            markers = self.reader.corners(session_id, revision=pinned)
            windows = build_windows(
                comparison,
                markers=markers,
                half_width_m=half_width_m,
                fallback_count=fallback_count,
            )
            items = analyze_windows(comparison, windows)
            return CornersResponse(
                session_id=session_id,
                revision=pinned,
                algorithm_version=CORNER_VERSION,
                items=[WindowComparisonResponse.model_validate(asdict(item)) for item in items],
                markers_available=bool(markers),
            )

        return self._cached(key, build)

    def microsectors(
        self,
        session_id: str,
        driver_a: str,
        lap_a: int,
        driver_b: str,
        lap_b: int,
        *,
        revision: str | None = None,
        settings: CompareSettings | None = None,
        count: int = 20,
    ) -> MicrosectorsResponse:
        settings = settings or CompareSettings()
        _channels(settings)
        pinned = self._pin(session_id, revision)
        key = (
            "microsectors",
            session_id,
            pinned,
            driver_a,
            str(lap_a),
            driver_b,
            str(lap_b),
            settings.model_dump_json(),
            str(count),
            ALIGNMENT_VERSION,
            CORNER_VERSION,
        )

        def build() -> MicrosectorsResponse:
            comparison, _, _ = self._pair(
                session_id, driver_a, lap_a, driver_b, lap_b, pinned, settings
            )
            return MicrosectorsResponse.model_validate(
                {
                    "session_id": session_id,
                    "revision": pinned,
                    **asdict(microsectors(comparison, count=count)),
                }
            )

        return self._cached(key, build)
