"""Async FastF1 3.8 adapter orchestration behind the provider-neutral boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

import fastf1  # type: ignore[import-untyped]

from downforce_core.domain.enums import SessionType
from downforce_core.exceptions import ProviderUnavailableError, SessionNotFoundError
from downforce_core.providers.base import (
    DatasetName,
    LoadOptions,
    ProviderCapabilities,
    ProviderSession,
    SessionRef,
)
from downforce_core.providers.fastf1_tables import extract_tables
from downforce_core.providers.fastf1_values import (
    clean_message,
    exception_detail,
    extract_metadata,
)

_FASTF1_SESSION_IDENTIFIERS: Mapping[SessionType, str] = {
    SessionType.PRACTICE_1: "FP1",
    SessionType.PRACTICE_2: "FP2",
    SessionType.PRACTICE_3: "FP3",
    SessionType.QUALIFYING: "Q",
    SessionType.SPRINT_SHOOTOUT: "SS",
    SessionType.SPRINT_QUALIFYING: "SQ",
    SessionType.SPRINT: "S",
    SessionType.RACE: "R",
}

_CAPABILITIES = ProviderCapabilities(
    drivers=True,
    laps=True,
    weather=True,
    race_control=True,
    race_positions=True,
    track_positions=True,
    car_telemetry=True,
    live=False,
)

# FastF1's cache path and force-renew switch are process globals. All adapter instances share this
# lock so that configuring one root cannot redirect another session while it is being loaded.
_FASTF1_CACHE_LOCK = Lock()
_ACTIVE_CACHE_PATH: str | None = None
_ACTIVE_FORCE_RENEW: bool | None = None


class _FastF1Session(Protocol):
    def load(
        self,
        *,
        laps: bool,
        telemetry: bool,
        weather: bool,
        messages: bool,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _LoadFlags:
    laps: bool
    telemetry: bool
    weather: bool
    messages: bool


class FastF1Provider:
    """Load FastF1 sessions without exposing FastF1-owned objects to core."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._cache_path = self._root / ".downforce" / "cache" / "fastf1"
        try:
            self._cache_path.mkdir(parents=True, exist_ok=True)
            with _FASTF1_CACHE_LOCK:
                self._configure_cache_locked(force_refresh=False)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"FastF1 cache initialization failed: {exception_detail(exc)}"
            ) from exc

    @property
    def name(self) -> str:
        return "fastf1"

    @property
    def version(self) -> str:
        return str(fastf1.__version__)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _CAPABILITIES

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    async def load_session(
        self,
        session: SessionRef,
        options: LoadOptions | None = None,
    ) -> ProviderSession:
        if not isinstance(session, SessionRef):
            raise TypeError("session must be a SessionRef")
        if options is None:
            options = LoadOptions()
        elif not isinstance(options, LoadOptions):
            raise TypeError("options must be LoadOptions or None")
        return await asyncio.to_thread(self._load_session_sync, session, options)

    def _configure_cache_locked(self, *, force_refresh: bool) -> None:
        global _ACTIVE_CACHE_PATH, _ACTIVE_FORCE_RENEW

        cache_path = str(self._cache_path)
        # Every explicit refresh must re-run FastF1's configuration so its HTTP cache is cleared.
        # Only the ordinary non-refresh case can be safely elided.
        if not force_refresh and _ACTIVE_CACHE_PATH == cache_path and _ACTIVE_FORCE_RENEW is False:
            return
        fastf1.Cache.enable_cache(cache_path, force_renew=force_refresh)
        _ACTIVE_CACHE_PATH = cache_path
        _ACTIVE_FORCE_RENEW = force_refresh

    def _load_session_sync(self, reference: SessionRef, options: LoadOptions) -> ProviderSession:
        # Hold through extraction: FastF1 properties may be lazy, and a second provider must not
        # change the process-global cache root until no provider-owned object remains in use.
        with _FASTF1_CACHE_LOCK:
            return self._load_session_locked(reference, options)

    def _load_session_locked(
        self,
        reference: SessionRef,
        options: LoadOptions,
    ) -> ProviderSession:
        flags = _load_flags(reference, options)
        try:
            self._configure_cache_locked(force_refresh=options.force_refresh)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"FastF1 cache configuration failed: {exception_detail(exc)}"
            ) from exc

        identifier = _FASTF1_SESSION_IDENTIFIERS[reference.session]
        try:
            provider_session = cast(
                _FastF1Session,
                fastf1.get_session(
                    reference.season,
                    reference.event,
                    identifier,
                    backend="fastf1",
                ),
            )
        except Exception as exc:
            if _is_missing_session_error(exc, value_error_is_missing=True):
                raise SessionNotFoundError(
                    _load_error_message(reference, identifier, "could not resolve", exc)
                ) from exc
            raise ProviderUnavailableError(
                _load_error_message(reference, identifier, "lookup failed", exc)
            ) from exc

        try:
            provider_session.load(
                laps=flags.laps,
                telemetry=flags.telemetry,
                weather=flags.weather,
                messages=flags.messages,
            )
        except Exception as exc:
            if _is_missing_session_error(exc):
                raise SessionNotFoundError(
                    _load_error_message(reference, identifier, "was not available", exc)
                ) from exc
            raise ProviderUnavailableError(
                _load_error_message(reference, identifier, "load failed", exc)
            ) from exc

        warnings: list[str] = []
        metadata, session_origin = extract_metadata(
            provider_session,
            reference,
            load_laps=flags.laps,
            load_telemetry=flags.telemetry,
            warnings=warnings,
        )
        api_supported = metadata.get("f1_api_support") is not False
        tables = extract_tables(
            provider_session,
            reference,
            options,
            api_supported=api_supported,
            session_origin=session_origin,
            warnings=warnings,
        )
        metadata["warnings"] = tuple(warnings)
        return ProviderSession(
            session=reference,
            provider_name=self.name,
            provider_version=self.version,
            retrieved_at=datetime.now(UTC),
            metadata=metadata,
            tables=tables,
        )


