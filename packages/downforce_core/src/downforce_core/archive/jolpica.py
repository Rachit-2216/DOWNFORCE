"""Jolpica bulk-dump and bounded classic-API archive adapter."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

import pandas as pd  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from downforce_core.archive.contracts import ProviderProvenance
from downforce_core.archive.schemas import ARCHIVE_SCHEMAS, ArchiveTableName
from downforce_core.archive.storage import HistoricalArchiveStore
from downforce_core.exceptions import ProviderUnavailableError, StorageIntegrityError
from downforce_core.storage.layout import ensure_contained
from downforce_core.storage.parquet import file_sha256

JOLPICA_BASE_URL = "https://api.jolpi.ca"
JOLPICA_DUMP_MANIFEST_URL = f"{JOLPICA_BASE_URL}/data/dumps/download/"
JOLPICA_PROVIDER_VERSION = "ergast-compatible+csv-dump-2026"


@dataclass(frozen=True, slots=True)
class JolpicaDumpDescriptor:
    file_hash: str
    file_size: int
    uploaded_at: str
    download_url: str
    delayed_by_days: int


@dataclass(frozen=True, slots=True)
class ArchiveSourceRace:
    season: int
    round_number: int
    name: str
    event_date: str
    circuit_name: str
    locality: str | None
    country: str | None
    country_code: str | None
    results: pa.Table
    laps: pa.Table
    pit_stops: pa.Table
    drivers: tuple[str, ...]
    teams: tuple[str, ...]
    provenance: ProviderProvenance
    cancelled: bool = False

    @property
    def session_id(self) -> str:
        return f"archive-{self.season}-round-{self.round_number:02d}-race"


def _request_bytes(url: str, *, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "DOWNFORCE/0.1 historical-sync"})
    with cast(BinaryIO, urlopen(request, timeout=timeout_seconds)) as response:
        return response.read()


def _optional_string(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(float(str(value)))


def _duration_ms(value: object) -> int | None:
    text = _optional_string(value)
    if text is None:
        return None
    try:
        return int(round(pd.to_timedelta(text).total_seconds() * 1_000))
    except (TypeError, ValueError):
        return None


def _bool_value(value: object) -> bool | None:
    text = _optional_string(value)
    if text is None:
        return None
    return text.casefold() in {"t", "true", "1", "yes"}


def _table(rows: list[dict[str, object]], table_name: ArchiveTableName) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=ARCHIVE_SCHEMAS[table_name])


class JolpicaClient:
    """HTTP client with rate limiting, bounded retries, and raw-response retention."""

    def __init__(
        self,
        store: HistoricalArchiveStore,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        minimum_interval_seconds: float = 0.27,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.minimum_interval_seconds:
            time.sleep(self.minimum_interval_seconds - elapsed)

    def _bytes(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self._wait_for_rate_limit()
            try:
                payload = _request_bytes(url, timeout_seconds=self.timeout_seconds)
                self._last_request_at = time.monotonic()
                return payload
            except HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (OSError, URLError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
            if attempt + 1 < self.max_attempts:
                time.sleep(min(2.0**attempt, 4.0))
        raise ProviderUnavailableError(f"Jolpica request failed: {url}") from last_error

    def _json(self, url: str) -> tuple[dict[str, object], str]:
        payload = self._bytes(url)
        digest = sha256(payload).hexdigest()
        raw_path = self.store.raw_root / "jolpica-classic" / f"sha256-{digest}.json"
        self.store.root.mkdir(parents=True, exist_ok=True)
        ensure_contained(raw_path, self.store.root, must_exist=False)
        retained_is_valid = False
        if raw_path.exists():
            ensure_contained(raw_path, self.store.root, must_exist=True)
            retained_is_valid = raw_path.is_file() and file_sha256(raw_path) == digest
        if not retained_is_valid:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = raw_path.with_name(f".{raw_path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, raw_path)
            finally:
                temporary.unlink(missing_ok=True)
        try:
            return cast(dict[str, object], json.loads(payload)), digest
        except (ValueError, TypeError) as exc:
            raise ProviderUnavailableError("Jolpica returned malformed JSON") from exc

    def dump_descriptor(self) -> JolpicaDumpDescriptor:
        manifest, _ = self._json(JOLPICA_DUMP_MANIFEST_URL)
        delayed = cast(dict[str, dict[str, object]], manifest["delayed_dumps"])["csv"]
        return JolpicaDumpDescriptor(
            file_hash=str(delayed["file_hash"]).casefold(),
            file_size=int(str(delayed["file_size"])),
            uploaded_at=str(delayed["uploaded_at"]),
            download_url=str(delayed["download_url"]),
            delayed_by_days=int(str(manifest["delay_days"])),
        )

    def fetch_dump(self, descriptor: JolpicaDumpDescriptor) -> Path:
        destination = self.store.raw_dump_path(descriptor.file_hash)
        self.store.root.mkdir(parents=True, exist_ok=True)
        ensure_contained(destination, self.store.root, must_exist=False)
        if destination.is_file():
            if destination.stat().st_size != descriptor.file_size:
                raise StorageIntegrityError("retained Jolpica dump size mismatch")
            if file_sha256(destination) != descriptor.file_hash:
                raise StorageIntegrityError("retained Jolpica dump digest mismatch")
            return destination
        audit_candidate = self.store.project_root / ".downforce" / "staging" / "jolpica-delayed.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        if audit_candidate.is_file() and file_sha256(audit_candidate) == descriptor.file_hash:
            shutil.copy2(audit_candidate, temporary)
        else:
            temporary.write_bytes(self._bytes(descriptor.download_url))
        if temporary.stat().st_size != descriptor.file_size:
            temporary.unlink(missing_ok=True)
            raise StorageIntegrityError("downloaded Jolpica dump size mismatch")
        if file_sha256(temporary) != descriptor.file_hash:
            temporary.unlink(missing_ok=True)
            raise StorageIntegrityError("downloaded Jolpica dump digest mismatch")
        os.replace(temporary, destination)
        return destination

    def season_schedule(self, season: int) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        payload, digest = self._json(f"{JOLPICA_BASE_URL}/ergast/f1/{season}.json?limit=100")
        races = cast(
            list[dict[str, object]],
            cast(dict[str, object], cast(dict[str, object], payload["MRData"])["RaceTable"])[
                "Races"
            ],
        )
        return races, (digest,)

    def race_payload(self, season: int, round_number: int) -> tuple[ArchiveSourceRace | None, str]:
        results_payload, result_digest = self._json(
            f"{JOLPICA_BASE_URL}/ergast/f1/{season}/{round_number}/results.json?limit=100"
        )
        race_table = cast(
            dict[str, object], cast(dict[str, object], results_payload["MRData"])["RaceTable"]
        )
        result_races = cast(list[dict[str, object]], race_table["Races"])
        if not result_races:
            return None, result_digest
        race = result_races[0]
        result_rows = cast(list[dict[str, object]], race.get("Results", []))
        if not result_rows:
            return None, result_digest

        lap_rows, lap_digests = self._race_laps(season, round_number)
        pit_rows, pit_digests = self._race_pits(season, round_number)
        session_id = f"archive-{season}-round-{round_number:02d}-race"
        normalized_results: list[dict[str, object]] = []
        driver_names: set[str] = set()
        team_names: set[str] = set()
        for item in result_rows:
            driver = cast(dict[str, object], item["Driver"])
            team = cast(dict[str, object], item["Constructor"])
            driver_name = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
            team_name = str(team.get("name", "")).strip()
            driver_names.add(driver_name)
            team_names.add(team_name)
            time_value = cast(dict[str, object], item.get("Time", {})).get("millis")
            normalized_results.append(
                {
                    "session_id": session_id,
                    "driver_id": str(driver["driverId"]),
                    "driver_code": _optional_string(driver.get("code")),
                    "driver_name": driver_name,
                    "team_id": _optional_string(team.get("constructorId")),
                    "team_name": team_name or None,
                    "car_number": _optional_int(item.get("number")),
                    "grid_position": _optional_int(item.get("grid")),
                    "finish_position": _optional_int(item.get("position")),
                    "points": float(str(item.get("points", 0))),
                    "laps_completed": _optional_int(item.get("laps")),
                    "status": _optional_string(item.get("status")),
                    "classified": str(item.get("positionText", "")).isdecimal(),
                    "total_time_ms": _optional_int(time_value),
                }
            )
        circuit = cast(dict[str, object], race.get("Circuit", {}))
        location = cast(dict[str, object], circuit.get("Location", {}))
        digests = (result_digest, *lap_digests, *pit_digests)
        source_digest = sha256("".join(digests).encode()).hexdigest()
        provenance = ProviderProvenance(
            provider="jolpica",
            provider_version=JOLPICA_PROVIDER_VERSION,
            source="jolpica-classic-api",
            source_url=f"{JOLPICA_BASE_URL}/ergast/f1/{season}/{round_number}",
            retrieved_at_utc=pd.Timestamp.now(tz="UTC").isoformat().replace("+00:00", "Z"),
            raw_sha256=source_digest,
        )
        return (
            ArchiveSourceRace(
                season=season,
                round_number=round_number,
                name=str(race["raceName"]),
                event_date=str(race["date"]),
                circuit_name=str(circuit.get("circuitName", "Unknown circuit")),
                locality=_optional_string(location.get("locality")),
                country=_optional_string(location.get("country")),
                country_code=None,
                results=_table(normalized_results, ArchiveTableName.RESULTS),
                laps=_table(
                    [
                        {"session_id": session_id, **row}
                        for row in sorted(
                            lap_rows,
                            key=lambda row: (
                                int(str(row["lap_number"])),
                                str(row["driver_id"]),
                            ),
                        )
                    ],
                    ArchiveTableName.LAPS,
                ),
                pit_stops=_table(
                    [{"session_id": session_id, **row} for row in pit_rows],
                    ArchiveTableName.PIT_STOPS,
                ),
                drivers=tuple(sorted(driver_names)),
                teams=tuple(sorted(team_names)),
                provenance=provenance,
            ),
            source_digest,
        )

    def _paged_race_data(
        self, season: int, round_number: int, endpoint: str
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        offset = 0
        collected: list[dict[str, object]] = []
        digests: list[str] = []
        total = 1
        while offset < total:
            payload, digest = self._json(
                f"{JOLPICA_BASE_URL}/ergast/f1/{season}/{round_number}/{endpoint}.json"
                f"?limit=100&offset={offset}"
            )
            digests.append(digest)
            metadata = cast(dict[str, object], payload["MRData"])
            total = int(str(metadata["total"]))
            race_table = cast(dict[str, object], metadata["RaceTable"])
            races = cast(list[dict[str, object]], race_table["Races"])
            if not races:
                break
            collected.append(races[0])
            offset += int(str(metadata["limit"]))
        return collected, tuple(digests)

    def _race_laps(
        self, season: int, round_number: int
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        pages, digests = self._paged_race_data(season, round_number, "laps")
        rows: dict[tuple[int, str], dict[str, object]] = {}
        for page in pages:
            for lap in cast(list[dict[str, object]], page.get("Laps", [])):
                lap_number = int(str(lap["number"]))
                for timing in cast(list[dict[str, object]], lap.get("Timings", [])):
                    driver_id = str(timing["driverId"])
                    rows[(lap_number, driver_id)] = {
                        "driver_id": driver_id,
                        "lap_number": lap_number,
                        "position": _optional_int(timing.get("position")),
                        "lap_time_ms": _duration_ms(timing.get("time")),
                        "average_speed_kph": None,
                        "is_fastest_lap": None,
                    }
        return list(rows.values()), digests

    def _race_pits(
        self, season: int, round_number: int
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        pages, digests = self._paged_race_data(season, round_number, "pitstops")
        rows: dict[tuple[str, int], dict[str, object]] = {}
        for page in pages:
            for item in cast(list[dict[str, object]], page.get("PitStops", [])):
                driver_id = str(item["driverId"])
                stop_number = int(str(item["stop"]))
                rows[(driver_id, stop_number)] = {
                    "driver_id": driver_id,
                    "stop_number": stop_number,
                    "lap_number": _optional_int(item.get("lap")),
                    "duration_ms": _duration_ms(item.get("duration")),
                    "local_time": _optional_string(item.get("time")),
                }
        return list(rows.values()), digests


class JolpicaDumpReader:
    """Stream normalized race payloads from one checksum-pinned Jolpica CSV dump."""

    def __init__(self, dump_path: Path, descriptor: JolpicaDumpDescriptor) -> None:
        self.dump_path = dump_path
        self.descriptor = descriptor

    def _csv(self, archive: ZipFile, name: str) -> pd.DataFrame:
        with archive.open(name) as handle:
            return pd.read_csv(handle, low_memory=False)

    def races(self, *, start_year: int, end_year: int) -> Iterator[ArchiveSourceRace]:
        with ZipFile(self.dump_path) as archive:
            seasons = self._csv(archive, "formula_one_season.csv").rename(
                columns={"id": "season_pk"}
            )
            circuits = self._csv(archive, "formula_one_circuit.csv").rename(
                columns={
                    "id": "circuit_pk",
                    "name": "circuit_name",
                    "country_code": "circuit_country_code",
                }
            )
            rounds = self._csv(archive, "formula_one_round.csv").rename(
                columns={
                    "id": "round_pk",
                    "name": "race_name",
                    "number": "round_number",
                }
            )
            sessions = self._csv(archive, "formula_one_session.csv").rename(
                columns={"id": "race_session_pk", "number": "session_number"}
            )
            drivers = self._csv(archive, "formula_one_driver.csv").rename(
                columns={
                    "id": "driver_pk",
                    "reference": "driver_id",
                    "abbreviation": "driver_code",
                }
            )
            teams = self._csv(archive, "formula_one_team.csv").rename(
                columns={"id": "team_pk", "reference": "team_id", "name": "team_name"}
            )
            team_drivers = self._csv(archive, "formula_one_teamdriver.csv").rename(
                columns={
                    "id": "team_driver_pk",
                    "driver_id": "driver_pk",
                    "team_id": "team_pk",
                }
            )
            round_entries = self._csv(archive, "formula_one_roundentry.csv").rename(
                columns={"id": "round_entry_pk"}
            )
            session_entries = self._csv(archive, "formula_one_sessionentry.csv").rename(
                columns={"id": "session_entry_pk", "time": "total_time"}
            )
            laps = self._csv(archive, "formula_one_lap.csv").rename(
                columns={"time": "lap_time", "number": "lap_number"}
            )
            pits = self._csv(archive, "formula_one_pitstop.csv").rename(
                columns={"number": "stop_number"}
            )

        event_frame = (
            sessions[sessions["type"].eq("R")]
            .merge(rounds, left_on="round_id", right_on="round_pk", how="inner")
            .merge(seasons[["season_pk", "year"]], left_on="season_id", right_on="season_pk")
            .merge(circuits, left_on="circuit_id", right_on="circuit_pk", how="left")
        )
        event_frame = event_frame[
            event_frame["year"].between(start_year, end_year) & event_frame["round_number"].notna()
        ].copy()

        driver_links = (
            team_drivers[["team_driver_pk", "driver_pk", "team_pk"]]
            .merge(
                drivers[
                    [
                        "driver_pk",
                        "driver_id",
                        "driver_code",
                        "forename",
                        "surname",
                    ]
                ],
                on="driver_pk",
            )
            .merge(
                teams[["team_pk", "team_id", "team_name"]],
                on="team_pk",
            )
        )
        entries = (
            session_entries.merge(
                round_entries[["round_entry_pk", "car_number", "team_driver_id"]],
                left_on="round_entry_id",
                right_on="round_entry_pk",
            )
            .merge(driver_links, left_on="team_driver_id", right_on="team_driver_pk")
            .merge(
                event_frame[["race_session_pk"]],
                left_on="session_id",
                right_on="race_session_pk",
            )
        )
        entries["driver_name"] = (
            entries["forename"].fillna("").astype(str).str.strip()
            + " "
            + entries["surname"].fillna("").astype(str).str.strip()
        ).str.strip()
        entries["archive_session_id"] = entries["race_session_pk"].map(
            {
                row.race_session_pk: (
                    f"archive-{int(row.year)}-round-{int(row.round_number):02d}-race"
                )
                for row in event_frame.itertuples()
            }
        )
        results_frame = pd.DataFrame(
            {
                "race_session_pk": entries["race_session_pk"],
                "session_id": entries["archive_session_id"].astype("string"),
                "driver_id": entries["driver_id"].astype("string"),
                "driver_code": entries["driver_code"].astype("string"),
                "driver_name": entries["driver_name"].astype("string"),
                "team_id": entries["team_id"].astype("string"),
                "team_name": entries["team_name"].astype("string"),
                "car_number": pd.to_numeric(entries["car_number"], errors="coerce").astype("Int32"),
                "grid_position": pd.to_numeric(entries["grid"], errors="coerce").astype("Int32"),
                "finish_position": pd.to_numeric(entries["position"], errors="coerce").astype(
                    "Int32"
                ),
                "points": pd.to_numeric(entries["points"], errors="coerce"),
                "laps_completed": pd.to_numeric(entries["laps_completed"], errors="coerce").astype(
                    "Int32"
                ),
                "status": entries["detail"].fillna(entries["status"]).astype("string"),
                "classified": entries["is_classified"].map(_bool_value).astype("boolean"),
                "total_time_ms": entries["total_time"].map(_duration_ms).astype("Int64"),
                "session_entry_pk": entries["session_entry_pk"],
            }
        )
        entry_lookup = results_frame[
            ["session_entry_pk", "race_session_pk", "session_id", "driver_id"]
        ]
        lap_frame = laps.merge(
            entry_lookup, left_on="session_entry_id", right_on="session_entry_pk"
        )
        normalized_laps = pd.DataFrame(
            {
                "race_session_pk": lap_frame["race_session_pk"],
                "session_id": lap_frame["session_id"].astype("string"),
                "driver_id": lap_frame["driver_id"].astype("string"),
                "lap_number": pd.to_numeric(lap_frame["lap_number"], errors="coerce").astype(
                    "Int32"
                ),
                "position": pd.to_numeric(lap_frame["position"], errors="coerce").astype("Int32"),
                "lap_time_ms": lap_frame["lap_time"].map(_duration_ms).astype("Int64"),
                "average_speed_kph": pd.to_numeric(lap_frame["average_speed"], errors="coerce"),
                "is_fastest_lap": lap_frame["is_entry_fastest_lap"]
                .map(_bool_value)
                .astype("boolean"),
            }
        )
        pit_frame = pits.merge(
            entry_lookup, left_on="session_entry_id", right_on="session_entry_pk"
        )
        normalized_pits = pd.DataFrame(
            {
                "race_session_pk": pit_frame["race_session_pk"],
                "session_id": pit_frame["session_id"].astype("string"),
                "driver_id": pit_frame["driver_id"].astype("string"),
                "stop_number": pd.to_numeric(pit_frame["stop_number"], errors="coerce").astype(
                    "Int32"
                ),
                "lap_number": pd.to_numeric(
                    pit_frame.merge(
                        laps[["id", "lap_number"]], left_on="lap_id", right_on="id", how="left"
                    )["lap_number"],
                    errors="coerce",
                ).astype("Int32"),
                "duration_ms": pit_frame["duration"].map(_duration_ms).astype("Int64"),
                "local_time": pit_frame["local_timestamp"].astype("string"),
            }
        )

        result_groups = results_frame.groupby("race_session_pk", sort=False)
        lap_groups = normalized_laps.groupby("race_session_pk", sort=False)
        pit_groups = normalized_pits.groupby("race_session_pk", sort=False)
        provenance = ProviderProvenance(
            provider="jolpica",
            provider_version=JOLPICA_PROVIDER_VERSION,
            source="jolpica-delayed-csv-dump",
            source_url=self.descriptor.download_url,
            retrieved_at_utc=self.descriptor.uploaded_at,
            raw_sha256=self.descriptor.file_hash,
        )
        for event in event_frame.sort_values(["year", "round_number"]).itertuples():
            key = event.race_session_pk
            result_group = (
                result_groups.get_group(key).drop(columns=["race_session_pk", "session_entry_pk"])
                if key in result_groups.groups
                else pd.DataFrame(
                    columns=[field.name for field in ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS]]
                )
            )
            lap_group = (
                lap_groups.get_group(key)
                .drop(columns=["race_session_pk"])
                .sort_values(["lap_number", "position", "driver_id"], kind="stable")
                if key in lap_groups.groups
                else pd.DataFrame(
                    columns=[field.name for field in ARCHIVE_SCHEMAS[ArchiveTableName.LAPS]]
                )
            )
            pit_group = (
                pit_groups.get_group(key).drop(columns=["race_session_pk"])
                if key in pit_groups.groups
                else pd.DataFrame(
                    columns=[field.name for field in ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS]]
                )
            )
            result_table = pa.Table.from_pandas(
                result_group, schema=ARCHIVE_SCHEMAS[ArchiveTableName.RESULTS], preserve_index=False
            )
            lap_table = pa.Table.from_pandas(
                lap_group, schema=ARCHIVE_SCHEMAS[ArchiveTableName.LAPS], preserve_index=False
            )
            pit_table = pa.Table.from_pandas(
                pit_group, schema=ARCHIVE_SCHEMAS[ArchiveTableName.PIT_STOPS], preserve_index=False
            )
            yield ArchiveSourceRace(
                season=int(event.year),
                round_number=int(event.round_number),
                name=str(event.race_name),
                event_date=str(event.date),
                circuit_name=str(event.circuit_name),
                locality=_optional_string(event.locality),
                country=_optional_string(event.country),
                country_code=_optional_string(event.circuit_country_code),
                results=result_table,
                laps=lap_table,
                pit_stops=pit_table,
                drivers=tuple(
                    sorted(set(str(item) for item in result_group["driver_name"].dropna()))
                ),
                teams=tuple(sorted(set(str(item) for item in result_group["team_name"].dropna()))),
                provenance=provenance,
                cancelled=bool(
                    _bool_value(getattr(event, "is_cancelled_x", None))
                    or _bool_value(getattr(event, "is_cancelled_y", None))
                ),
            )


def completed_by_date(event_date: str, *, today: date) -> bool:
    """Only decides whether a result request is eligible; results still prove completion."""

    try:
        return date.fromisoformat(event_date) <= today
    except ValueError:
        return False


__all__ = [
    "ArchiveSourceRace",
    "JOLPICA_BASE_URL",
    "JOLPICA_PROVIDER_VERSION",
    "JolpicaClient",
    "JolpicaDumpDescriptor",
    "JolpicaDumpReader",
    "completed_by_date",
]
