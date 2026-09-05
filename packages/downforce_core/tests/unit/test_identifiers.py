import unicodedata
from hashlib import sha256

import pytest
from downforce_core.domain.enums import SessionType
from downforce_core.domain.identifiers import (
    DriverId,
    SessionId,
    is_driver_id_for_session,
    make_driver_id,
    make_session_id,
    slugify,
    validate_safe_identifier,
)
from downforce_core.providers.base import SessionRef


def _expected_slug(source: str, display: str) -> str:
    normalized = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", source).casefold())
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return f"{display}-sha256-{digest}"


def test_slug_and_identifiers_are_deterministic_and_session_scoped() -> None:
    event_slug = _expected_slug("British Grand Prix", "british-grand-prix")
    assert slugify("British Grand Prix") == event_slug
    assert slugify("São Paulo") == _expected_slug("São Paulo", "sao-paulo")

    session_id = make_session_id(2024, "British Grand Prix", SessionType.RACE)
    assert session_id == SessionId(f"session-2024-event-{event_slug}-type-race")
    assert session_id.season == 2024
    assert session_id.event_selector == f"event-{event_slug}"
    assert session_id.session_type is SessionType.RACE
    assert make_session_id(2024, "British Grand Prix", SessionType.RACE) == session_id

    driver_id = make_driver_id(session_id, "VER")
    assert driver_id.startswith("driver-")
    assert driver_id.endswith(f"-key-{_expected_slug('VER', 'ver')}")
    assert is_driver_id_for_session(driver_id, session_id)
    assert make_driver_id(session_id, "VER") == driver_id
    assert (
        make_driver_id(make_session_id(2024, "Monaco Grand Prix", SessionType.RACE), "VER")
        != driver_id
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "",
        ".",
        "..",
        "../race",
        r"race\driver",
        "race/driver",
        "%2e%2e%2frace",
        " race",
        "race ",
        "RACE",
        "race--driver",
        "race.driver",
        "con",
    ],
)
def test_one_validator_rejects_unsafe_or_alias_prone_canonical_ids(unsafe: str) -> None:
    with pytest.raises(ValueError):
        validate_safe_identifier(unsafe)
    with pytest.raises(ValueError):
        SessionId(unsafe)
    with pytest.raises(ValueError):
        DriverId(unsafe)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("British  Grand Prix", "British Grand Prix"),
        ("British--Grand-Prix", "British-Grand-Prix"),
        ("British - Grand Prix", "British-Grand Prix"),
        ("British.GP (Race) #1", "British GP Race 1"),
    ],
)
def test_human_punctuation_and_repeated_separators_are_accepted_without_collisions(
    left: str, right: str
) -> None:
    left_slug = slugify(left)
    assert validate_safe_identifier(left_slug) == left_slug
    assert left_slug != slugify(right)


@pytest.mark.parametrize("raw", ["", " race", "race ", "race\x00name", "race\nname", "\u200b"])
def test_human_identifier_sources_reject_empty_surrounding_whitespace_and_controls(
    raw: str,
) -> None:
    with pytest.raises(ValueError):
        slugify(raw)


def test_human_identifier_source_accepts_bounded_trimmed_unicode_and_punctuation() -> None:
    raw = "S\u00e3o Paulo \u2014 GP \u21161"
    slug = slugify(raw)
    maximum_length_slug = slugify("A" * 4_096)

    assert validate_safe_identifier(slug) == slug
    assert validate_safe_identifier(maximum_length_slug) == maximum_length_slug
    with pytest.raises(ValueError, match="at most 4096"):
        slugify("A" * 4_097)


@pytest.mark.parametrize("raw", ["../British.GP", "race/weekend", r"race\weekend", "event: #1"])
def test_path_like_human_sources_generate_only_safe_storage_identifiers(raw: str) -> None:
    slug = slugify(raw)
    session_id = make_session_id(2024, raw, SessionType.RACE)
    driver_id = make_driver_id(make_session_id(2024, 1, SessionType.RACE), raw)

    assert validate_safe_identifier(slug) == slug
    assert validate_safe_identifier(session_id) == session_id
    assert validate_safe_identifier(driver_id) == driver_id
    assert all(token not in slug for token in ("/", "\\", ".", ".."))


@pytest.mark.parametrize(("left", "right"), [("A B", "A-B"), ("ab", "a中b")])
def test_distinct_accepted_sources_cannot_collide_at_any_identifier_level(
    left: str, right: str
) -> None:
    left_slug = slugify(left)
    right_slug = slugify(right)
    assert left_slug != right_slug
    assert slugify(left) == left_slug
    assert slugify(right) == right_slug

    left_session = make_session_id(2024, left, SessionType.RACE)
    right_session = make_session_id(2024, right, SessionType.RACE)
    assert left_session != right_session
    assert make_session_id(2024, left, SessionType.RACE) == left_session
    assert make_session_id(2024, right, SessionType.RACE) == right_session

    scope = make_session_id(2024, 1, SessionType.RACE)
    left_driver = make_driver_id(scope, left)
    right_driver = make_driver_id(scope, right)
    assert left_driver != right_driver
    assert make_driver_id(scope, left) == left_driver
    assert make_driver_id(scope, right) == right_driver
    assert is_driver_id_for_session(left_driver, scope)
    assert is_driver_id_for_session(right_driver, scope)


