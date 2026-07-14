"""Cross-process safety of the JSON state store."""
from __future__ import annotations

from datetime import datetime, timezone

from nightcool.state import (
    add_subscription,
    mutate_state,
    read_state,
    set_last_action,
    update_state,
    write_state,
)


def test_update_state_preserves_foreign_keys(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"push_subscriptions": [{"endpoint": "e1"}]})
    update_state(path, {"last_action": "open"})
    st = read_state(path)
    assert st["push_subscriptions"] == [{"endpoint": "e1"}]
    assert st["last_action"] == "open"


def test_mutate_state_returns_state_and_result(tmp_path):
    path = tmp_path / "state.json"
    st, added = mutate_state(path, lambda s: add_subscription(s, {"endpoint": "e1"}))
    assert added is True
    assert st["push_subscriptions"] == [{"endpoint": "e1"}]
    assert read_state(path)["push_subscriptions"] == [{"endpoint": "e1"}]


def test_daemon_write_does_not_clobber_concurrent_web_write(tmp_path):
    # Daemon reads state, a web request lands mid-poll, daemon then persists
    # only its own keys — the subscription must survive.
    path = tmp_path / "state.json"
    write_state(path, {})
    read_state(path)  # daemon's stale in-memory copy
    mutate_state(path, lambda s: add_subscription(s, {"endpoint": "e1"}))  # web server
    now = datetime(2024, 6, 15, tzinfo=timezone.utc)
    mutate_state(path, lambda s: set_last_action(s, "open", now))  # daemon persists
    st = read_state(path)
    assert st["push_subscriptions"] == [{"endpoint": "e1"}]
    assert st["last_action"] == "open"
