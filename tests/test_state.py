"""Tests for the JSON state store: corruption recovery and locked updates."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from nightcool.state import add_subscription, read_state, update_state, write_state


def test_read_state_missing_file(tmp_path):
    assert read_state(tmp_path / "state.json") == {}


def test_read_state_empty_file_self_heals(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("")
    assert read_state(p) == {}


def test_read_state_corrupt_json_self_heals(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"push_subscriptions": [tru')
    assert read_state(p) == {}


def test_read_state_non_object_self_heals(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("[1, 2, 3]")
    assert read_state(p) == {}


def test_update_state_applies_and_returns(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"a": 1})
    result = update_state(p, lambda st: st.update({"b": 2}) or "done")
    assert result == "done"
    assert read_state(p) == {"a": 1, "b": 2}


def test_update_state_skips_write_when_unchanged(tmp_path):
    p = tmp_path / "state.json"
    update_state(p, lambda st: None)
    assert not p.exists()


def test_update_state_concurrent_mutations_all_survive(tmp_path):
    p = tmp_path / "state.json"

    def subscribe(i: int) -> None:
        update_state(p, lambda st: add_subscription(st, {"endpoint": f"https://e/{i}", "keys": {}}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(subscribe, range(20)))

    subs = read_state(p)["push_subscriptions"]
    assert sorted(s["endpoint"] for s in subs) == sorted(f"https://e/{i}" for i in range(20))
    # File stays valid JSON throughout.
    json.loads(p.read_text())
