"""FastAPI HTTP layer."""
from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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
from nightcool.engine import HourlyForecast
from nightcool.geocode import GeocodeResult
from nightcool.weather import MockWeatherProvider
from nightcool.web.app import create_app


def _cfg(tmp_path: Path) -> AppConfig:
    weekend = DaySchedule(target_f=65.0, home_all_day=True)
    return AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="UTC"),
        schedule=DailySchedule(
            mon=weekend, tue=weekend, wed=weekend, thu=weekend, fri=weekend,
            sat=weekend, sun=weekend,
        ),
        indoor_temp=IndoorTempConfig(source="manual", manual_default_f=71.0),
        windows=[Window(id="w", name="W", exposure=Exposure.EXPOSED, security=Security.SECURE)],
        notifications=NotificationConfig(service="console"),
        web=WebServerConfig(data_log_path=tmp_path / "data.sqlite"),
    )


def _forecast(start: datetime) -> list[HourlyForecast]:
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


@pytest.fixture
def client(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"indoor_temp_f": 71.0}))
    cfg = _cfg(tmp_path)
    provider = MockWeatherProvider(_forecast(datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)))
    app = create_app(cfg, state, provider=provider)
    with TestClient(app) as c:
        yield c, state, cfg, tmp_path


def test_get_state_returns_recommendation(client):
    c, *_ = client
    r = c.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["indoor_f"] == 71.0
    assert "today" in data
    assert data["today"]["target_f"] == 65.0
    assert data["recommendation"]["action"] in ("open", "close", "no_change", "summary")
    assert len(data["forecast_head"]) == 6


def test_get_schedule_lists_all_days(client):
    c, *_ = client
    r = c.get("/api/schedule")
    assert r.status_code == 200
    s = r.json()
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        assert day in s
        assert "target_f" in s[day]


def test_post_indoor_temp_persists(client):
    c, state, *_ = client
    r = c.post("/api/indoor-temp", json={"temperature_f": 68.5})
    assert r.status_code == 200
    assert json.loads(state.read_text())["indoor_temp_f"] == 68.5


def test_post_schedule_day_requires_config_path(client):
    c, *_ = client
    r = c.post("/api/schedule/mon", json={"target_f": 70.0})
    assert r.status_code == 409  # No config_path → can't persist.


def test_post_schedule_day_persists_with_config_path(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
location: {latitude: 39, longitude: -104, timezone: UTC}
windows: [{id: w, name: W, exposure: exposed, security: secure}]
""".strip()
    )
    from nightcool.config import load_config
    cfg = load_config(config_path)
    provider = MockWeatherProvider(_forecast(datetime(2024, 6, 15, 14, tzinfo=timezone.utc)))
    app = create_app(cfg, state, config_path=config_path, provider=provider)
    with TestClient(app) as c:
        r = c.post("/api/schedule/mon", json={"target_f": 70.0, "leave_at": "08:00"})
        assert r.status_code == 200
    import yaml
    raw = yaml.safe_load(config_path.read_text())
    assert raw["schedule"]["mon"]["target_f"] == 70.0
    assert raw["schedule"]["mon"]["leave_at"].startswith("08:00")


def test_unchecking_home_all_day_preserves_leave_at(tmp_path):
    # The PWA sends only the changed field, so unchecking the box POSTs
    # {"home_all_day": false} with no leave_at — that must not erase the
    # stored leave time.
    state = tmp_path / "state.json"
    state.write_text("{}")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
location: {latitude: 39, longitude: -104, timezone: UTC}
windows: [{id: w, name: W, exposure: exposed, security: secure}]
schedule: {mon: {target_f: 65, leave_at: "07:30", home_all_day: false}}
""".strip()
    )
    from nightcool.config import load_config
    cfg = load_config(config_path)
    provider = MockWeatherProvider(_forecast(datetime(2024, 6, 15, 14, tzinfo=timezone.utc)))
    app = create_app(cfg, state, config_path=config_path, provider=provider)
    with TestClient(app) as c:
        r = c.post("/api/schedule/mon", json={"home_all_day": False})
        assert r.status_code == 200
    assert cfg.schedule.mon.leave_at == time(7, 30)
    import yaml
    raw = yaml.safe_load(config_path.read_text())
    assert raw["schedule"]["mon"]["leave_at"].startswith("07:30")


def test_post_geocode_resolves_and_persists(client):
    c, state, cfg, _ = client
    fake = GeocodeResult(latitude=39.74, longitude=-104.99, matched_address="Denver, CO")
    with patch("nightcool.web.app.do_geocode", return_value=fake):
        r = c.post("/api/geocode", json={"address": "Denver, CO"})
    assert r.status_code == 200
    data = r.json()
    assert data["latitude"] == 39.74
    assert cfg.location.latitude == 39.74
    assert cfg.location.address == "Denver, CO"


def test_subscribe_and_unsubscribe_roundtrip(client):
    c, state, *_ = client
    sub = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "x", "auth": "y"}}
    r1 = c.post("/api/subscribe", json=sub)
    assert r1.json()["added"] is True
    r2 = c.post("/api/subscribe", json=sub)
    assert r2.json()["added"] is False
    r3 = c.post("/api/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert r3.json()["removed"] is True


def test_vapid_public_404_when_unconfigured(client):
    c, *_ = client
    r = c.get("/api/vapid-public")
    assert r.status_code == 404


def test_savings_returns_null_model_when_no_data(client):
    c, *_ = client
    r = c.get("/api/savings")
    assert r.status_code == 200
    assert r.json()["model"] is None


def test_savings_counts_open_events_not_poll_rows(client):
    # The daemon logs "open" on every 15-minute poll while the criteria hold;
    # savings must be priced per open *event* (transition into "open"), not
    # per logged row.
    import random
    from nightcool.thermal import MIN_SAMPLES, Observation, connect, log_observation

    c, _, cfg, _ = client
    random.seed(0)
    conn = connect(Path(cfg.web.data_log_path))
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    indoor = 72.0
    n = MIN_SAMPLES + 40
    expected_events = 0
    prev_open = False
    for i in range(n):
        outdoor = 60.0 + 8.0 * math.sin(i / 6.0)
        windows_open = (i % 8) < 4
        hvac = (i % 20) < 5
        vent = (1.0 if windows_open else 0.0) * (outdoor - indoor)
        solar = 50.0 - outdoor
        indoor += (1.6 * vent + 0.1 * solar + (-5.0 if hvac else 0.0)) * 0.25 + random.gauss(0, 0.05)
        if windows_open and not prev_open:
            expected_events += 1
        prev_open = windows_open
        log_observation(conn, Observation(
            ts=base + timedelta(minutes=15 * i),
            indoor_f=indoor, outdoor_f=outdoor,
            wind_mph=0, rain_pct=0,
            action="open" if windows_open else "no_change",
            windows_open=windows_open, hvac_active=hvac,
        ))
    conn.close()

    r = c.get("/api/savings")
    assert r.status_code == 200
    data = r.json()
    assert data["model"] is not None
    assert data["open_events_counted"] == expected_events
    open_rows = sum(1 for i in range(n) if (i % 8) < 4)
    assert expected_events < open_rows  # the old per-row count would be ~4x this


def test_static_index_served(client):
    c, *_ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "NightCool" in r.text
