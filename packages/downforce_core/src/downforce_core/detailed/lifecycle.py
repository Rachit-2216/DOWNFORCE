"""Atomic single-writer checkpoints for recoverable detailed-data jobs."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from downforce_core.detailed.paths import DetailedPaths
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.exceptions import StorageIntegrityError
from downforce_core.storage.layout import ensure_contained, write_text_atomic

ORDER = (
    "DISCOVERED",
    "DOWNLOADING",
    "SOURCE_CACHED",
    "RAW_EVIDENCE_PUBLISHED",
    "NORMALIZING",
    "CANONICAL_PUBLISHED",
    "VALIDATED",
    "CACHE_RELEASED",
    "COMPLETE",
)


class Lifecycle:
    """A checkpoint is a recovery hint, never a substitute for artifact validation.

    The caller must validate both immutable stores before VALIDATED, again before
    deleting any owned cache, and before reusing COMPLETE. One sync worker owns
    a session at a time. Failed writes leave the previous atomic record intact.
    """

    def __init__(self, paths: DetailedPaths, session_id: str) -> None:
        validate_safe_identifier(session_id)
        self.paths = paths
        self.session_id = session_id
        paths.root.mkdir(parents=True, exist_ok=True)
        self.path = ensure_contained(
            paths.root / "jobs" / f"{session_id}.json", paths.root, must_exist=False
        )

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "session_id": self.session_id,
                "state": "DISCOVERED",
                "checkpoint": "DISCOVERED",
                "history": [],
            }
        try:
            value: dict[str, Any] = json.loads(self.path.read_text())
            if (
                value["session_id"] != self.session_id
                or value["checkpoint"] not in ORDER
                or value["state"] not in (*ORDER, "FAILED")
            ):
                raise ValueError("invalid lifecycle record")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageIntegrityError("invalid detailed job checkpoint") from exc

    def _write(self, value: dict[str, Any]) -> None:
        ensure_contained(self.path, self.paths.root, must_exist=False)
        write_text_atomic(self.path, json.dumps(value, sort_keys=True, allow_nan=False))

    def advance(self, state: str, *, identity: str | None = None, verified: bool = False) -> None:
        current = self.read()
        field = {
            "RAW_EVIDENCE_PUBLISHED": "raw_identity",
            "CANONICAL_PUBLISHED": "canonical_identity",
        }.get(state)
        if field and (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity)):
            raise ValueError("publication checkpoint requires an immutable identity")
        if current["state"] == state:
            if field and current.get(field) != identity:
                raise ValueError("cannot change a published checkpoint identity")
            return
        allowed = ORDER[ORDER.index(current["checkpoint"]) + 1 :][:1]
        if current["checkpoint"] == "VALIDATED":
            allowed = ("CACHE_RELEASED", "COMPLETE")
        if current["state"] == "FAILED" or state not in allowed:
            raise ValueError("invalid lifecycle transition; resume failed jobs first")
        if state == "VALIDATED" and not verified:
            raise ValueError("both immutable artifacts must be verified")
        if field:
            current[field] = identity
        current.update(state=state, checkpoint=state)
        current["history"].append({"state": state, "at": datetime.now(UTC).isoformat()})
        self._write(current)

    def fail(self, error: str) -> None:
        current = self.read()
        if current["state"] == "COMPLETE":
            raise ValueError("completed jobs require revalidation, not checkpoint rollback")
        current.update(state="FAILED", error=error)
        current["history"].append({"state": "FAILED", "at": datetime.now(UTC).isoformat()})
        self._write(current)

    def resume(self) -> str:
        current = self.read()
        if current["state"] == "FAILED":
            current["state"] = current["checkpoint"]
            current.pop("error", None)
            self._write(current)
        return str(current["checkpoint"])
