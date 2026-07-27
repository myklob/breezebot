"""Tests for the decision engine."""
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
    is_quiet_hours,
    should_notify,
)


# A weekday Saturday-equivalent base so most tests aren't fighting leave-time logic.
# 2024-06-15 is a Saturday → home_all_day in the default schedule, so CLOSE always fires.
BASE = datetime(2024, 6, 15, 20, 0, tzinfo=timezone.utc)


def make_hour(hours_ahead, temp_f, *, wind_dir=270.0, gusts=5.0, rain=0.0,
              dew_point_f=None, base=BASE):
    return HourlyForecast(
        timestamp=base + timedelta(hours=hours_ahead),
        temperature_f=temp_f,
        wind_speed_mph=5.0,
        wind_gust_mph=gusts,
        wind_direction_deg=wind_dir,
        rain_chance_pct=rain,
        dew_point_f=dew_point_f,
    )


def make_window(id="br_west", exposure=Exposure.EXPOSED, security=Security.SECURE, on_bad=False):
    return Window(id=id, name=id, exposure=exposure, security=security, on_bad_wind_sector=on_bad)


def _every_day(target_f=65.0):
    return DailySchedule(
        mon=DaySchedule(target_f=target_f, home_all_day=True),
        tue=DaySchedule(target_f=target_f, home_all_day=True),
        wed=DaySchedule(target_f=target_f, home_all_day=True),
        thu=DaySchedule(target_f=target_f, home_all_day=True),
        fri=DaySchedule(target_f=target_f, home_all_day=True),
        sat=DaySchedule(target_f=target_f, home_all_day=True),
        sun=DaySchedule(target_f=target_f, home_all_day=True),
    )


def _decide(indoor, forecast, windows, *, target_f=65.0, hysteresis=2.5,
            bad_sector=None, warn_rain=False, warn_gusts=False, floor=10.0,
            max_dew_point_f=None, max_rain=20.0, now=BASE):
    return decide_actions(
        indoor, forecast, windows, now,
        schedule=_every_day(target_f),
        prefs=Prefs(hysteresis_f=hysteresis),
        comfort_floor=ComfortFloor(min_indoor_f=floor),
        warnings=WarningPrefs(
            bad_wind_sector_deg=bad_sector,
            warn_on_rain=warn_rain,
            warn_on_gusts=warn_gusts,
            max_dew_point_f=max_dew_point_f,
            max_rain_chance_pct=max_rain,
        ),
    )


# ---- Core open/close gate ----

def test_cooling_opportunity_when_outdoor_below_indoor_minus_hysteresis():
    forecast = [make_hour(i, 62.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window()])
    assert rec.action == "open"
    assert rec.eligible_windows == ["br_west"]
    assert rec.open_at == forecast[0].timestamp


def test_no_recommendation_when_outdoor_below_min_tolerable():
    forecast = [make_hour(i, 45.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window()])
    assert rec.action == "no_change"


def test_no_open_when_outdoor_only_barely_below_target():
    # target=65, useful_max=70. Outdoor 71 exceeds it.
    forecast = [make_hour(i, 71.0) for i in range(12)]
    rec = _decide(80.0, forecast, [make_window()], target_f=65.0)
    assert rec.action != "open"


# ---- Window eligibility (now opt-in) ----

def test_rain_not_blocked_when_warning_off():
    forecast = [make_hour(i, 62.0, rain=80.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window(exposure=Exposure.EXPOSED)])
    assert rec.action == "open"
    assert rec.eligible_windows == ["br_west"]


def test_rain_blocks_exposed_when_warning_on():
    # With no eligible windows the engine falls through to no_change.
    forecast = [make_hour(i, 62.0, rain=80.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window(exposure=Exposure.EXPOSED)], warn_rain=True)
    assert rec.action == "no_change"


def test_tiled_bathroom_recommended_when_exposed_blocked_by_rain():
    forecast = [make_hour(i, 62.0, rain=80.0) for i in range(12)]
    windows = [
        make_window(id="exposed", exposure=Exposure.EXPOSED),
        make_window(id="bath", exposure=Exposure.TILED),
    ]
    rec = _decide(71.0, forecast, windows, warn_rain=True)
    assert rec.eligible_windows == ["bath"]


def test_bad_wind_sector_blocks_only_facing_windows_when_wind_is_outside_sector():
    forecast = [make_hour(i, 62.0, wind_dir=180.0) for i in range(12)]
    windows = [
        make_window(id="east_facing", on_bad=True),
        make_window(id="west_facing", on_bad=False),
    ]
    rec = _decide(71.0, forecast, windows, bad_sector=(60.0, 120.0))
    assert set(rec.eligible_windows) == {"east_facing", "west_facing"}


def test_bad_wind_direction_blocks_opening_entirely():
    forecast = [make_hour(i, 62.0, wind_dir=90.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window(on_bad=True)], bad_sector=(60.0, 120.0))
    assert rec.action != "open"


def test_bad_wind_in_sector_skips_only_flagged_windows():
    forecast = [make_hour(i, 62.0, wind_dir=90.0) for i in range(12)]
    windows = [
        make_window(id="east_facing", on_bad=True),
        make_window(id="west_facing", on_bad=False),
    ]
    rec = _decide(71.0, forecast, windows, bad_sector=(60.0, 120.0))
    assert rec.action == "open"
    assert rec.eligible_windows == ["west_facing"]


