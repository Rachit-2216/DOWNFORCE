"""Deterministic, path-safe canonical identifiers."""

import re
import unicodedata
from hashlib import sha256

from downforce_core.domain.enums import SessionType

_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENCODED_PATH_TOKEN = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_MAX_IDENTIFIER_LENGTH = 240
_MAX_SLUG_SOURCE_LENGTH = 4_096
_SHA256_HEX_LENGTH = 64
_HASHED_SLUG_SUFFIX_LENGTH = len("-sha256-") + _SHA256_HEX_LENGTH
# A string-key driver ID is the longest container for a slug:
# ``driver-`` + 64-character session hash + ``-key-`` + slug.
_MAX_SLUG_LENGTH = _MAX_IDENTIFIER_LENGTH - len("driver-") - _SHA256_HEX_LENGTH - len("-key-")
_MAX_SLUG_DISPLAY_LENGTH = _MAX_SLUG_LENGTH - _HASHED_SLUG_SUFFIX_LENGTH
_SAFE_SEGMENTS_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_HASHED_SLUG_PATTERN = rf"{_SAFE_SEGMENTS_PATTERN}-sha256-[0-9a-f]{{{_SHA256_HEX_LENGTH}}}"
_HASHED_SLUG = re.compile(rf"^{_HASHED_SLUG_PATTERN}$")
_SESSION_TYPE_VALUES = frozenset(
    session_type.value for session_type in SessionType if session_type is not SessionType.UNKNOWN
)
_DRIVER_ID = re.compile(
    rf"^driver-(?P<scope>[0-9a-f]{{{_SHA256_HEX_LENGTH}}})-"
    rf"(?:number-(?P<number>0|[1-9][0-9]*)|key-(?P<key>{_HASHED_SLUG_PATTERN}))$"
)


