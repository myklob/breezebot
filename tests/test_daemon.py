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


class _PruningNotifier:
    """Mimics WebPushNotifier: send() discovers a 410-gone subscription and
    prunes it through the callback the daemon wired up."""

    def __init__(self, pruner):
        self._pruner = pruner

    def send(self, title: str, body: str) -> None:
        self._pruner("https://push.example/DEAD")


def test_run_once_does_not_resurrect_pruned_subscription(tmp_path):
    state_path = tmp_path / "state.json"
    dead_sub = {"endpoint": "https://push.example/DEAD", "keys": {"auth": "x", "p256dh": "y"}}
    write_state(state_path, {"indoor_temp_f": 71.0, "push_subscriptions": [dead_sub]})

    cfg = _make_cfg(tmp_path)
    fake_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    provider = MockWeatherProvider(_cool_forecast(fake_now))

    def fake_make_notifier(ncfg, *, state_path=None, subscription_loader=None,
                           prune_subscription=None):
        return _PruningNotifier(prune_subscription)

    with patch("nightcool.daemon.datetime") as dt, \
         patch("nightcool.daemon.make_notifier", fake_make_notifier):
        dt.now.return_value = fake_now
        rec = run_once(cfg, state_path, provider=provider)

    assert rec.action == "open"
    stored = json.loads(state_path.read_text())
    assert stored.get("push_subscriptions") == []
    assert stored["last_action"] == "open"


class _ConcurrentWriterNotifier:
    """Simulates the web process storing a new indoor temp while the daemon
    is blocked inside notifier.send()."""

    def __init__(self, state_path: Path):
        self._state_path = state_path

    def send(self, title: str, body: str) -> None:
        from nightcool.state import read_state
        st = read_state(self._state_path)
        set_indoor_temp(st, 68.0, datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc))
        write_state(self._state_path, st)


def test_run_once_preserves_concurrent_state_updates(tmp_path):
    state_path = tmp_path / "state.json"
    write_state(state_path, {"indoor_temp_f": 71.0})
    cfg = _make_cfg(tmp_path)
    fake_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    provider = MockWeatherProvider(_cool_forecast(fake_now))

    with patch("nightcool.daemon.datetime") as dt, \
         patch("nightcool.daemon.make_notifier",
               lambda ncfg, **kw: _ConcurrentWriterNotifier(state_path)):
        dt.now.return_value = fake_now
        run_once(cfg, state_path, provider=provider)

    stored = json.loads(state_path.read_text())
    assert stored["indoor_temp_f"] == 68.0
    assert stored["last_action"] == "open"