def test_covered_window_never_stricter_than_exposed_on_rain():
    forecast = [make_hour(i, 62.0, rain=65.0) for i in range(12)]
    windows = [
        make_window(id="exposed", exposure=Exposure.EXPOSED),
        make_window(id="covered", exposure=Exposure.COVERED),
    ]
    rec = _decide(71.0, forecast, windows, warn_rain=True, max_rain=70.0)
    assert set(rec.eligible_windows) == {"exposed", "covered"}


def test_unsecure_window_blocked_by_gusts_when_warning_on():
    forecast = [make_hour(i, 62.0, gusts=25.0) for i in range(12)]
    windows = [
        make_window(id="secure_w", security=Security.SECURE),
        make_window(id="unsecure_w", security=Security.UNSECURE),
    ]
    rec = _decide(71.0, forecast, windows, warn_gusts=True)
    assert rec.eligible_windows == ["secure_w"]


def test_unsecure_window_not_blocked_when_warning_off():
    forecast = [make_hour(i, 62.0, gusts=25.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window(security=Security.UNSECURE)])
    assert rec.action == "open"


# ---- Hysteresis ----

def test_hysteresis_prevents_flapping_one_degree_delta():
    forecast = [make_hour(i, 70.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window()])
    assert rec.action != "open"


def test_close_signal_when_outdoor_warm_enough():
    forecast = [make_hour(i, 75.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window()])
    assert rec.action == "close"


# ---- Sector wrap-around ----

def test_bad_sector_wraps_around_north():
    forecast = [make_hour(i, 62.0, wind_dir=5.0) for i in range(12)]
    rec = _decide(71.0, forecast, [make_window(on_bad=True)], bad_sector=(350.0, 10.0))
    assert rec.action != "open"


# ---- Quiet hours + dedup (notification layer) ----

def test_quiet_hours_suppress_open_but_not_close():
    prefs = Prefs(quiet_hours_start=time(22, 30), quiet_hours_end=time(6, 0))
    night = datetime(2024, 6, 15, 23, 30, tzinfo=timezone.utc)
    assert is_quiet_hours(night, prefs)
    open_rec = Recommendation("open", ["br_west"], night, night, "")
    close_rec = Recommendation("close", [], None, night, "")
    assert should_notify(open_rec, None, night, prefs=prefs, schedule=_every_day()) is False
    assert should_notify(close_rec, "open", night, prefs=prefs, schedule=_every_day()) is True


def test_dedup_same_action_twice_in_a_row_suppressed():
    day = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    open_rec = Recommendation("open", ["br_west"], day, day, "")
    assert should_notify(open_rec, None, day, schedule=_every_day()) is True
    assert should_notify(open_rec, "open", day, schedule=_every_day()) is False


def test_close_only_notifies_if_previously_opened():
    day = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    close_rec = Recommendation("close", [], None, day, "")
    assert should_notify(close_rec, None, day, schedule=_every_day()) is False
    assert should_notify(close_rec, "open", day, schedule=_every_day()) is True


def test_is_quiet_hours_non_wrapping_window():
    prefs = Prefs(quiet_hours_start=time(13, 0), quiet_hours_end=time(15, 0))
    assert is_quiet_hours(datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc), prefs)
    assert not is_quiet_hours(datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc), prefs)


# ---- Comfort floor ----

def test_open_close_at_pulled_in_when_floor_would_be_hit():
    # Cold outdoors → predicted indoor would crash through 65 floor quickly.
    forecast = [make_hour(i, 52.0) for i in range(12)]
    rec = _decide(75.0, forecast, [make_window()], floor=65.0)
    assert rec.action == "open"
    # close_at should be earlier than +12 hours
    assert rec.close_at is not None
    assert (rec.close_at - forecast[0].timestamp).total_seconds() < 8 * 3600
    assert rec.warnings  # we surface the floor reason


def test_open_suppressed_if_floor_overshot_immediately():
    # Floor exactly at indoor; first prediction step would already be below.
    forecast = [make_hour(i, 55.0) for i in range(12)]
    rec = _decide(72.0, forecast, [make_window()], floor=72.0)
    assert rec.action == "no_change"


# ---- Dew point gate ----

def test_dew_point_gate_blocks_muggy_open():
    # Cool 65 °F night, but a 70 °F dew point — air is saturated, not worth
    # letting in. With the gate set at 60 °F, every hour fails the check.
    forecast = [make_hour(i, 65.0, dew_point_f=70.0) for i in range(12)]
    rec = _decide(75.0, forecast, [make_window()], max_dew_point_f=60.0)
    assert rec.action != "open"


def test_dew_point_gate_allows_open_when_air_is_dry():
    # Same temps, dry 50 °F dew point — gate set at 60 °F, should still open.
    forecast = [make_hour(i, 65.0, dew_point_f=50.0) for i in range(12)]
    rec = _decide(75.0, forecast, [make_window()], max_dew_point_f=60.0)
    assert rec.action == "open"


def test_dew_point_gate_disabled_by_default_ignores_dew_point():
    # Even a muggy 70 °F dew point shouldn't block when the gate is unset.
    forecast = [make_hour(i, 65.0, dew_point_f=70.0) for i in range(12)]
    rec = _decide(75.0, forecast, [make_window()])
    assert rec.action == "open"


def test_dew_point_gate_passes_through_unknown_dew_point():
    # Provider returned no dew point — don't block on absent data.
    forecast = [make_hour(i, 65.0, dew_point_f=None) for i in range(12)]
    rec = _decide(75.0, forecast, [make_window()], max_dew_point_f=60.0)
    assert rec.action == "open"
