"""Locked read-modify-write semantics for the state file."""
from __future__ import annotations

from nightcool.state import read_state, update_state, write_state


def test_update_state_preserves_concurrent_writes(tmp_path):
    # A stale-snapshot writer would clobber the indoor temp written between
    # its read and its write; update_state re-reads under the lock instead.
    path = tmp_path / "state.json"
    write_state(path, {"last_action": "close"})
    write_state(path, {"last_action": "close", "indoor_temp_f": 68.0})
    update_state(path, lambda st: st.__setitem__("last_action", "open"))
    st = read_state(path)
    assert st["indoor_temp_f"] == 68.0
    assert st["last_action"] == "open"


def test_update_state_skips_write_when_unchanged(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"a": 1})
    mtime = path.stat().st_mtime_ns
    result = update_state(path, lambda st: st.get("a"))
    assert result == 1
    assert path.stat().st_mtime_ns == mtime


def test_update_state_returns_mutator_result(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {})

    def mutate(st):
        st["k"] = "v"
        return "the-result"

    assert update_state(path, mutate) == "the-result"
    assert read_state(path)["k"] == "v"