def _load_flags(reference: SessionRef, options: LoadOptions) -> _LoadFlags:
    requested = options.datasets
    laps = bool(
        requested
        & {
            DatasetName.LAPS,
            DatasetName.RACE_CONTROL,
            DatasetName.RACE_POSITIONS,
        }
    )
    # FastF1 3.8.3 computes ``Session.t0_date`` only in ``_load_telemetry``.
    # Race-control messages can contain absolute UTC timestamps, so serving that
    # dataset incurs an internal telemetry load even though telemetry/position
    # ProviderTables remain NOT_REQUESTED unless callers explicitly request them.
    telemetry = (
        DatasetName.RACE_CONTROL in requested
        or DatasetName.CAR_TELEMETRY in requested
        or (DatasetName.TRACK_POSITIONS in requested and reference.season >= 2020)
    )
    return _LoadFlags(
        laps=laps,
        telemetry=telemetry,
        weather=DatasetName.WEATHER in requested,
        # FastF1 applies race-control deletion/reinstatement messages after loading laps.
        # Every lap-backed view needs those corrections even when the independently
        # exposed race-control dataset was not requested.
        messages=laps,
    )


def _is_missing_session_error(
    error: BaseException,
    *,
    value_error_is_missing: bool = False,
) -> bool:
    return (value_error_is_missing and isinstance(error, ValueError)) or type(error).__name__ in {
        "InvalidSessionError",
        "SessionNotAvailableError",
    }


def _load_error_message(
    reference: SessionRef,
    identifier: str,
    action: str,
    error: BaseException,
) -> str:
    return clean_message(
        f"FastF1 {action} season {reference.season}, event {reference.event!r}, "
        f"session {identifier}: {exception_detail(error)}"
    )


__all__ = ["FastF1Provider"]
