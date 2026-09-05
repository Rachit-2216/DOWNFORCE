"""Pure orchestration from an owned provider snapshot to canonical records."""

from __future__ import annotations

from dataclasses import replace

from downforce_core.exceptions import NormalizationError, SessionDataIncompleteError
from downforce_core.normalization.laps import derive_pit_stops, derive_stints, normalize_laps
from downforce_core.normalization.metadata import normalize_drivers, normalize_metadata
from downforce_core.normalization.models import NormalizedSession, ValidationReport
from downforce_core.normalization.observations import normalize_race_control, normalize_weather
from downforce_core.normalization.positions import (
    normalize_race_positions,
    normalize_telemetry_index,
    normalize_track_positions,
)
from downforce_core.normalization.validation import validate_normalized_session
from downforce_core.providers.base import (
    DatasetAvailability,
    DatasetName,
    ProviderCapabilities,
    ProviderSession,
)


def _capabilities(session: ProviderSession) -> ProviderCapabilities:
    supported = {
        name: session.table(name).availability is not DatasetAvailability.UNSUPPORTED
        for name in DatasetName
    }
    return ProviderCapabilities(
        drivers=supported[DatasetName.DRIVERS],
        laps=supported[DatasetName.LAPS],
        weather=supported[DatasetName.WEATHER],
        race_control=supported[DatasetName.RACE_CONTROL],
        race_positions=supported[DatasetName.RACE_POSITIONS],
        track_positions=supported[DatasetName.TRACK_POSITIONS],
        car_telemetry=supported[DatasetName.CAR_TELEMETRY],
        live=False,
    )


def _check_required_provider_tables(session: ProviderSession) -> None:
    missing = [
        name.value
        for name in (DatasetName.DRIVERS, DatasetName.LAPS)
        if session.table(name).availability is not DatasetAvailability.AVAILABLE
    ]
    if missing:
        states = ", ".join(
            f"{name}={session.table(DatasetName(name)).availability.value}" for name in missing
        )
        raise SessionDataIncompleteError(f"required provider datasets are incomplete: {states}")


def _warning_text(report: ValidationReport) -> tuple[str, ...]:
    warnings: list[str] = []
    for issue in report.warnings:
        row = f",row={issue.row_key}" if issue.row_key is not None else ""
        warnings.append(f"validation.{issue.code}: table={issue.table}{row}: {issue.message}")
    return tuple(warnings)


def normalize_session(session: ProviderSession) -> NormalizedSession:
    """Normalize one immutable provider session or raise only typed normalization failures."""

    if not isinstance(session, ProviderSession):
        raise TypeError("session must be a ProviderSession")
    _check_required_provider_tables(session)
    raw_provider_warnings = session.metadata.get("warnings", ())
    warnings: list[str] = []
    if isinstance(raw_provider_warnings, (list, tuple)):
        warnings.extend(
            f"provider.warning: {warning}"
            for warning in raw_provider_warnings
            if isinstance(warning, str) and warning.strip()
        )
    try:
        metadata = normalize_metadata(session)
        drivers, classifications, driver_ids = normalize_drivers(session, metadata, warnings)
        laps, pit_observations = normalize_laps(
            session,
            metadata,
            driver_ids,
            warnings,
        )
        stints = derive_stints(session, laps, driver_ids, warnings)
        pit_stops = derive_pit_stops(
            session,
            metadata,
            pit_observations,
            driver_ids,
            warnings,
        )
        weather = normalize_weather(session, metadata, warnings)
        race_control = normalize_race_control(
            session,
            metadata,
            driver_ids,
            warnings,
        )
        race_positions = normalize_race_positions(
            session,
            metadata,
            driver_ids,
            warnings,
        )
        track_positions = normalize_track_positions(
            session,
            metadata,
            driver_ids,
            warnings,
        )
        telemetry_index = normalize_telemetry_index(
            session,
            metadata,
            driver_ids,
            warnings,
        )
        normalized = NormalizedSession(
            metadata=metadata,
            drivers=drivers,
            classifications=classifications,
            laps=laps,
            stints=stints,
            pit_stops=pit_stops,
            weather=weather,
            race_control=race_control,
            race_positions=race_positions,
            track_positions=track_positions,
            telemetry_index=telemetry_index,
            capabilities=_capabilities(session),
            completeness={name: session.table(name).availability for name in DatasetName},
            warnings=tuple(warnings),
            provider_name=session.provider_name,
            provider_version=session.provider_version,
            retrieved_at=session.retrieved_at,
            provider_metadata=session.metadata,
            requested_session=session.session,
        )
    except (TypeError, ValueError, KeyError) as error:
        raise NormalizationError(f"provider data cannot be normalized: {error}") from error

    report = validate_normalized_session(normalized)
    if report.errors:
        summary = "; ".join(
            f"{issue.table}.{issue.code}: {issue.message}" for issue in report.errors
        )
        incomplete_codes = {"missing-drivers", "missing-laps"}
        if any(issue.code in incomplete_codes for issue in report.errors):
            raise SessionDataIncompleteError(summary)
        raise NormalizationError(summary)
    return replace(
        normalized,
        warnings=normalized.warnings + _warning_text(report),
        validation_report=report,
    )


normalize_provider_session = normalize_session


__all__ = ["normalize_provider_session", "normalize_session"]
