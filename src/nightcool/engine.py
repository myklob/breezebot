"""Pure decision logic for opening/closing windows.

The engine is a single pure function: given current indoor temp, an hourly
forecast, the window list, and user prefs, it returns one Recommendation.
No I/O, no globals; trivial to test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from .config import Exposure, ScheduleProfile, Security, UserPrefs, Window


# Covered windows tolerate heavier rain than exposed ones; tiled ones ignore rain.
COVERED_RAIN_MAX_PCT = 60.0
# Don't pester the user about an opportunity more than this far in advance.
OPEN_LOOKAHEAD_HOURS = 2
# Opening to air that's barely under the sleep target is wasted effort.
USEFUL_DELTA_OVER_TARGET_F = 5.0


Action = Literal["open", "close", "no_change", "summary"]


@dataclass(frozen=True)
class HourlyForecast:
    """One hour of weather, normalized across providers."""

    timestamp: datetime
    temperature_f: float
    wind_speed_mph: float
    wind_gust_mph: float
    wind_direction_deg: float
    rain_chance_pct: float


@dataclass(frozen=True)
class Recommendation:
    """Engine output: what to tell the user, if anything."""

    action: Action
    eligible_windows: list[str]
    open_at: datetime | None
    close_at: datetime | None
    reason: str


def _in_bad_sector(direction_deg: float, sector: tuple[float, float] | None) -> bool:
    """Return True if `direction_deg` is inside the user's bad-wind arc.

    Supports wrap-around: a sector of (350, 10) covers 350..359 and 0..10.
    """
    if sector is None:
        return False
    lo, hi = sector
    if lo <= hi:
        return lo <= direction_deg <= hi
    return direction_deg >= lo or direction_deg <= hi


def _window_eligible(window: Window, hour: HourlyForecast, prefs: UserPrefs) -> bool:
    """Return True if `window` is safe to open at `hour`."""
    if window.on_bad_wind_sector and _in_bad_sector(hour.wind_direction_deg, prefs.bad_wind_sector_deg):
        return False
    if window.exposure == Exposure.EXPOSED and hour.rain_chance_pct > prefs.max_rain_chance_pct:
        return False
    if window.exposure == Exposure.COVERED and hour.rain_chance_pct > COVERED_RAIN_MAX_PCT:
        return False
    # Tiled (bathroom) windows are exempt from rain checks entirely.
    if window.security == Security.UNSECURE and hour.wind_gust_mph > prefs.max_gust_mph:
        return False
    return True


def _hour_passes_open_criteria(
    hour: HourlyForecast, indoor_temp_f: float, prefs: UserPrefs, useful_max: float
) -> bool:
    open_threshold = indoor_temp_f - prefs.hysteresis_f
    return (
        hour.temperature_f <= open_threshold
        and hour.temperature_f >= prefs.min_tolerable_outdoor_f
        and hour.temperature_f <= useful_max
        and not _in_bad_sector(hour.wind_direction_deg, prefs.bad_wind_sector_deg)
    )


def _find_open_moment(
    forecast: list[HourlyForecast],
    indoor_temp_f: float,
    prefs: UserPrefs,
    profile: ScheduleProfile,
) -> HourlyForecast | None:
    """First hour in the forecast that meets the global open criteria and is
    sustained for `profile.sustained_hours_required` consecutive hours."""
    useful_max = prefs.sleep_target_f + USEFUL_DELTA_OVER_TARGET_F
    streak: list[HourlyForecast] = []
    for hour in forecast:
        if _hour_passes_open_criteria(hour, indoor_temp_f, prefs, useful_max):
            streak.append(hour)
            if len(streak) >= profile.sustained_hours_required:
                return streak[0]
        else:
            streak = []
    return None


def _next_local_time(after: datetime, target: time) -> datetime:
    """Next occurrence of wall-clock `target` strictly after `after`."""
    candidate = after.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _find_close_moment(
    forecast: list[HourlyForecast],
    open_moment: HourlyForecast,
    indoor_temp_f: float,
    prefs: UserPrefs,
) -> datetime:
    """Earliest of: outdoor crossing back above (indoor - hysteresis), or morning_close_time."""
    threshold = indoor_temp_f - prefs.hysteresis_f
    open_idx = forecast.index(open_moment)
    warmup_at: datetime | None = None
    for hour in forecast[open_idx + 1:]:
        if hour.temperature_f >= threshold:
            warmup_at = hour.timestamp
            break
    morning_close = _next_local_time(open_moment.timestamp, prefs.morning_close_time)
    if warmup_at is None:
        return morning_close
    return min(warmup_at, morning_close)


def is_quiet_hours(now: datetime, prefs: UserPrefs) -> bool:
    """True if `now` (local wall clock) falls between quiet_hours_start and quiet_hours_end."""
    t = now.time()
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _is_weekend(now: datetime) -> bool:
    """Saturday or Sunday in `now`'s local wall-clock."""
    return now.weekday() >= 5


