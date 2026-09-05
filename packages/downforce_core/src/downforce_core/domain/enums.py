"""Controlled canonical vocabularies with explicit unknown fallbacks."""

from collections.abc import Mapping
from enum import StrEnum


def _normalized_token(raw: str) -> str:
    return " ".join(raw.strip().casefold().replace("_", " ").replace("-", " ").split())


def _parse_with_fallback[EnumT: StrEnum](
    raw: str | None, aliases: Mapping[str, EnumT], unknown: EnumT
) -> EnumT:
    if raw is None or not isinstance(raw, str):
        return unknown
    token = _normalized_token(raw)
    if not token:
        return unknown
    return aliases.get(token, unknown)


class TyreCompound(StrEnum):
    UNKNOWN = "unknown"
    SOFT = "soft"
    MEDIUM = "medium"
    HARD = "hard"
    INTERMEDIATE = "intermediate"
    WET = "wet"

    @classmethod
    def from_raw(cls, raw: str | None) -> "TyreCompound":
        return _parse_with_fallback(raw, _TYRE_COMPOUND_ALIASES, cls.UNKNOWN)


class DriverStatus(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    FINISHED = "finished"
    RETIRED = "retired"
    DISQUALIFIED = "disqualified"
    DID_NOT_START = "did-not-start"
    DID_NOT_FINISH = "did-not-finish"
    NOT_CLASSIFIED = "not-classified"

    @classmethod
    def from_raw(cls, raw: str | None) -> "DriverStatus":
        return _parse_with_fallback(raw, _DRIVER_STATUS_ALIASES, cls.UNKNOWN)


class TrackStatus(StrEnum):
    UNKNOWN = "unknown"
    CLEAR = "clear"
    YELLOW = "yellow"
    SAFETY_CAR = "safety-car"
    RED_FLAG = "red-flag"
    VIRTUAL_SAFETY_CAR = "virtual-safety-car"
    VSC_ENDING = "vsc-ending"

    @classmethod
    def from_raw(cls, raw: str | None) -> "TrackStatus":
        return _parse_with_fallback(raw, _TRACK_STATUS_ALIASES, cls.UNKNOWN)


class SessionType(StrEnum):
    UNKNOWN = "unknown"
    PRACTICE_1 = "practice-1"
    PRACTICE_2 = "practice-2"
    PRACTICE_3 = "practice-3"
    QUALIFYING = "qualifying"
    SPRINT_SHOOTOUT = "sprint-shootout"
    SPRINT_QUALIFYING = "sprint-qualifying"
    SPRINT = "sprint"
    RACE = "race"

    @classmethod
    def from_raw(cls, raw: str | None) -> "SessionType":
        return _parse_with_fallback(raw, _SESSION_TYPE_ALIASES, cls.UNKNOWN)


class DataQuality(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    INVALID = "invalid"

    @classmethod
    def from_raw(cls, raw: str | None) -> "DataQuality":
        return _parse_with_fallback(raw, _DATA_QUALITY_ALIASES, cls.UNKNOWN)


_TYRE_COMPOUND_ALIASES: Mapping[str, TyreCompound] = {
    member.value: member for member in TyreCompound
}

_DRIVER_STATUS_ALIASES: Mapping[str, DriverStatus] = {
    "running": DriverStatus.RUNNING,
    "active": DriverStatus.RUNNING,
    "finished": DriverStatus.FINISHED,
    "classified": DriverStatus.FINISHED,
    "retired": DriverStatus.RETIRED,
    "disqualified": DriverStatus.DISQUALIFIED,
    "dsq": DriverStatus.DISQUALIFIED,
    "did not start": DriverStatus.DID_NOT_START,
    "dns": DriverStatus.DID_NOT_START,
    "did not finish": DriverStatus.DID_NOT_FINISH,
    "dnf": DriverStatus.DID_NOT_FINISH,
    "not classified": DriverStatus.NOT_CLASSIFIED,
    "nc": DriverStatus.NOT_CLASSIFIED,
}

_TRACK_STATUS_ALIASES: Mapping[str, TrackStatus] = {
    "1": TrackStatus.CLEAR,
    "all clear": TrackStatus.CLEAR,
    "clear": TrackStatus.CLEAR,
    "2": TrackStatus.YELLOW,
    "yellow": TrackStatus.YELLOW,
    "yellow flag": TrackStatus.YELLOW,
    "4": TrackStatus.SAFETY_CAR,
    "safety car": TrackStatus.SAFETY_CAR,
    "sc": TrackStatus.SAFETY_CAR,
    "5": TrackStatus.RED_FLAG,
    "red": TrackStatus.RED_FLAG,
    "red flag": TrackStatus.RED_FLAG,
    "6": TrackStatus.VIRTUAL_SAFETY_CAR,
    "virtual safety car": TrackStatus.VIRTUAL_SAFETY_CAR,
    "vsc": TrackStatus.VIRTUAL_SAFETY_CAR,
    "7": TrackStatus.VSC_ENDING,
    "vsc ending": TrackStatus.VSC_ENDING,
}

_SESSION_TYPE_ALIASES: Mapping[str, SessionType] = {
    "fp1": SessionType.PRACTICE_1,
    "p1": SessionType.PRACTICE_1,
    "practice 1": SessionType.PRACTICE_1,
    "free practice 1": SessionType.PRACTICE_1,
    "fp2": SessionType.PRACTICE_2,
    "p2": SessionType.PRACTICE_2,
    "practice 2": SessionType.PRACTICE_2,
    "free practice 2": SessionType.PRACTICE_2,
    "fp3": SessionType.PRACTICE_3,
    "p3": SessionType.PRACTICE_3,
    "practice 3": SessionType.PRACTICE_3,
    "free practice 3": SessionType.PRACTICE_3,
    "q": SessionType.QUALIFYING,
    "qualifying": SessionType.QUALIFYING,
    "ss": SessionType.SPRINT_SHOOTOUT,
    "sprint shootout": SessionType.SPRINT_SHOOTOUT,
    "sq": SessionType.SPRINT_QUALIFYING,
    "sprint qualifying": SessionType.SPRINT_QUALIFYING,
    "s": SessionType.SPRINT,
    "sprint": SessionType.SPRINT,
    "r": SessionType.RACE,
    "race": SessionType.RACE,
}

_DATA_QUALITY_ALIASES: Mapping[str, DataQuality] = {member.value: member for member in DataQuality}

__all__ = ["DataQuality", "DriverStatus", "SessionType", "TrackStatus", "TyreCompound"]
