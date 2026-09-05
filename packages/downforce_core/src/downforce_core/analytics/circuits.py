"""Resolve stable provider circuit identities without mutating the locked archive."""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pandas as pd  # type: ignore[import-untyped]

from downforce_core.archive.storage import HistoricalArchiveStore


def _fallback_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return value or "unknown-circuit"


class CircuitIdentityResolver:
    """Map season/round to Jolpica's canonical circuit reference."""

    def __init__(self, identities: dict[tuple[int, int], str] | None = None) -> None:
        self.identities = identities or {}

    @classmethod
    def from_store(cls, store: HistoricalArchiveStore) -> CircuitIdentityResolver:
        dumps = sorted((store.raw_root / "jolpica").glob("jolpica-csv-sha256-*.zip"))
        if not dumps:
            return cls()
        return cls(cls._read_dump(dumps[-1]))

    @staticmethod
    def _read_dump(path: Path) -> dict[tuple[int, int], str]:
        try:
            with ZipFile(path) as archive:
                seasons = pd.read_csv(archive.open("formula_one_season.csv"), low_memory=False)
                rounds = pd.read_csv(archive.open("formula_one_round.csv"), low_memory=False)
                circuits = pd.read_csv(archive.open("formula_one_circuit.csv"), low_memory=False)
        except (BadZipFile, KeyError, OSError, ValueError):
            return {}
        frame = rounds.merge(
            seasons[["id", "year"]].rename(columns={"id": "season_pk"}),
            left_on="season_id",
            right_on="season_pk",
            how="inner",
        ).merge(
            circuits[["id", "reference"]].rename(columns={"id": "circuit_pk"}),
            left_on="circuit_id",
            right_on="circuit_pk",
            how="left",
        )
        frame = frame[frame["number"].notna() & frame["reference"].notna()]
        return {(int(row.year), int(row.number)): str(row.reference) for row in frame.itertuples()}

    def resolve(self, season: int, round_number: int, circuit_name: str) -> str:
        return self.identities.get((season, round_number), _fallback_id(circuit_name))

    def provider_identity_count(self) -> int:
        return len(self.identities)


__all__ = ["CircuitIdentityResolver"]