def _is_before_morning_close(now: datetime, prefs: UserPrefs) -> bool:
    """`now` is in the early-morning window before morning_close_time."""
    return now.time() < prefs.morning_close_time


def decide_actions(
    indoor_temp_f: float,
    hourly_forecast: list[HourlyForecast],
    windows: list[Window],
    prefs: UserPrefs,
    now: datetime,
    profile: ScheduleProfile | None = None,
) -> Recommendation:
    """Decide whether to open, close, or do nothing.

    Pure function. No I/O, no globals. The caller (daemon) handles
    notification dedup and quiet-hours suppression via `should_notify`.
    """
    profile = profile or prefs.resolved_profile()

    if not hourly_forecast:
        return Recommendation("no_change", [], None, None, "No forecast available.")

    open_threshold = indoor_temp_f - prefs.hysteresis_f
    current = hourly_forecast[0]

    open_moment = _find_open_moment(hourly_forecast, indoor_temp_f, prefs, profile)
    if open_moment is not None:
        eligible = [w for w in windows if _window_eligible(w, open_moment, prefs)]
        starts_in = open_moment.timestamp - now
        if eligible and starts_in <= timedelta(hours=OPEN_LOOKAHEAD_HOURS):
            close_at = _find_close_moment(hourly_forecast, open_moment, indoor_temp_f, prefs)
            return Recommendation(
                action="open",
                eligible_windows=[w.id for w in eligible],
                open_at=open_moment.timestamp,
                close_at=close_at,
                reason=(
                    f"Outdoor {open_moment.temperature_f:.1f}°F at "
                    f"{open_moment.timestamp:%H:%M}, indoor {indoor_temp_f:.1f}°F. "
                    f"Close by {close_at:%H:%M}."
                ),
            )

    # Outdoor warmer than the open threshold: signal CLOSE. The daemon will
    # only forward this to the user if we previously told them to OPEN.
    if current.temperature_f >= open_threshold:
        return Recommendation(
            action="close",
            eligible_windows=[],
            open_at=None,
            close_at=now,
            reason=(
                f"Outdoor warmed to {current.temperature_f:.1f}°F "
                f"(threshold {open_threshold:.1f}°F). Trap the cool air."
            ),
        )

    return Recommendation("no_change", [], None, None, "No action needed.")


def should_notify(
    rec: Recommendation,
    last_action: Action | None,
    now: datetime,
    prefs: UserPrefs,
    profile: ScheduleProfile | None = None,
) -> bool:
    """Apply dedup + quiet-hours + profile rules to an engine recommendation.

    - Never notify the same action twice in a row (dedup).
    - Quiet hours suppress OPEN but NOT CLOSE (CLOSE can wake you to save cool air).
    - CLOSE only fires if the user was previously told to OPEN.
    - Profile-aware: commuter skips weekday-morning CLOSE; light-sleeper
      defers overnight OPENs to the morning summary.
    """
    profile = profile or prefs.resolved_profile()

    if rec.action in ("no_change", "summary"):
        return False
    if rec.action == last_action:
        return False
    if rec.action == "open":
        if is_quiet_hours(now, prefs):
            # Light sleepers want quiet-hours opens queued for a morning summary.
            return False
        return True
    if rec.action == "close":
        if last_action != "open":
            return False
        # Commuter: weekday morning before close-time → they handle it themselves.
        if (
            not profile.weekday_morning_close_notify
            and not _is_weekend(now)
            and _is_before_morning_close(now, prefs)
        ):
            return False
        if not profile.weekend_close_notify and _is_weekend(now):
            return False
        return True
    return False


def summarize_missed_opportunity(
    forecast: list[HourlyForecast],
    indoor_temp_f: float,
    prefs: UserPrefs,
) -> Recommendation | None:
    """Build a morning-summary 'you slept through a cooling window' note.

    Light-sleeper users prefer this to a 2am ping. The summary covers any
    forecast hour that *would have* met the open criteria. Returns None when
    nothing notable happened.
    """
    useful_max = prefs.sleep_target_f + USEFUL_DELTA_OVER_TARGET_F
    cool_hours = [
        h for h in forecast
        if _hour_passes_open_criteria(h, indoor_temp_f, prefs, useful_max)
    ]
    if not cool_hours:
        return None
    coolest = min(cool_hours, key=lambda h: h.temperature_f)
    delta = indoor_temp_f - coolest.temperature_f
    return Recommendation(
        action="summary",
        eligible_windows=[],
        open_at=coolest.timestamp,
        close_at=None,
        reason=(
            f"Overnight low {coolest.temperature_f:.1f}°F at "
            f"{coolest.timestamp:%H:%M} — {delta:.1f}°F of free cooling "
            f"if windows had been open."
        ),
    )