def test_slug_intentionally_canonicalizes_case_and_unicode_normalization() -> None:
    assert slugify("São Paulo") == slugify("SÃO PAULO")


def test_long_sources_keep_full_digest_while_all_identifiers_remain_bounded() -> None:
    source = "A" * 1_000
    normalized = unicodedata.normalize("NFKC", source).casefold()
    digest = sha256(normalized.encode("utf-8")).hexdigest()

    slug = slugify(source)
    session_id = make_session_id(2024, source, SessionType.RACE)
    driver_id = make_driver_id(make_session_id(2024, 1, SessionType.RACE), source)

    assert slug.endswith(f"-sha256-{digest}")
    assert len(digest) == 64
    assert len(slug) <= 164
    assert len(session_id) <= 240
    assert len(driver_id) <= 240
    assert digest in session_id
    assert digest in driver_id
    assert slugify(source) == slug


def test_session_ref_validates_and_normalizes_stable_session_aliases() -> None:
    short = SessionRef(season=2024, event="British Grand Prix", session="R")
    long = SessionRef(season=2024, event="British Grand Prix", session="Race")

    assert short.session_type is SessionType.RACE
    assert short.session is SessionType.RACE
    assert short.session_id == long.session_id
    assert short == long
    assert hash(short) == hash(long)
    event_slug = _expected_slug("British Grand Prix", "british-grand-prix")
    assert short.session_id == SessionId(f"session-2024-event-{event_slug}-type-race")

    round_ref = SessionRef(season=2024, event=12, session="Race")
    assert round_ref.session_id == SessionId("session-2024-round-12-type-race")
    named_round_ref = SessionRef(2024, "Round 12", "R")
    assert named_round_ref.session_id != round_ref.session_id
    assert named_round_ref != round_ref
    assert len({named_round_ref, round_ref}) == 2


@pytest.mark.parametrize(
    ("left_event", "right_event"),
    [
        ("British Grand Prix", "BRITISH GRAND PRIX"),
        ("S\u00e3o Paulo", "SA\u0303O PAULO"),
    ],
)
def test_session_ref_identity_uses_canonical_event_id_but_retains_provider_spelling(
    left_event: str, right_event: str
) -> None:
    left = SessionRef(2024, left_event, "R")
    right = SessionRef(2024, right_event, "Race")

    assert left.event == left_event
    assert right.event == right_event
    assert left.session_id == right.session_id
    assert left == right
    assert hash(left) == hash(right)
    assert len({left, right}) == 1


def test_session_ref_different_named_event_ids_remain_distinct_cache_keys() -> None:
    british = SessionRef(2024, "British Grand Prix", "Race")
    monaco = SessionRef(2024, "Monaco Grand Prix", "Race")

    assert british.session_id != monaco.session_id
    assert british != monaco
    assert len({british, monaco}) == 2


def test_numeric_driver_zero_and_string_zero_have_explicit_noncolliding_semantics() -> None:
    session_id = make_session_id(1950, 1, SessionType.RACE)
    numeric = make_driver_id(session_id, 0)
    string = make_driver_id(session_id, "0")
    leading_zero_string = make_driver_id(session_id, "00")

    assert len({numeric, string, leading_zero_string}) == 3
    assert "-number-0" in numeric
    assert "-key-0-sha256-" in string
    assert "-key-00-sha256-" in leading_zero_string
    with pytest.raises(ValueError, match="nonnegative"):
        make_driver_id(session_id, -1)


def test_identifier_types_reject_safe_but_semantically_invalid_values() -> None:
    with pytest.raises(ValueError):
        SessionId("race")
    with pytest.raises(ValueError):
        SessionId("session-2024-race")
    with pytest.raises(ValueError):
        SessionId("session-2024-event-british-grand-prix-type-race")
    with pytest.raises(ValueError):
        SessionId("session-2024-round-01-type-race")
    with pytest.raises(ValueError):
        DriverId("session-2024-round-12-type-race")


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        ("FP1", SessionType.PRACTICE_1),
        ("Practice 2", SessionType.PRACTICE_2),
        ("Q", SessionType.QUALIFYING),
        ("Sprint Qualifying", SessionType.SPRINT_QUALIFYING),
        ("S", SessionType.SPRINT),
        ("R", SessionType.RACE),
    ],
)
def test_session_ref_accepts_supported_codes_and_names(session: str, expected: SessionType) -> None:
    assert SessionRef(2024, 1, session).session_type is expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"season": 1949, "event": 1, "session": "R"},
        {"season": 10_000, "event": 1, "session": "R"},
        {"season": True, "event": 1, "session": "R"},
        {"season": 2024, "event": 0, "session": "R"},
        {"season": 2024, "event": True, "session": "R"},
        {"season": 2024, "event": 1.5, "session": "R"},
        {"season": 2024, "event": "", "session": "R"},
        {"season": 2024, "event": 1, "session": "Warmup"},
        {"season": 2024, "event": 1, "session": ""},
        {"season": 2024, "event": 1, "session": " Race"},
    ],
)
def test_session_ref_rejects_invalid_season_event_and_session(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        SessionRef(**kwargs)  # type: ignore[arg-type]
