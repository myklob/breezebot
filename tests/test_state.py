"""State store: atomic writes and cross-process-safe read-modify-write."""
from __future__ import annotations

from nightcool.state import mutate_state, read_state, write_state


def test_mutate_state_builds_on_latest_disk_contents(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"a": 1})
    # Another process updates the file after our hypothetical earlier read.
    write_state(path, {"a": 1, "b": 2})
    mutate_state(path, lambda s: s.update(c=3))
    assert read_state(path) == {"a": 1, "b": 2, "c": 3}


def test_mutate_state_returns_mutator_result(tmp_path):
    path = tmp_path / "state.json"
    write_state(path, {"n": 41})
    got = mutate_state(path, lambda s: s.get("n"))
    assert got == 41


def test_mutate_state_creates_missing_file(tmp_path):
    path = tmp_path / "state.json"
    assert mutate_state(path, lambda s: s.setdefault("fresh", True)) is True
    assert read_state(path) == {"fresh": True}
