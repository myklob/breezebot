"""Tests for schedule profile presets and their effect on notifications."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from nightcool.config import (
    PROFILE_PRESETS,
    AppConfig,
    Exposure,
    IndoorTempConfig,
    Location,
    NotificationConfig,
    ProfileName,
    Security,
    UserPrefs,
    Window,
)
from nightcool.engine import (
    HourlyForecast,
    Recommendation,
    decide_actions,
    should_notify,
    summarize_missed_opportunity,
)


BASE = datetime(2024, 6, 17, 5, 0, tzinfo=timezone.utc)  # A weekday Monday 05:00 UTC.


def make_hour(i: int, temp: float, base: datetime = BASE) -> HourlyForecast:
    return HourlyForecast(
        timestamp=base + timedelta(hours=i),
        temperature_f=temp,
        wind_speed_mph=5.0,
        wind_gust_mph=5.0,
        wind_direction_deg=270.0,
        rain_chance_pct=0.0,
    )


def _prefs(profile: ProfileName, **kw) -> UserPrefs:
    base = dict(
        sleep_target_f=65.0,
        min_tolerable_outdoor_f=50.0,
        hysteresis_f=2.5,
        bad_wind_sector_deg=(60.0, 120.0),
        quiet_hours_start=time(22, 30),
        quiet_hours_end=time(6, 0),
        morning_close_time=time(6, 30),
        profile=profile,
    )
    base.update(kw)
    return UserPrefs(**base)


def test_commuter_suppresses_weekday_morning_close():
    prefs = _prefs(ProfileName.COMMUTER)
    profile = prefs.resolved_profile()
    # Monday 06:00 local: weekday, before morning_close_time 06:30.
    weekday_morning = datetime(2024, 6, 17, 6, 0)
    close_rec = Recommendation("close", [], None, weekday_morning, "warmed")
    assert should_notify(close_rec, "open", weekday_morning, prefs, profile=profile) is False


def test_commuter_still_pings_close_on_weekend_morning():
    prefs = _prefs(ProfileName.COMMUTER)
    profile = prefs.resolved_profile()
    sat_morning = datetime(2024, 6, 15, 9, 30)  # Saturday — should ping.
    close_rec = Recommendation("close", [], None, sat_morning, "warmed")
    assert should_notify(close_rec, "open", sat_morning, prefs, profile=profile) is True


def test_wfh_still_gets_weekday_morning_close():
    prefs = _prefs(ProfileName.WFH)
    profile = prefs.resolved_profile()
    weekday_morning = datetime(2024, 6, 17, 6, 0)
    close_rec = Recommendation("close", [], None, weekday_morning, "warmed")
    assert should_notify(close_rec, "open", weekday_morning, prefs, profile=profile) is True


def test_light_sleeper_summary_lists_overnight_lows():
    prefs = _prefs(ProfileName.LIGHT_SLEEPER)
    # Indoor 75, forecast dips to 58 → that's a missed opportunity.
    forecast = [make_hour(i, t) for i, t in enumerate([72, 70, 65, 60, 58, 60, 65, 68])]
    summary = summarize_missed_opportunity(forecast, 75.0, prefs)
    assert summary is not None
    assert summary.action == "summary"
    assert "58.0°F" in summary.reason


def test_conservative_requires_sustained_hours():
    prefs = _prefs(ProfileName.CONSERVATIVE, hysteresis_f=2.5)
    profile = prefs.resolved_profile()
    # Only one hour cool, then it warms — sustained=3 requires 3 in a row.
    forecast = [make_hour(0, 60.0), make_hour(1, 75.0), make_hour(2, 75.0)]
    rec = decide_actions(71.0, forecast, [_window()], prefs, BASE, profile=profile)
    assert rec.action != "open"

    # Three consecutive cool hours qualify.
    forecast2 = [make_hour(i, 60.0) for i in range(3)] + [make_hour(i, 75.0) for i in range(3, 6)]
    rec2 = decide_actions(71.0, forecast2, [_window()], prefs, BASE, profile=profile)
    assert rec2.action == "open"


def test_aggressive_hysteresis_override_applied():
    cfg = AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="America/Denver"),
        user_prefs=UserPrefs(
            sleep_target_f=65.0,
            min_tolerable_outdoor_f=50.0,
            profile=ProfileName.AGGRESSIVE,
        ),
        indoor_temp=IndoorTempConfig(source="manual", manual_default_f=71.0),
        windows=[_window()],
        notifications=NotificationConfig(service="console"),
    )
    effective = cfg.effective_prefs()
    assert effective.hysteresis_f == 1.0  # Aggressive preset shrinks hysteresis.


def test_user_explicit_hysteresis_overrides_preset():
    cfg = AppConfig(
        location=Location(latitude=39.0, longitude=-104.0, timezone="America/Denver"),
        user_prefs=UserPrefs(
            sleep_target_f=65.0,
            min_tolerable_outdoor_f=50.0,
            hysteresis_f=3.0,  # Explicitly set — preset must not clobber it.
            profile=ProfileName.AGGRESSIVE,
        ),
        indoor_temp=IndoorTempConfig(source="manual", manual_default_f=71.0),
        windows=[_window()],
        notifications=NotificationConfig(service="console"),
    )
    assert cfg.effective_prefs().hysteresis_f == 3.0


def test_all_preset_names_resolve():
    # Defends against typos in the preset table.
    for name in ProfileName:
        assert PROFILE_PRESETS[name].name == name


def _window() -> Window:
    return Window(id="br_west", name="Master West", exposure=Exposure.EXPOSED, security=Security.SECURE)
