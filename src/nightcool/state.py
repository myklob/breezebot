"""Tiny JSON-backed state store.

Holds the last-notified action (for dedup) and the manually-entered indoor
temperature. No database; the file lives in the working directory.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def read_state(path: Path) -> dict[str, Any]:
    """Load state from disk; return empty dict if file is missing."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically-ish overwrite the state file."""
    p = Path(path)
    p.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def get_last_action(state: dict[str, Any]) -> str | None:
    return state.get("last_action")


def set_last_action(state: dict[str, Any], action: str, now: datetime) -> None:
    state["last_action"] = action
    state["last_action_time"] = now.isoformat()


def get_indoor_temp(state: dict[str, Any], default: float) -> float:
    val = state.get("indoor_temp_f")
    return float(val) if val is not None else float(default)


def set_indoor_temp(state: dict[str, Any], temp_f: float, now: datetime) -> None:
    state["indoor_temp_f"] = float(temp_f)
    state["indoor_temp_time"] = now.isoformat()
