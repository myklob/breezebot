"""Tests for the YAML config loader."""
from __future__ import annotations

from datetime import time

import pytest

from nightcool.config import Exposure, Security, load_config


VALID_YAML = """
location:
  latitude: 39.7392
  longitude: -104.9903
  timezone: America/Denver
user_prefs:
  sleep_target_f: 65.0
  min_tolerable_outdoor_f: 50.0
  hysteresis_f: 2.5
  max_gust_mph: 18.0
  max_rain_chance_pct: 20.0
  bad_wind_sector_deg: [60, 120]
  quiet_hours_start: "22:30"
  quiet_hours_end: "06:00"
indoor_temp:
  source: manual
  manual_default_f: 71.0
windows:
  - id: br_west
    name: Master Bedroom West
    exposure: exposed
    security: secure
    on_bad_wind_sector: false
  - id: bathroom_upstairs
    name: Upstairs Bathroom
    exposure: tiled
    security: secure
notifications:
  service: ntfy
  ntfy_topic: nightcool-test-topic
"""


def test_load_full_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(VALID_YAML)
    cfg = load_config(p)
    assert cfg.location.latitude == pytest.approx(39.7392)
    assert cfg.location.timezone == "America/Denver"
    assert cfg.user_prefs.sleep_target_f == 65.0
    assert cfg.user_prefs.bad_wind_sector_deg == (60.0, 120.0)
    assert cfg.user_prefs.quiet_hours_start == time(22, 30)
    assert len(cfg.windows) == 2
    assert cfg.windows[0].exposure == Exposure.EXPOSED
    assert cfg.windows[1].exposure == Exposure.TILED
    assert cfg.windows[0].security == Security.SECURE
    assert cfg.notifications.service == "ntfy"
    assert cfg.notifications.ntfy_topic == "nightcool-test-topic"


def test_duplicate_window_ids_rejected(tmp_path):
    bad = """
location: {latitude: 39, longitude: -104, timezone: America/Denver}
user_prefs:
  sleep_target_f: 65
  min_tolerable_outdoor_f: 50
indoor_temp: {source: manual, manual_default_f: 71}
windows:
  - {id: w1, name: a, exposure: exposed, security: secure}
  - {id: w1, name: b, exposure: exposed, security: secure}
notifications: {service: console}
"""
    p = tmp_path / "config.yaml"
    p.write_text(bad)
    with pytest.raises(Exception):
        load_config(p)


def test_bad_wind_sector_validates_range(tmp_path):
    bad = """
location: {latitude: 39, longitude: -104, timezone: America/Denver}
user_prefs:
  sleep_target_f: 65
  min_tolerable_outdoor_f: 50
  bad_wind_sector_deg: [400, 500]
indoor_temp: {source: manual, manual_default_f: 71}
windows:
  - {id: w1, name: a, exposure: exposed, security: secure}
notifications: {service: console}
"""
    p = tmp_path / "config.yaml"
    p.write_text(bad)
    with pytest.raises(Exception):
        load_config(p)
