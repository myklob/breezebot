"""FastAPI HTTP layer: state, indoor temp updates, profile changes, subscriptions."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nightcool.config import (
    AppConfig,
    Exposure,
    IndoorTempConfig,
    Location,
    NotificationConfig,
    ProfileName,
    Security,
    UserPrefs,
    WebServerConfig,
    Window,
)
from nightcool.engine import HourlyForecast
from nightcool.weather import MockWeatherProvider
from nightcool.web.app import create_app


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="UTC"),
        user_prefs=UserPrefs(
            sleep_target_f=65.0,
            min_tolerable_outdoor_f=50.0,
            hysteresis_f=2.5,
            profile=ProfileName.COMMUTER,
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
        yield c, state, tmp_path


def test_get_state_returns_recommendation(client):
    c, _, _ = client
    r = c.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["indoor_f"] == 71.0
    assert data["profile"] == "commuter"
    assert data["recommendation"]["action"] in ("open", "close", "no_change", "summary")
    assert len(data["forecast_head"]) == 6


def test_post_indoor_temp_persists(client):
    c, state, _ = client
    r = c.post("/api/indoor-temp", json={"temperature_f": 68.5})
    assert r.status_code == 200
    assert json.loads(state.read_text())["indoor_temp_f"] == 68.5


def test_post_profile_without_config_path_refused(client):
    c, _, _ = client
    r = c.post("/api/profile", json={"profile": "wfh"})
    assert r.status_code == 409


def test_post_profile_with_config_path_rewrites_yaml(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
location: {latitude: 39, longitude: -104, timezone: UTC}
user_prefs: {sleep_target_f: 65, min_tolerable_outdoor_f: 50, profile: commuter}
indoor_temp: {source: manual, manual_default_f: 71}
windows: [{id: w, name: W, exposure: exposed, security: secure}]
notifications: {service: console}
""".strip()
    )
    from nightcool.config import load_config
    cfg = load_config(config_path)
    provider = MockWeatherProvider(_forecast(datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)))
    app = create_app(cfg, state, config_path=config_path, provider=provider)
    with TestClient(app) as c:
        r = c.post("/api/profile", json={"profile": "wfh"})
        assert r.status_code == 200
        assert r.json()["profile"] == "wfh"
    import yaml
    raw = yaml.safe_load(config_path.read_text())
    assert raw["user_prefs"]["profile"] == "wfh"


def test_subscribe_and_unsubscribe_roundtrip(client):
    c, state, _ = client
    sub = {
        "endpoint": "https://push.example.com/abc",
        "keys": {"p256dh": "x", "auth": "y"},
    }
    r1 = c.post("/api/subscribe", json=sub)
    assert r1.status_code == 200
    assert r1.json()["added"] is True

    # Duplicate post is idempotent.
    r2 = c.post("/api/subscribe", json=sub)
    assert r2.json()["added"] is False

    stored = json.loads(state.read_text())
    assert len(stored["push_subscriptions"]) == 1

    r3 = c.post("/api/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert r3.json()["removed"] is True
    stored = json.loads(state.read_text())
    assert stored["push_subscriptions"] == []


def test_vapid_public_404_when_unconfigured(client):
    c, _, _ = client
    r = c.get("/api/vapid-public")
    assert r.status_code == 404


def test_savings_returns_null_model_when_no_data(client):
    c, _, _ = client
    r = c.get("/api/savings")
    assert r.status_code == 200
    data = r.json()
    assert data["model"] is None


def test_static_index_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "NightCool" in r.text
