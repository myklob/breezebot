"""Daemon-level integration test: state dedup with a MockWeatherProvider."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nightcool.config import (
    AppConfig,
    DailySchedule,
    DaySchedule,
    Exposure,
    IndoorTempConfig,
    Location,
    NotificationConfig,
    Security,
    WebServerConfig,
    Window,
)
from nightcool.daemon import run_once
from nightcool.engine import HourlyForecast
from nightcool.state import set_indoor_temp, write_state
from nightcool.weather import MockWeatherProvider


def _cool_forecast(start: datetime) -> list[HourlyForecast]:
    return [
        HourlyForecast(
            timestamp=start + timedelta(hours=i),
            temperature_f=62.0,
            wind_speed_mph=5.0,
            wind_gust_mph=5.0,
            wind_direction_deg=270.0,
            rain_chance_pct=0.0,
        )
        for i in range(12)
    ]


def _make_cfg(tmp_path: Path) -> AppConfig:
    weekend = DaySchedule(target_f=65.0, home_all_day=True)
    return AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="America/Denver"),
        schedule=DailySchedule(
            mon=weekend, tue=weekend, wed=weekend, thu=weekend, fri=weekend,
            sat=weekend, sun=weekend,
        ),
        indoor_temp=IndoorTempConfig(source="manual", manual_default_f=71.0),
        windows=[
            Window(id="br_west", name="Master West", exposure=Exposure.EXPOSED, security=Security.SECURE),
        ],
        notifications=NotificationConfig(service="console"),
        web=WebServerConfig(data_log_path=tmp_path / "data.sqlite"),
    )


def test_run_once_dedups_repeated_open(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    cfg = _make_cfg(tmp_path)
    st: dict = {}
    set_indoor_temp(st, 71.0, datetime.now())
    write_state(state_path, st)

    fake_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    provider = MockWeatherProvider(_cool_forecast(fake_now))

    with patch("nightcool.daemon.datetime") as dt:
        dt.now.return_value = fake_now
        dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        rec1 = run_once(cfg, state_path, provider=provider)
        rec2 = run_once(cfg, state_path, provider=provider)

    assert rec1.action == "open"
    assert rec2.action == "open"

    captured = capsys.readouterr().out
    assert captured.count("OPEN:") == 1

    stored = json.loads(state_path.read_text())
    assert stored["last_action"] == "open"
    assert (tmp_path / "data.sqlite").exists()


def test_read_state_recovers_from_corrupt_file(tmp_path):
    from nightcool.state import read_state

    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    assert read_state(state_path) == {}
    assert not state_path.exists()
    assert (tmp_path / "state.json.corrupt").exists()


def test_run_once_preserves_concurrent_state_writes(tmp_path):
    """A write landing during the poll (e.g. the web process pruning a push
    subscription) must not be clobbered by run_once's own state write."""
    from nightcool.state import read_state

    state_path = tmp_path / "state.json"
    cfg = _make_cfg(tmp_path)
    st: dict = {}
    set_indoor_temp(st, 71.0, datetime.now())
    write_state(state_path, st)

    fake_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    provider = MockWeatherProvider(_cool_forecast(fake_now))

    class ConcurrentWriteNotifier:
        def send(self, title, body):
            mid = read_state(state_path)
            mid["written_mid_poll"] = True
            write_state(state_path, mid)

    with patch("nightcool.daemon.datetime") as dt, \
         patch("nightcool.daemon._build_notifier", return_value=ConcurrentWriteNotifier()):
        dt.now.return_value = fake_now
        dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        rec = run_once(cfg, state_path, provider=provider)

    assert rec.action == "open"
    stored = json.loads(state_path.read_text())
    assert stored["last_action"] == "open"
    assert stored.get("written_mid_poll") is True
