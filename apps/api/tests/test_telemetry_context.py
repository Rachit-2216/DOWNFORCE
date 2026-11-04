"""Cross-store destinations are verified, never inferred from telemetry IDs."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from conftest import _historical_provider_session
from downforce_core.archive import (
    ArchiveEventStatus,
    DataQuality,
    HistoricalArchiveStore,
    HistoricalCatalog,
    HistoricalEvent,
    HistoricalSeason,
    HistoricalSession,
    QualityStatus,
    RaceDataCapabilities,
    SyncLifecycle,
)
from downforce_core.archive.schemas import ARCHIVE_SCHEMAS, ArchiveTableName
from downforce_core.detailed.storage import DetailedStore
from downforce_core.normalization import normalize_session
from downforce_core.replay import build_timeline
from downforce_core.storage import DownforceRepository
from test_telemetry_api import client, publish


def telemetry(root: Path, **metadata: object) -> tuple[DetailedStore, str]:
    store, identity, revision = publish(root)
    manifest = store.manifest(identity, revision=revision)
    tables = {
        name: store.read_table(identity, name, revision=revision) for name in manifest["tables"]
    }
    session = {**manifest["metadata"]["session"], "round_number": 12, **metadata}
    store.publish(identity, tables, {"session": session})
    return store, identity


def replay(root: Path, *, events: bool = True) -> str:
    normalized = normalize_session(_historical_provider_session())
    repository = DownforceRepository(root)
    manifest = repository.write_session(
        normalized, "fixture-context", events=build_timeline(normalized).events if events else None
    )
    return manifest.session_id


def archive(root: Path) -> HistoricalArchiveStore:
    store = HistoricalArchiveStore(root)
    identity = "archive-2024-round-12-race"
    tables = {
        name: pa.Table.from_pylist([], schema=schema) for name, schema in ARCHIVE_SCHEMAS.items()
    }
    tables[ArchiveTableName.RESULTS] = pa.Table.from_pylist(
        [
            {
                "session_id": identity,
                "driver_id": "fixture",
                "driver_name": "Fixture Driver",
                "finish_position": 1,
                "points": 25.0,
                "laps_completed": 2,
                "status": "Finished",
                "classified": True,
            }
        ],
        schema=ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS],
    )
    quality = DataQuality(QualityStatus.VERIFIED, (), {}, "2024-07-08T00:00:00Z")
    capabilities = RaceDataCapabilities(results=True)
    revision = store.write_session(
        identity,
        tables,
        source_revision="fixture-context",
        manifest={
            "season": 2024,
            "round_number": 12,
            "session_type": "race",
            "capabilities": capabilities.to_dict(),
            "quality": quality.to_dict(),
        },
    )
    session = HistoricalSession(
        identity,
        "race",
        ArchiveEventStatus.COMPLETED,
        SyncLifecycle.COMPLETE,
        capabilities,
        quality,
        (),
        {"results": 1},
        revision,
    )
    event = HistoricalEvent(
        "event-2024-round-12",
        2024,
        12,
        "Different spelling",
        "British Grand Prix",
        "2024-07-07",
        "A circuit label is not an identity",
        None,
        None,
        None,
        ArchiveEventStatus.COMPLETED,
        (session,),
        (),
        (),
    )
    store.save_catalog(
        HistoricalCatalog(
            "1",
            "2024-07-08T00:00:00Z",
            1950,
            event.event_id,
            event.event_date,
            "fixture-context",
            (HistoricalSeason(2024, (event,)),),
        )
    )
    return store


def row(root: Path) -> dict[str, Any]:
    response = client(root).get("/api/v1/telemetry/sessions")
    assert response.status_code == 200, response.text
    assert ".downforce" not in response.text
    return response.json()["items"][0]  # type: ignore[no-any-return]


def test_verifies_distinct_replay_and_archive_identity_from_source_metadata(tmp_path: Path) -> None:
    telemetry(tmp_path)
    replay_id = replay(tmp_path)
    archive(tmp_path)
    value = row(tmp_path)
    assert value["replay_session_id"] == replay_id
    assert value["replay_driver_numbers"] == ["0", "7"]
    assert value["replay_laps_by_driver"] == {"0": [1, 2], "7": [1]}
    assert value["race_analytics_session_id"] == "archive-2024-round-12-race"
    assert value["replay_unavailable_reason"] is None
    assert value["race_analytics_unavailable_reason"] is None
    api = client(tmp_path)
    assert api.get(f"/api/v1/sessions/{value['replay_session_id']}").status_code == 200
    assert (
        api.get(
            f"/api/v1/sessions/{value['replay_session_id']}/state", params={"lap": 1}
        ).status_code
        == 200
    )


def test_missing_context_has_no_guessed_destinations(tmp_path: Path) -> None:
    telemetry(tmp_path)
    value = row(tmp_path)
    assert value["replay_session_id"] is None
    assert value["race_analytics_session_id"] is None
    assert value["replay_unavailable_reason"]
    assert value["race_analytics_unavailable_reason"]


@pytest.mark.parametrize(
    "metadata", [{"round_number": None}, {"season": None}, {"session_type": "unknown"}]
)
def test_unknown_identity_does_not_guess_from_dataset_or_circuit(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    telemetry(tmp_path, **metadata)
    replay(tmp_path)
    archive(tmp_path)
    value = row(tmp_path)
    assert value["replay_session_id"] is None
    assert value["race_analytics_session_id"] is None


def test_nonrace_context_can_only_link_actual_completed_weekend_race(tmp_path: Path) -> None:
    telemetry(tmp_path, session_type="qualifying")
    replay(tmp_path)
    archive(tmp_path)
    value = row(tmp_path)
    assert value["replay_session_id"] is None
    assert value["race_analytics_session_id"] == "archive-2024-round-12-race"


def test_replay_without_persisted_timeline_is_not_available(tmp_path: Path) -> None:
    telemetry(tmp_path)
    replay(tmp_path, events=False)
    assert row(tmp_path)["replay_session_id"] is None


@pytest.mark.parametrize(
    "change",
    [
        "upcoming",
        "quality",
        "missing",
        "corrupt",
        "wrong-event",
        "wrong-session",
        "manifest-quality",
        "manifest-capabilities",
    ],
)
def test_archive_unavailable_or_mismatched_destination_does_not_break_telemetry(
    tmp_path: Path, change: str
) -> None:
    telemetry(tmp_path)
    store = archive(tmp_path)
    catalog = store.load_catalog()
    event = catalog.events[0]
    session = event.race_session
    if change == "upcoming":
        event = replace(event, status=ArchiveEventStatus.UPCOMING)
    elif change == "quality":
        session = replace(session, quality=replace(session.quality, status=QualityStatus.UNUSABLE))
    elif change == "wrong-event":
        event = replace(event, round_number=13)
    elif change == "wrong-session":
        session = replace(session, session_type="qualifying")
    elif change == "missing":
        session = replace(session, data_revision="archive-revision-sha256-" + "f" * 64)
    elif change.startswith("manifest-"):
        manifest = store.load_manifest(session.session_id)
        manifest[change.removeprefix("manifest-")] = None
        tables = {name: store.load_table(session.session_id, name) for name in ArchiveTableName}
        revision = store.write_session(
            session.session_id, tables, source_revision="malformed-context", manifest=manifest
        )
        session = replace(session, data_revision=revision)
    else:
        store.catalog_path.write_text("not json", encoding="utf-8")
    if change != "corrupt":
        store.save_catalog(
            replace(
                catalog, seasons=(HistoricalSeason(2024, (replace(event, sessions=(session,)),)),)
            )
        )
    assert row(tmp_path)["race_analytics_session_id"] is None


def test_corrupt_replay_alias_is_isolated_from_native_telemetry(tmp_path: Path) -> None:
    telemetry(tmp_path)
    replay(tmp_path)
    repository = DownforceRepository(tmp_path)
    repository.layout.alias_pointer("session-2024-round-12-type-race").write_text(
        "not json", encoding="utf-8"
    )
    assert row(tmp_path)["replay_session_id"] is None


def test_malformed_catalog_capability_is_isolated_from_native_telemetry(tmp_path: Path) -> None:
    telemetry(tmp_path)
    store = archive(tmp_path)
    value = store.load_catalog().to_dict()
    value["seasons"][0]["events"][0]["sessions"][0]["capabilities"] = None  # type: ignore[index]
    store.catalog_path.write_text(json.dumps(value), encoding="utf-8")
    assert row(tmp_path)["race_analytics_session_id"] is None
