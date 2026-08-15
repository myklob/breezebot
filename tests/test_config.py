"""Tests for the YAML config loader."""
from __future__ import annotations

from datetime import time

import pytest

from nightcool.config import Exposure, Security, load_config


VALID_YAML = """
location:
  address: "1600 Pennsylvania Ave NW, Washington DC"
  latitude: 38.8977
  longitude: -77.0365
  timezone: America/New_York

schedule:
  mon: { target_f: 68, leave_at: "07:30" }
  tue: { target_f: 68, leave_at: "07:30" }
  wed: { target_f: 68, leave_at: "07:30" }
  thu: { target_f: 68, leave_at: "07:30" }
  fri: { target_f: 68, leave_at: "07:30" }
  sat: { target_f: 65, home_all_day: true }
  sun: { target_f: 65, home_all_day: true }

comfort_floor:
  min_indoor_f: 60.0

prefs:
  hysteresis_f: 2.5
  min_tolerable_outdoor_f: 50
  quiet_hours_start: "22:30"
  quiet_hours_end: "06:00"

warnings:
  warn_on_rain: true
  bad_wind_sector_deg: [60, 120]

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
    assert cfg.location.latitude == pytest.approx(38.8977)
    assert cfg.location.timezone == "America/New_York"
    assert cfg.schedule.mon.target_f == 68.0
    assert cfg.schedule.mon.leave_at == time(7, 30)
    assert cfg.schedule.sat.home_all_day is True
    assert cfg.warnings.warn_on_rain is True
    assert cfg.warnings.bad_wind_sector_deg == (60.0, 120.0)
    assert cfg.prefs.quiet_hours_start == time(22, 30)
    assert cfg.comfort_floor.min_indoor_f == 60.0
    assert len(cfg.windows) == 2
    assert cfg.windows[0].exposure == Exposure.EXPOSED
    assert cfg.windows[1].exposure == Exposure.TILED
    assert cfg.notifications.service == "ntfy"


def test_duplicate_window_ids_rejected(tmp_path):
    bad = """
location: {address: "x", timezone: UTC}
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
location: {address: "x", timezone: UTC}
warnings:
  bad_wind_sector_deg: [400, 500]
windows:
  - {id: w1, name: a, exposure: exposed, security: secure}
notifications: {service: console}
"""
    p = tmp_path / "config.yaml"
    p.write_text(bad)
    with pytest.raises(Exception):
        load_config(p)


def test_location_without_address_or_coordinates_rejected(tmp_path):
    bad = """
location: {timezone: America/Denver}
windows:
  - {id: w1, name: a, exposure: exposed, security: secure}
"""
    p = tmp_path / "config.yaml"
    p.write_text(bad)
    with pytest.raises(Exception, match="location requires"):
        load_config(p)


def test_defaults_fill_in_missing_sections(tmp_path):
    minimal = """
location: {address: "Denver CO", timezone: America/Denver}
windows:
  - {id: w, name: w, exposure: exposed, security: secure}
"""
    p = tmp_path / "c.yaml"
    p.write_text(minimal)
    cfg = load_config(p)
    assert cfg.schedule.sat.home_all_day is True
    assert cfg.prefs.hysteresis_f == 2.5
    assert cfg.warnings.warn_on_rain is False
    assert cfg.notifications.service == "console"


def test_invalid_timezone_rejected(tmp_path):
    bad = """
location: {address: "x", timezone: Mars/Olympus}
windows:
  - {id: w, name: w, exposure: exposed, security: secure}
"""
    p = tmp_path / "c.yaml"
    p.write_text(bad)
    with pytest.raises(Exception, match="timezone"):
        load_config(p)
