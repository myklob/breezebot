"""Tiny JSON-backed state store.

Holds the last-notified action (for dedup), the manually-entered indoor
temperature, and the list of web-push subscriptions. No database; the file
lives in the working directory.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows; falls back to no locking.
    fcntl = None  # type: ignore[assignment]


def read_state(path: Path) -> dict[str, Any]:
    """Load state from disk; return empty dict if file is missing."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


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
def _file_lock(path: Path) -> Iterator[None]:
    """Exclusive inter-process lock keyed to the state file."""
    if fcntl is None:
        yield
        return
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def mutate_state(path: Path, fn: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
    """Read-modify-write the state file under an exclusive lock.

    The daemon, web server, and CLI share state.json across processes;
    writers must mutate a freshly-read copy inside the lock (never persist
    a dict read earlier) or they clobber each other's keys.

    Returns (state after mutation, fn's return value).
    """
    with _file_lock(path):
        state = read_state(path)
        result = fn(state)
        write_state(path, state)
    return state, result


def update_state(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` into the on-disk state under the lock."""
    state, _ = mutate_state(path, lambda s: s.update(updates))
    return state


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
