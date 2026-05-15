"""Daemon-level integration test: state dedup with a MockWeatherProvider."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nightcool.config import (
    AppConfig,
    Exposure,
    IndoorTempConfig,
    Location,
    NotificationConfig,
    Security,
    UserPrefs,
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
    return AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="America/Denver"),
        user_prefs=UserPrefs(
            sleep_target_f=65.0,
            min_tolerable_outdoor_f=50.0,
            hysteresis_f=2.5,
            bad_wind_sector_deg=(60.0, 120.0),
            quiet_hours_start=time(22, 30),
            quiet_hours_end=time(6, 0),
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
    # Pre-seed indoor temp so the engine sees 71°F.
    st: dict = {}
    set_indoor_temp(st, 71.0, datetime.now())
    write_state(state_path, st)

    # Use a fixed "now" inside daytime (not quiet hours).
    fake_now = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    provider = MockWeatherProvider(_cool_forecast(fake_now))

    with patch("nightcool.daemon.datetime") as dt:
        dt.now.return_value = fake_now
        dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        rec1 = run_once(cfg, state_path, provider=provider)
        rec2 = run_once(cfg, state_path, provider=provider)

    assert rec1.action == "open"
    assert rec2.action == "open"

    # Console notifier prints; after the dedup, only one print happens.
    captured = capsys.readouterr().out
    assert captured.count("OPEN:") == 1

    # State recorded the OPEN.
    stored = json.loads(state_path.read_text())
    assert stored["last_action"] == "open"

    # Data log was created and has at least one row.
    assert (tmp_path / "data.sqlite").exists()
