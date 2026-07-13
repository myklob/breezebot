"""Tiny JSON-backed state store.

Holds the last-notified action (for dedup), the manually-entered indoor
temperature, and the list of web-push subscriptions. No database; the file
lives in the working directory.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger("nightcool.state")

T = TypeVar("T")


def read_state(path: Path) -> dict[str, Any]:
    """Load state from disk; return empty dict if file is missing or unreadable."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("state file %s unreadable (%s); starting fresh", p, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("state file %s is not a JSON object; starting fresh", p)
        return {}
    return data


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically overwrite the state file (write-to-temp then rename)."""
    p = Path(path)
    data = json.dumps(state, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Advisory cross-process lock. Uses a sidecar file because write_state
    replaces the state file's inode on every write."""
    lock_path = Path(str(path) + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def update_state(path: Path, mutator: Callable[[dict[str, Any]], T]) -> T:
    """Read-modify-write the state file under an exclusive lock.

    The daemon and the web server (and the web server's own request threads)
    all mutate the same file; a bare read_state → write_state pair loses
    whichever update commits first. Skips the write when the mutator changed
    nothing.
    """
    with _state_lock(path):
        state = read_state(path)
        before = json.dumps(state, sort_keys=True, default=str)
        result = mutator(state)
        if json.dumps(state, sort_keys=True, default=str) != before:
            write_state(path, state)
        return result


def get_last_action(state: dict[str, Any]) -> str | None:
    return state.get("last_action")


def set_last_action(state: dict[str, Any], action: str, now: datetime) -> None:
    state["last_action"] = action
    state["last_action_time"] = now.isoformat()


def get_indoor_temp(state: dict[str, Any], default: float) -> float:
    val = state.get("indoor_temp_f")
    return float(val) if val is not None else float(default)


def get_indoor_temp_or_none(state: dict[str, Any]) -> float | None:
    val = state.get("indoor_temp_f")
    return float(val) if val is not None else None


def set_indoor_temp(state: dict[str, Any], temp_f: float, now: datetime) -> None:
    state["indoor_temp_f"] = float(temp_f)
    state["indoor_temp_time"] = now.isoformat()


def list_subscriptions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """All registered web-push subscriptions."""
    return list(state.get("push_subscriptions", []))


def add_subscription(state: dict[str, Any], subscription: dict[str, Any]) -> bool:
    """Append `subscription` if its endpoint isn't already registered.

    Returns True if newly added.
    """
    subs = list(state.get("push_subscriptions", []))
    endpoint = subscription.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    if any(s.get("endpoint") == endpoint for s in subs):
        return False
    subs.append(subscription)
    state["push_subscriptions"] = subs
    return True


def remove_subscription(state: dict[str, Any], endpoint: str) -> bool:
    """Drop the subscription with the given endpoint. Returns True if removed."""
    subs = list(state.get("push_subscriptions", []))
    kept = [s for s in subs if s.get("endpoint") != endpoint]
    if len(kept) == len(subs):
        return False
    state["push_subscriptions"] = kept
    return True
