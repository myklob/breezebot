"""State-file robustness."""
from __future__ import annotations

import json

from nightcool.state import read_state, write_state


def test_read_state_missing_file_returns_empty(tmp_path):
    assert read_state(tmp_path / "nope.json") == {}


def test_read_state_survives_corrupt_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{truncated", encoding="utf-8")
    assert read_state(p) == {}


def test_read_state_survives_empty_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("", encoding="utf-8")
    assert read_state(p) == {}


def test_read_state_rejects_non_object_json(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("null", encoding="utf-8")
    assert read_state(p) == {}


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"last_action": "open"})
    assert json.loads(p.read_text())["last_action"] == "open"
    assert read_state(p) == {"last_action": "open"}