def validate_safe_identifier(value: str, *, field_name: str = "identifier") -> str:
    """Return a canonical identifier or reject unsafe/collision-prone aliases.

    Canonical IDs use lowercase ASCII alphanumeric segments separated by one hyphen.
    Rejecting alternate spellings (case changes, repeated separators and encoded path
    tokens) keeps a single filesystem-safe representation for each accepted ID.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or len(value) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} must contain 1-{_MAX_IDENTIFIER_LENGTH} characters")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if _ENCODED_PATH_TOKEN.search(value) or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{field_name} must not contain traversal or path syntax")
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{field_name} must use lowercase ASCII alphanumeric segments separated by one hyphen"
        )
    if value in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{field_name} is reserved by the filesystem")
    return value


def _normalize_slug_source(value: str) -> str:
    """Apply the intentional slug equivalence: Unicode NFKC plus case-folding."""

    normalized = unicodedata.normalize("NFKC", value)
    return unicodedata.normalize("NFKC", normalized.casefold())


def _validate_slug_source(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be nonempty without surrounding whitespace")
    if len(value) > _MAX_SLUG_SOURCE_LENGTH:
        raise ValueError(
            f"{field_name} must contain at most {_MAX_SLUG_SOURCE_LENGTH} source characters"
        )
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValueError(f"{field_name} must not contain control or formatting characters")
    normalized = _normalize_slug_source(value)
    if not normalized or len(normalized) > _MAX_SLUG_SOURCE_LENGTH:
        raise ValueError(
            f"{field_name} must contain 1-{_MAX_SLUG_SOURCE_LENGTH} normalized characters"
        )
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in normalized):
        raise ValueError(f"{field_name} must not normalize to control or formatting characters")
    return normalized


def slugify(value: str, *, field_name: str = "value") -> str:
    """Return a readable, collision-resistant slug for a validated source string.

    Unicode NFKC and case-folding intentionally define equivalent source spellings. The
    display prefix is ASCII and may be shortened, but identity comes from the complete
    SHA-256 digest of that normalized source. Distinct accepted separators and Unicode
    characters therefore remain distinct even when their display prefixes look alike.
    """

    normalized = _validate_slug_source(value, field_name=field_name)
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    ascii_value = (
        unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    )
    display = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
    display = display[:_MAX_SLUG_DISPLAY_LENGTH].rstrip("-") or "value"
    slug = f"{display}-sha256-{digest}"
    return validate_safe_identifier(slug, field_name=field_name)


class SessionId(str):
    """A validated canonical session identifier."""

    __slots__ = ()

    def __new__(cls, value: str) -> "SessionId":
        validated = validate_safe_identifier(value, field_name="session_id")
        prefix, separator, session_type = validated.rpartition("-type-")
        parts = prefix.split("-", 2)
        if (
            not separator
            or session_type not in _SESSION_TYPE_VALUES
            or len(parts) != 3
            or parts[0] != "session"
            or not parts[1].isdigit()
        ):
            raise ValueError("session_id must contain the session prefix, season, event and type")
        season = int(parts[1])
        if not 1950 <= season <= 9999:
            raise ValueError("session_id season must be between 1950 and 9999")
        event_selector = parts[2]
        if event_selector.startswith("round-"):
            round_value = event_selector.removeprefix("round-")
            if (
                not round_value.isdigit()
                or str(int(round_value)) != round_value
                or int(round_value) < 1
            ):
                raise ValueError("session_id round selector must be a canonical positive integer")
        elif event_selector.startswith("event-"):
            event_slug = event_selector.removeprefix("event-")
            if len(event_slug) > _MAX_SLUG_LENGTH or _HASHED_SLUG.fullmatch(event_slug) is None:
                raise ValueError("session_id named event selector must contain a hashed slug")
        else:
            raise ValueError("session_id must contain a round- or event-qualified selector")
        return str.__new__(cls, validated)

    @property
    def season(self) -> int:
        """Return the season encoded in this canonical ID."""

        return int(self.split("-", 2)[1])

    @property
    def event_selector(self) -> str:
        """Return the canonical round- or event-qualified selector."""

        prefix, _, _ = self.rpartition("-type-")
        return prefix.split("-", 2)[2]

    @property
    def session_type(self) -> SessionType:
        """Return the canonical session type encoded in this ID."""

        _, _, session_type = self.rpartition("-type-")
        return SessionType(session_type)


class DriverId(str):
    """A validated driver identifier whose hash prefix scopes it to one session."""

    __slots__ = ()

    def __new__(cls, value: str) -> "DriverId":
        validated = validate_safe_identifier(value, field_name="driver_id")
        if _DRIVER_ID.fullmatch(validated) is None:
            raise ValueError(
                "driver_id must contain a SHA-256 session scope and typed canonical driver key"
            )
        return str.__new__(cls, validated)


def _driver_scope(session_id: SessionId) -> str:
    return sha256(str(session_id).encode("utf-8")).hexdigest()


def is_driver_id_for_session(driver_id: DriverId, session_id: SessionId) -> bool:
    """Return whether a validated driver ID carries the supplied session scope."""

    if not isinstance(driver_id, DriverId):
        raise TypeError("driver_id must be a DriverId")
    if not isinstance(session_id, SessionId):
        raise TypeError("session_id must be a SessionId")
    return driver_id.startswith(f"driver-{_driver_scope(session_id)}-")


def make_session_id(season: int, event: int | str, session_type: SessionType) -> SessionId:
    """Build a stable session ID from a season, event selector and canonical type."""

    if isinstance(season, bool) or not isinstance(season, int):
        raise TypeError("season must be an integer")
    if not 1950 <= season <= 9999:
        raise ValueError("season must be between 1950 and 9999")
    if isinstance(event, bool):
        raise TypeError("event must be a positive round number or event name")
    if isinstance(event, int):
        if event < 1:
            raise ValueError("event round number must be positive")
        event_slug = f"round-{event}"
    elif isinstance(event, str):
        event_slug = f"event-{slugify(event, field_name='event')}"
    else:
        raise TypeError("event must be a positive round number or event name")
    if not isinstance(session_type, SessionType):
        raise TypeError("session_type must be a SessionType")
    if session_type is SessionType.UNKNOWN:
        raise ValueError("session_type must be a recognized session")
    return SessionId(f"session-{season}-{event_slug}-type-{session_type.value}")


def make_driver_id(session_id: SessionId, driver_key: int | str) -> DriverId:
    """Build a stable driver ID scoped to one canonical session.

    Integer keys use a canonical ``number-N`` representation without leading zeroes. String
    keys, including ``"0"`` and ``"00"``, use the separate hashed ``key-`` representation.
    """

    if not isinstance(session_id, SessionId):
        raise TypeError("session_id must be a SessionId")
    if isinstance(driver_key, bool):
        raise TypeError("driver_key must be a nonnegative number or driver code")
    if isinstance(driver_key, int):
        if driver_key < 0:
            raise ValueError("driver number must be nonnegative")
        driver_slug = f"number-{driver_key}"
    elif isinstance(driver_key, str):
        driver_slug = f"key-{slugify(driver_key, field_name='driver_key')}"
    else:
        raise TypeError("driver_key must be a nonnegative number or driver code")
    return DriverId(f"driver-{_driver_scope(session_id)}-{driver_slug}")


__all__ = [
    "DriverId",
    "SessionId",
    "is_driver_id_for_session",
    "make_driver_id",
    "make_session_id",
    "slugify",
    "validate_safe_identifier",
]
