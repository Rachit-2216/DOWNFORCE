"""Strict relative selection of default or explicitly reverified OpenF1 evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from downforce_core.detailed.provider_guard import _unlinked_path
from downforce_core.domain.identifiers import validate_safe_identifier
from downforce_core.storage.layout import ensure_contained

EVIDENCE_PATH = "openf1_evidence_path"


def default_evidence_path(session: dict[str, Any]) -> str:
    identity = validate_safe_identifier(session["session_id"])
    return f"study/openf1/{identity}"


def reverified_evidence_path(session: dict[str, Any]) -> str:
    identity = validate_safe_identifier(session["session_id"])
    return f"study/openf1/reverified/{identity}"


def evidence_path(session: dict[str, Any], record: dict[str, Any]) -> str:
    default = default_evidence_path(session)
    if EVIDENCE_PATH not in record:
        return default
    selected = record[EVIDENCE_PATH]
    if type(selected) is not str or selected not in (default, reverified_evidence_path(session)):
        raise ValueError("enrichment evidence path must select this session's owned relative leaf")
    return selected


def evidence_directory(root: Path, session: dict[str, Any], relative: str) -> Path:
    evidence_path(session, {EVIDENCE_PATH: relative})
    return ensure_contained(_unlinked_path(root / relative), root, must_exist=False)
