"""State-file read/modify/write behavior."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from nightcool.state import (
    read_state,
    set_last_action,
    update_state,
    write_state,
)


def test_update_state_keeps_writes_made_since_the_caller_read(tmp_path):
    """A cycle holds its state dict across a push send that prunes dead
    subscriptions. Writing that stale dict back resurrected them."""
    path = tmp_path / "state.json"
    write_state(path, {
        "indoor_temp_f": 71.0,
        "push_subscriptions": [{"endpoint": "https://example.test/dead"}],
    })

    stale = read_state(path)  # what the cycle read at the top

    # Something else prunes the dead subscription while the cycle is working.
    write_state(path, {"indoor_temp_f": 71.0, "push_subscriptions": []})

    now = datetime(2024, 6, 15, 22, 0, tzinfo=timezone.utc)
    update_state(path, lambda s: set_last_action(s, "open", now))

    on_disk = json.loads(path.read_text())
    assert on_disk["push_subscriptions"] == []
    assert on_disk["last_action"] == "open"
    assert stale["push_subscriptions"] != []  # the caller's copy is untouched


def test_update_state_returns_the_written_state(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"a": 1})
    result = update_state(path, lambda s: s.__setitem__("b", 2))
    assert result == {"a": 1, "b": 2}
    assert json.loads(path.read_text()) == {"a": 1, "b": 2}
