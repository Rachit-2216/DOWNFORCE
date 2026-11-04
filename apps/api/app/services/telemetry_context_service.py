"""Read-only verification of destinations owned by the legacy replay and archive stores."""

import re
from typing import Any

from downforce_core.archive import HistoricalArchiveStore
from downforce_core.archive.contracts import ArchiveEventStatus, QualityStatus, SyncLifecycle
from downforce_core.archive.schemas import ArchiveTableName
from downforce_core.detailed.storage import DetailedStore
from downforce_core.domain.enums import SessionType
from downforce_core.exceptions import DownforceError
from downforce_core.providers import SessionRef
from downforce_core.replay import ReplayEngine
from downforce_core.storage import DownforceRepository

from app.schemas.telemetry import TelemetryContext

CONTEXT_ERRORS = (
    DownforceError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
)


class TelemetryContextService:
    """No aliases, snapshots, provider calls or derived datasets are created here."""

    def __init__(self, repository: DownforceRepository, archive: HistoricalArchiveStore) -> None:
        self.repository = repository
        self.archive = archive

    def resolve(self, store: DetailedStore, session_id: str, revision: str) -> TelemetryContext:
        result = TelemetryContext()
        try:
            metadata = store.manifest(session_id, revision=revision)["metadata"]["session"]
            if not isinstance(metadata, dict):
                return result
            season, round_number = metadata.get("season"), metadata.get("round_number")
            session_type = SessionType.from_raw(metadata.get("session_type"))
            if (
                type(season) is not int
                or type(round_number) is not int
                or not 1950 <= season <= 9999
                or not 1 <= round_number <= 99
                or session_type is SessionType.UNKNOWN
            ):
                return result
            candidate = str(SessionRef(season, round_number, session_type).session_id)
        except CONTEXT_ERRORS:
            return result
        result.replay_unavailable_reason = "No compatible replay is stored for this session."
        result.race_analytics_unavailable_reason = (
            "No validated completed race analytics are stored for this event."
        )
        self._replay(result, candidate, season, round_number, session_type)
        self._analytics(result, season, round_number)
        return result

    def _replay(
        self,
        result: TelemetryContext,
        candidate: str,
        season: int,
        round_number: int,
        session_type: SessionType,
    ) -> None:
        try:
            manifest = self.repository.load_manifest(candidate)
            if (
                manifest.session.get("season") != season
                or manifest.session.get("round_number") != round_number
                or manifest.session.get("session_type") != session_type.value
                or not manifest.replay_version
            ):
                return
            engine = ReplayEngine.from_repository(self.repository, manifest.session_id)
            session = self.repository.load_session(
                manifest.session_id, include_track_positions=False
            )
            if self.repository.load_manifest(candidate).dataset_id != manifest.dataset_id:
                return
            numbers = {
                driver.driver_id: str(driver.racing_number)
                for driver in session.drivers
                if driver.racing_number is not None
            }
            laps: dict[str, list[int]] = {number: [] for number in numbers.values()}
            for lap in session.laps:
                number = numbers.get(lap.driver_id)
                if (
                    number is not None
                    and lap.lap_end_time_ms is not None
                    and 0 <= lap.lap_end_time_ms <= engine.timeline.max_time_ms
                ):
                    laps[number].append(lap.lap_number)
            result.replay_session_id = manifest.session_id
            result.replay_driver_numbers = sorted(laps)
            result.replay_laps_by_driver = {
                number: sorted(set(values)) for number, values in laps.items()
            }
            result.replay_unavailable_reason = None
        except CONTEXT_ERRORS:
            return

    def _analytics(self, result: TelemetryContext, season: int, round_number: int) -> None:
        try:
            matches = [
                event
                for event in self.archive.load_catalog().events
                if event.season == season and event.round_number == round_number
            ]
            if len(matches) != 1:
                return
            event = matches[0]
            race = event.race_session
            if (
                event.status is not ArchiveEventStatus.COMPLETED
                or race.status is not ArchiveEventStatus.COMPLETED
                or race.session_type != "race"
                or race.sync_status not in {SyncLifecycle.COMPLETE, SyncLifecycle.PARTIAL}
                or race.quality.status not in {QualityStatus.VERIFIED, QualityStatus.GOOD}
                or not race.capabilities.results
                or race.row_counts.get("results", 0) < 1
                or race.data_revision is None
                or not re.fullmatch(r"archive-\d{4}-round-\d{2}-race", race.session_id)
            ):
                return
            manifest: dict[str, Any] = self.archive.load_manifest(race.session_id)
            quality, capabilities = manifest.get("quality"), manifest.get("capabilities")
            if (
                manifest.get("season") != season
                or manifest.get("round_number") != round_number
                or manifest.get("data_revision") != race.data_revision
                or not isinstance(quality, dict)
                or quality.get("status") not in {"verified", "good"}
                or not isinstance(capabilities, dict)
                or capabilities.get("results") is not True
                or not self.archive.revision_is_valid(race.session_id, race.data_revision)
            ):
                return
            results = self.archive.load_table(race.session_id, ArchiveTableName.RESULTS)
            if not results.num_rows or any(
                value != race.session_id for value in results["session_id"].to_pylist()
            ):
                return
            result.race_analytics_session_id = race.session_id
            result.race_analytics_unavailable_reason = None
        except CONTEXT_ERRORS:
            return
