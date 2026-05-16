"""Tests for daily schedule + leave-time aware notifications."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

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
from nightcool.engine import (
    HourlyForecast,
    Recommendation,
    decide_actions,
    predict_indoor_path,
    should_notify,
    summarize_missed_opportunity,
)


def make_hour(i, t, base):
    return HourlyForecast(
        timestamp=base + timedelta(hours=i),
        temperature_f=t,
        wind_speed_mph=5.0,
        wind_gust_mph=5.0,
        wind_direction_deg=270.0,
        rain_chance_pct=0.0,
    )


def _commuter_schedule(target=68.0, leave_at=time(7, 30)):
    weekday = DaySchedule(target_f=target, leave_at=leave_at)
    weekend = DaySchedule(target_f=target, home_all_day=True)
    return DailySchedule(
        mon=weekday, tue=weekday, wed=weekday, thu=weekday, fri=weekday,
        sat=weekend, sun=weekend,
    )


def _window() -> Window:
    return Window(id="w", name="W", exposure=Exposure.EXPOSED, security=Security.SECURE)


# ---- leave_at suppresses weekday-morning close ----

def test_weekday_morning_close_suppressed_before_leave_at():
    sched = _commuter_schedule()
    monday_6am = datetime(2024, 6, 17, 6, 0)  # Monday, before 07:30.
    close_rec = Recommendation("close", [], None, monday_6am, "warmed")
    assert should_notify(close_rec, "open", monday_6am, schedule=sched) is False


def test_weekday_morning_close_fires_after_leave_at():
    sched = _commuter_schedule()
    monday_9am = datetime(2024, 6, 17, 9, 0)
    close_rec = Recommendation("close", [], None, monday_9am, "warmed")
    assert should_notify(close_rec, "open", monday_9am, schedule=sched) is True


def test_weekend_close_always_fires():
    sched = _commuter_schedule()
    sat_morning = datetime(2024, 6, 15, 9, 30)  # Saturday — home_all_day.
    close_rec = Recommendation("close", [], None, sat_morning, "warmed")
    assert should_notify(close_rec, "open", sat_morning, schedule=sched) is True


def test_home_all_day_does_not_suppress_close():
    sched = _commuter_schedule()
    sat_early = datetime(2024, 6, 15, 5, 0)  # Saturday very early.
    close_rec = Recommendation("close", [], None, sat_early, "warmed")
    assert should_notify(close_rec, "open", sat_early, schedule=sched) is True


# ---- per-day target_f is honored ----

def test_target_temperature_uses_today():
    # Lower target on Saturday → useful_max=70 ceiling. Outdoor 72 too warm.
    sched = _commuter_schedule(target=65.0)
    sat_evening = datetime(2024, 6, 15, 20, 0)
    forecast = [make_hour(i, 72.0, sat_evening) for i in range(12)]
    rec = decide_actions(
        80.0, forecast, [_window()], sat_evening,
        schedule=sched, prefs=Prefs(),
        comfort_floor=ComfortFloor(min_indoor_f=10.0),
        warnings=WarningPrefs(),
    )
    assert rec.action != "open"


# ---- predict_indoor_path is sane ----

def test_predict_indoor_decays_toward_outdoor():
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    forecast = [make_hour(i, 55.0, base) for i in range(12)]
    path = predict_indoor_path(75.0, forecast, alpha_per_hr=1.0)
    # Should be monotonically decreasing toward 55.
    temps = [t for _, t in path]
    assert temps == sorted(temps, reverse=True)
    assert temps[-1] < 60.0  # well on the way to outdoor.


# ---- summary surfaces a missed cooling window ----

def test_summary_lists_overnight_lows():
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    forecast = [make_hour(i, t, base) for i, t in enumerate([72, 70, 65, 60, 58, 60, 65, 68])]
    summary = summarize_missed_opportunity(forecast, 75.0, Prefs(), 65.0, WarningPrefs())
    assert summary is not None
    assert summary.action == "summary"
    assert "58.0°F" in summary.reason


def test_summary_returns_none_when_no_cool_hours():
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    forecast = [make_hour(i, 80.0, base) for i in range(8)]
    assert summarize_missed_opportunity(forecast, 75.0, Prefs(), 65.0, WarningPrefs()) is None
