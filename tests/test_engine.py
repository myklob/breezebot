"""Tests for the decision engine.

Covers the gate logic exhaustively because that's where bugs hide. Notification
plumbing (dedup, quiet hours) is exercised against `should_notify`.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from nightcool.config import Exposure, Security, UserPrefs, Window
from nightcool.engine import (
    HourlyForecast,
    Recommendation,
    decide_actions,
    is_quiet_hours,
    should_notify,
)


BASE = datetime(2024, 6, 15, 20, 0, tzinfo=timezone.utc)


def make_hour(
    hours_ahead: int,
    temp_f: float,
    *,
    wind_dir: float = 270.0,
    gusts: float = 5.0,
    rain: float = 0.0,
    base: datetime = BASE,
) -> HourlyForecast:
    return HourlyForecast(
        timestamp=base + timedelta(hours=hours_ahead),
        temperature_f=temp_f,
        wind_speed_mph=5.0,
        wind_gust_mph=gusts,
        wind_direction_deg=wind_dir,
        rain_chance_pct=rain,
    )


def make_window(
    id: str = "br_west",
    exposure: Exposure = Exposure.EXPOSED,
    security: Security = Security.SECURE,
    on_bad: bool = False,
) -> Window:
    return Window(id=id, name=id, exposure=exposure, security=security, on_bad_wind_sector=on_bad)


def default_prefs(**overrides) -> UserPrefs:
    base = dict(
        sleep_target_f=65.0,
        min_tolerable_outdoor_f=50.0,
        hysteresis_f=2.5,
        max_gust_mph=18.0,
        max_rain_chance_pct=20.0,
        bad_wind_sector_deg=(60.0, 120.0),
        quiet_hours_start=time(22, 30),
        quiet_hours_end=time(6, 0),
        morning_close_time=time(6, 30),
    )
    base.update(overrides)
    return UserPrefs(**base)


# ---- Core open/close gate ----

def test_cooling_opportunity_when_outdoor_below_indoor_minus_hysteresis():
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action == "open"
    assert rec.eligible_windows == ["br_west"]
    assert rec.open_at == forecast[0].timestamp
    assert rec.close_at is not None


def test_no_recommendation_when_outdoor_below_min_tolerable():
    forecast = [make_hour(i, 45.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action == "no_change"


def test_no_open_when_outdoor_only_barely_below_sleep_target():
    # sleep_target=65, sleep+5=70. Outdoor 70 hits the ceiling exactly, so
    # we need a value that exceeds it.
    forecast = [make_hour(i, 71.0) for i in range(12)]
    # Indoor 80, hyst 2.5: open_threshold=77.5, so 71 IS cool enough by threshold,
    # but useful_max=70 blocks it.
    rec = decide_actions(80.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action != "open"


# ---- Window eligibility ----

def test_tiled_bathroom_recommended_when_exposed_blocked_by_rain():
    forecast = [make_hour(i, 62.0, rain=80.0) for i in range(12)]
    windows = [
        make_window(id="exposed", exposure=Exposure.EXPOSED),
        make_window(id="bath", exposure=Exposure.TILED),
    ]
    rec = decide_actions(71.0, forecast, windows, default_prefs(), BASE)
    assert rec.action == "open"
    assert rec.eligible_windows == ["bath"]


def test_covered_window_tolerates_light_rain_but_not_heavy():
    light_rain = [make_hour(i, 62.0, rain=40.0) for i in range(12)]
    heavy_rain = [make_hour(i, 62.0, rain=80.0) for i in range(12)]
    win = make_window(id="covered", exposure=Exposure.COVERED)
    rec_light = decide_actions(71.0, light_rain, [win], default_prefs(), BASE)
    rec_heavy = decide_actions(71.0, heavy_rain, [win], default_prefs(), BASE)
    assert rec_light.eligible_windows == ["covered"]
    assert rec_heavy.eligible_windows == []


def test_bad_wind_sector_blocks_only_facing_windows_when_wind_is_outside_sector():
    # Wind from due south (180) — not in bad sector [60, 120].
    forecast = [make_hour(i, 62.0, wind_dir=180.0) for i in range(12)]
    windows = [
        make_window(id="east_facing", on_bad=True),
        make_window(id="west_facing", on_bad=False),
    ]
    rec = decide_actions(71.0, forecast, windows, default_prefs(), BASE)
    assert set(rec.eligible_windows) == {"east_facing", "west_facing"}


def test_bad_wind_direction_blocks_opening_entirely():
    # Wind from east (90°), squarely in bad sector → no open recommendation at all.
    forecast = [make_hour(i, 62.0, wind_dir=90.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action != "open"


def test_unsecure_window_blocked_by_gusts_secure_window_still_open():
    forecast = [make_hour(i, 62.0, wind_dir=270.0, gusts=25.0) for i in range(12)]
    windows = [
        make_window(id="secure_w", security=Security.SECURE),
        make_window(id="unsecure_w", security=Security.UNSECURE),
    ]
    rec = decide_actions(71.0, forecast, windows, default_prefs(), BASE)
    assert rec.action == "open"
    assert rec.eligible_windows == ["secure_w"]


# ---- Hysteresis ----

def test_hysteresis_prevents_flapping_one_degree_delta():
    # Indoor 71, hysteresis 2.5: need outdoor ≤ 68.5 to qualify. 70 should NOT trigger open.
    forecast = [make_hour(i, 70.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action != "open"


def test_close_signal_when_outdoor_warm_enough():
    forecast = [make_hour(i, 75.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], default_prefs(), BASE)
    assert rec.action == "close"


# ---- Sector wrap-around ----

def test_bad_sector_wraps_around_north():
    prefs = default_prefs(bad_wind_sector_deg=(350.0, 10.0))
    # Wind direction 5° (NNE-ish) should be inside the wrap-around sector.
    forecast = [make_hour(i, 62.0, wind_dir=5.0) for i in range(12)]
    rec = decide_actions(71.0, forecast, [make_window()], prefs, BASE)
    assert rec.action != "open"


# ---- Quiet hours + dedup (notification layer) ----

def test_quiet_hours_suppress_open_but_not_close():
    prefs = default_prefs()
    # 23:30 falls inside [22:30, 06:00) overnight window.
    night = datetime(2024, 6, 15, 23, 30, tzinfo=timezone.utc)
    assert is_quiet_hours(night, prefs)

    open_rec = Recommendation("open", ["br_west"], night, night, "")
    close_rec = Recommendation("close", [], None, night, "")

    assert should_notify(open_rec, None, night, prefs) is False
    assert should_notify(close_rec, "open", night, prefs) is True


def test_dedup_same_action_twice_in_a_row_suppressed():
    prefs = default_prefs()
    day = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    open_rec = Recommendation("open", ["br_west"], day, day, "")
    assert should_notify(open_rec, None, day, prefs) is True
    assert should_notify(open_rec, "open", day, prefs) is False


def test_close_only_notifies_if_previously_opened():
    prefs = default_prefs()
    day = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    close_rec = Recommendation("close", [], None, day, "")
    assert should_notify(close_rec, None, day, prefs) is False
    assert should_notify(close_rec, "open", day, prefs) is True


def test_is_quiet_hours_non_wrapping_window():
    prefs = default_prefs(quiet_hours_start=time(13, 0), quiet_hours_end=time(15, 0))
    assert is_quiet_hours(datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc), prefs)
    assert not is_quiet_hours(datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc), prefs)
