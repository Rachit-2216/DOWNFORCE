"""Provider-backed modern session discovery with evidence-aware completion states."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from downforce_core.domain.enums import SessionType

Completion = Literal["future", "past-unverified", "unknown", "completed", "cancelled"]


def canonical_session_type(year: int, provider_label: str) -> str:
    token = " ".join(provider_label.lower().replace("_", " ").split())
    if year == 2021 and token == "sprint qualifying":
        return SessionType.SPRINT.value
    if year == 2023 and token == "sprint qualifying":
        # OpenF1 uses this label for the 2023 standalone Sprint Shootout.
        # Preserve the original provider label separately; do not confuse it
        # with the 2021 sprint race or rewrite canonical historical identities.
        return SessionType.SPRINT_SHOOTOUT.value
    return SessionType.from_raw(provider_label).value


def _timestamp(value: object) -> datetime | None:
    if value is None or str(value) in ("", "NaT", "nan", "None"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Provider *DateUtc fields are explicitly UTC even when timezone-naive.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class DetailedSession:
    session_id: str
    event_id: str
    season: int
    round_number: int
    event_name: str
    country: str
    circuit_name: str
    session_type: str
    provider_session_name: str
    scheduled_at: str | None
    completion: Completion
    fastf1_slot: int
    fastf1_supported: bool | None
    provider: str = "fastf1"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def discover_schedule(
    season: int, rows: Iterable[Mapping[str, object]], *, now: datetime
) -> list[DetailedSession]:
    """Read actual provider slots. Past schedule entries are NOT proof of completion."""
    if now.tzinfo is None:
        raise ValueError("discovery clock must be timezone aware")
    if season < 2018 or season > now.year + 1:
        raise ValueError("season is outside the detailed discovery window")
    found: list[DetailedSession] = []
    identities: set[str] = set()
    for row in rows:
        raw_round = row.get("RoundNumber", 0)
        try:
            round_number = int(str(raw_round))
        except ValueError as exc:
            raise ValueError("invalid provider round") from exc
        if round_number == 0:  # pre-season testing is not a championship weekend
            continue
        if not 1 <= round_number <= 99:
            raise ValueError("invalid provider round")
        for slot in range(1, 6):
            raw_label = row.get(f"Session{slot}")
            if not isinstance(raw_label, str) or not raw_label.strip():
                continue
            label = raw_label.strip()
            category = canonical_session_type(season, label)
            event_id = f"{season}-{round_number:02d}"
            suffix = category if category != "unknown" else f"slot-{slot}"
            identity = f"{event_id}-{suffix}"
            if identity in identities:
                raise ValueError(f"duplicate detailed session identity: {identity}")
            identities.add(identity)
            scheduled = _timestamp(row.get(f"Session{slot}DateUtc"))
            completion: Completion = "unknown"
            if scheduled is not None:
                completion = "future" if scheduled >= now else "past-unverified"
            support = row.get("F1ApiSupport", row.get("FastF1Support"))
            found.append(
                DetailedSession(
                    session_id=identity,
                    event_id=event_id,
                    season=season,
                    round_number=round_number,
                    event_name=str(row.get("EventName", "")),
                    country=str(row.get("Country", "")),
                    circuit_name=str(row.get("Location", "")),
                    session_type=category,
                    provider_session_name=label,
                    scheduled_at=scheduled.isoformat() if scheduled else None,
                    completion=completion,
                    fastf1_slot=slot,
                    fastf1_supported=support if isinstance(support, bool) else None,
                )
            )
    return found
