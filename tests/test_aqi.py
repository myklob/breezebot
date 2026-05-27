"""Tests for the AQI provider and engine AQI gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from nightcool.aqi import (
    AirNowProvider,
    CachedAQIProvider,
    MockAQIProvider,
    PurpleAirProvider,
    _pm25_to_aqi,
)
from nightcool.config import (
    ComfortFloor,
    DailySchedule,
    DaySchedule,
    Exposure,
    Prefs,
    Security,
    WarningPrefs,
    Window,
)
from nightcool.engine import HourlyForecast, decide_actions


BASE = datetime(2024, 6, 15, 20, 0, tzinfo=timezone.utc)


def make_hour(hours_ahead: int, temp_f: float) -> HourlyForecast:
    return HourlyForecast(
        timestamp=BASE + timedelta(hours=hours_ahead),
        temperature_f=temp_f,
        wind_speed_mph=5.0,
        wind_gust_mph=5.0,
        wind_direction_deg=270.0,
        rain_chance_pct=0.0,
    )


def make_window() -> Window:
    return Window(id="br_west", name="West", exposure=Exposure.EXPOSED, security=Security.SECURE)


def _every_day(target_f: float = 65.0) -> DailySchedule:
    day = DaySchedule(target_f=target_f, home_all_day=True)
    return DailySchedule(
        mon=day, tue=day, wed=day, thu=day, fri=day, sat=day, sun=day,
    )


# ---- Engine AQI gate ----

def test_aqi_gate_blocks_open_when_aqi_exceeds_threshold():
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(
        75.0, forecast, [make_window()], BASE,
        schedule=_every_day(),
        prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(max_aqi=100),
        current_aqi=150,
    )
    assert rec.action == "no_change"
    assert "AQI" in rec.reason


def test_aqi_gate_allows_open_when_aqi_at_threshold():
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(
        75.0, forecast, [make_window()], BASE,
        schedule=_every_day(),
        prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(max_aqi=100),
        current_aqi=100,
    )
    assert rec.action == "open"


def test_aqi_gate_allows_open_when_aqi_below_threshold():
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(
        75.0, forecast, [make_window()], BASE,
        schedule=_every_day(),
        prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(max_aqi=100),
        current_aqi=42,
    )
    assert rec.action == "open"


def test_aqi_gate_disabled_when_max_aqi_is_none():
    # Even wildfire-smoke AQI should not block when the gate is unset.
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(
        75.0, forecast, [make_window()], BASE,
        schedule=_every_day(),
        prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(max_aqi=None),
        current_aqi=300,
    )
    assert rec.action == "open"


def test_aqi_gate_passes_through_when_current_aqi_is_none():
    # Provider not configured or fetch failed — don't block on missing data.
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(
        75.0, forecast, [make_window()], BASE,
        schedule=_every_day(),
        prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(max_aqi=50),
        current_aqi=None,
    )
    assert rec.action == "open"


# ---- MockAQIProvider ----

def test_mock_aqi_provider_returns_configured_value():
    provider = MockAQIProvider(aqi=42)
    assert provider.current_aqi(39.9, -75.1) == 42


def test_mock_aqi_provider_returns_none():
    provider = MockAQIProvider(aqi=None)
    assert provider.current_aqi(39.9, -75.1) is None


# ---- CachedAQIProvider ----

def test_cached_provider_calls_underlying_once_within_cache_window():
    inner = MockAQIProvider(aqi=55)
    inner.current_aqi = MagicMock(return_value=55)
    cached = CachedAQIProvider(inner, cache_minutes=30)

    cached.current_aqi(39.9, -75.1)
    cached.current_aqi(39.9, -75.1)

    assert inner.current_aqi.call_count == 1


# ---- AirNow response parsing ----

def test_airnow_returns_highest_aqi_across_parameters():
    mock_response = [
        {"ParameterName": "PM2.5", "AQI": 42},
        {"ParameterName": "O3", "AQI": 75},
    ]
    client = MagicMock()
    client.get.return_value.json.return_value = mock_response
    client.get.return_value.raise_for_status = MagicMock()

    provider = AirNowProvider("TEST_KEY", client=client)
    assert provider.current_aqi(39.9, -75.1) == 75


def test_airnow_returns_none_on_empty_response():
    client = MagicMock()
    client.get.return_value.json.return_value = []
    client.get.return_value.raise_for_status = MagicMock()

    provider = AirNowProvider("TEST_KEY", client=client)
    assert provider.current_aqi(39.9, -75.1) is None


def test_airnow_returns_none_on_http_error():
    client = MagicMock()
    client.get.side_effect = Exception("network error")

    provider = AirNowProvider("TEST_KEY", client=client)
    assert provider.current_aqi(39.9, -75.1) is None


# ---- PurpleAir response parsing ----

def test_purpleair_averages_pm25_and_converts_to_aqi():
    mock_response = {
        "fields": ["pm2.5_atm"],
        "data": [[12.0], [0.0]],  # avg = 6.0 µg/m³ → AQI 25
    }
    client = MagicMock()
    client.get.return_value.json.return_value = mock_response
    client.get.return_value.raise_for_status = MagicMock()

    provider = PurpleAirProvider("TEST_KEY", client=client)
    result = provider.current_aqi(39.9, -75.1)
    assert result == 25


def test_purpleair_returns_none_when_no_sensors():
    mock_response = {"fields": ["pm2.5_atm"], "data": []}
    client = MagicMock()
    client.get.return_value.json.return_value = mock_response
    client.get.return_value.raise_for_status = MagicMock()

    provider = PurpleAirProvider("TEST_KEY", client=client)
    assert provider.current_aqi(39.9, -75.1) is None


# ---- _pm25_to_aqi boundary values ----

def test_pm25_to_aqi_boundary_zero():
    assert _pm25_to_aqi(0.0) == 0


def test_pm25_to_aqi_boundary_good_ceiling():
    assert _pm25_to_aqi(12.0) == 50


def test_pm25_to_aqi_boundary_moderate_ceiling():
    assert _pm25_to_aqi(35.4) == 100


def test_pm25_to_aqi_offscale_high():
    assert _pm25_to_aqi(600.0) == 500
