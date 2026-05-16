"""Pure decision logic for opening/closing windows.

The engine is a single pure function: given current indoor temp, an hourly
forecast, the window list, and the user's daily schedule + prefs, it returns
one Recommendation. No I/O, no globals; trivial to test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from .config import (
    ComfortFloor,
    DailySchedule,
    DaySchedule,
    Exposure,
    Prefs,
    Security,
    WarningPrefs,
    Window,
)


# Don't pester the user about an opportunity more than this far in advance.
OPEN_LOOKAHEAD_HOURS = 2
# Opening to air that's barely under the day's target is wasted effort.
USEFUL_DELTA_OVER_TARGET_F = 5.0
# Default first-order thermal time constant in 1/hour when windows are open.
# Empirically reasonable for a typical house with cross-ventilation; replace
# with a fitted value from `thermal.fit_model` once data is available.
DEFAULT_OPEN_ALPHA_PER_HR = 0.6


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
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Frozen dataclass: bypass setattr immutability for the default.
        if self.warnings is None:
            object.__setattr__(self, "warnings", [])


def _in_bad_sector(direction_deg: float, sector: tuple[float, float] | None) -> bool:
    """Return True if `direction_deg` is inside the user's bad-wind arc."""
    if sector is None:
        return False
    lo, hi = sector
    if lo <= hi:
        return lo <= direction_deg <= hi
    return direction_deg >= lo or direction_deg <= hi


def _window_eligible(window: Window, hour: HourlyForecast, warnings: WarningPrefs) -> bool:
    """Return True if `window` is safe to open at `hour`.

    All checks here are opt-in via WarningPrefs. With defaults (no warnings
    enabled), every window is eligible regardless of weather.
    """
    if window.on_bad_wind_sector and _in_bad_sector(
        hour.wind_direction_deg, warnings.bad_wind_sector_deg
    ):
        return False
    if warnings.warn_on_rain:
        if window.exposure == Exposure.EXPOSED and hour.rain_chance_pct > warnings.max_rain_chance_pct:
            return False
        if window.exposure == Exposure.COVERED and hour.rain_chance_pct > 60.0:
            return False
    if warnings.warn_on_gusts and window.security == Security.UNSECURE:
        if hour.wind_gust_mph > warnings.max_gust_mph:
            return False
    return True


def _hour_passes_open_criteria(
    hour: HourlyForecast,
    indoor_temp_f: float,
    prefs: Prefs,
    target_f: float,
    warnings: WarningPrefs,
) -> bool:
    open_threshold = indoor_temp_f - prefs.hysteresis_f
    useful_max = target_f + USEFUL_DELTA_OVER_TARGET_F
    return (
        hour.temperature_f <= open_threshold
        and hour.temperature_f >= prefs.min_tolerable_outdoor_f
        and hour.temperature_f <= useful_max
        and not _in_bad_sector(hour.wind_direction_deg, warnings.bad_wind_sector_deg)
    )


def _find_open_moment(
    forecast: list[HourlyForecast],
    indoor_temp_f: float,
    prefs: Prefs,
    target_f: float,
    warnings: WarningPrefs,
) -> HourlyForecast | None:
    """First hour in the forecast that meets the global open criteria."""
    for hour in forecast:
        if _hour_passes_open_criteria(hour, indoor_temp_f, prefs, target_f, warnings):
            return hour
    return None


def _next_local_time(after: datetime, target: time) -> datetime:
    """Next occurrence of wall-clock `target` strictly after `after`."""
    candidate = after.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def predict_indoor_path(
    indoor_temp_f: float,
    forecast: list[HourlyForecast],
    *,
    alpha_per_hr: float = DEFAULT_OPEN_ALPHA_PER_HR,
) -> list[tuple[datetime, float]]:
    """First-order model of indoor temp with the windows open.

    Newton's law of cooling: indoor approaches outdoor exponentially with
    rate `alpha_per_hr` per hour. This is intentionally simple — a fitted
    model from `thermal.fit_model` should plug in here later via
    `alpha_per_hr`.
    """
    path: list[tuple[datetime, float]] = []
    if not forecast:
        return path
    cur_t = indoor_temp_f
    prev_ts = forecast[0].timestamp
    for hour in forecast:
        dt_hr = max(0.0, (hour.timestamp - prev_ts).total_seconds() / 3600.0)
        # Closed-form integration step for dT/dt = -α(T - T_out).
        cur_t = hour.temperature_f + (cur_t - hour.temperature_f) * math.exp(-alpha_per_hr * dt_hr)
        path.append((hour.timestamp, cur_t))
        prev_ts = hour.timestamp
    return path


def _find_close_moment(
    forecast: list[HourlyForecast],
    open_moment: HourlyForecast,
    indoor_temp_f: float,
    prefs: Prefs,
    floor: ComfortFloor,
    schedule: DailySchedule,
) -> tuple[datetime, str | None]:
    """Earliest of:
      * outdoor crossing back above (indoor - hysteresis)
      * the predicted indoor temp hitting the comfort floor
      * tomorrow's `leave_at` (or, fallback, quiet_hours_end)

    Returns (timestamp, optional warning string).
    """
    threshold = indoor_temp_f - prefs.hysteresis_f
    open_idx = forecast.index(open_moment)
    tail = forecast[open_idx:]

    warmup_at: datetime | None = None
    for hour in tail[1:]:
        if hour.temperature_f >= threshold:
            warmup_at = hour.timestamp
            break

    # Predict indoor path under "windows open" assumption.
    floor_hit: datetime | None = None
    for ts, predicted in predict_indoor_path(indoor_temp_f, tail):
        if predicted <= floor.min_indoor_f:
            floor_hit = ts
            break

    # The morning close: use the leave_at for the day we'd be waking into.
    morning_leave = _morning_leave_after(open_moment.timestamp, schedule, prefs)

    candidates: list[tuple[datetime, str]] = []
    if warmup_at is not None:
        candidates.append((warmup_at, "outdoor warmed back up"))
    if floor_hit is not None:
        candidates.append((floor_hit, f"indoor would hit {floor.min_indoor_f:.0f} °F floor"))
    candidates.append((morning_leave, "morning routine"))

    best_ts, best_reason = min(candidates, key=lambda c: c[0])
    warning = best_reason if best_ts is floor_hit else None
    return best_ts, warning


def _morning_leave_after(now_local: datetime, schedule: DailySchedule, prefs: Prefs) -> datetime:
    """Find the next `leave_at` time on the schedule. If the day's schedule
    has none (home_all_day), fall back to `prefs.quiet_hours_end`."""
    for offset in (0, 1):
        cand_date = (now_local + timedelta(days=offset)).date()
        day = schedule.for_weekday(cand_date.weekday())
        leave_at = day.leave_at or prefs.quiet_hours_end
        cand = now_local.replace(
            year=cand_date.year, month=cand_date.month, day=cand_date.day,
            hour=leave_at.hour, minute=leave_at.minute, second=0, microsecond=0,
        )
        if cand > now_local:
            return cand
    # Shouldn't happen — quiet_hours_end at least always exists.
    return now_local + timedelta(hours=8)


def is_quiet_hours(now: datetime, prefs: Prefs) -> bool:
    """True if `now` (local wall clock) falls between quiet_hours_start and quiet_hours_end."""
    t = now.time()
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _today_schedule(now: datetime, schedule: DailySchedule) -> DaySchedule:
    return schedule.for_weekday(now.weekday())


def decide_actions(
    indoor_temp_f: float,
    hourly_forecast: list[HourlyForecast],
    windows: list[Window],
    now: datetime,
    *,
    schedule: DailySchedule | None = None,
    prefs: Prefs | None = None,
    comfort_floor: ComfortFloor | None = None,
    warnings: WarningPrefs | None = None,
) -> Recommendation:
    """Decide whether to open, close, or do nothing. Pure function."""
    schedule = schedule or DailySchedule()
    prefs = prefs or Prefs()
    comfort_floor = comfort_floor or ComfortFloor()
    warnings = warnings or WarningPrefs()

    if not hourly_forecast:
        return Recommendation("no_change", [], None, None, "No forecast available.")

    today = _today_schedule(now, schedule)
    open_threshold = indoor_temp_f - prefs.hysteresis_f
    current = hourly_forecast[0]

    open_moment = _find_open_moment(
        hourly_forecast, indoor_temp_f, prefs, today.target_f, warnings
    )
    if open_moment is not None:
        eligible = [w for w in windows if _window_eligible(w, open_moment, warnings)]
        starts_in = open_moment.timestamp - now
        if eligible and starts_in <= timedelta(hours=OPEN_LOOKAHEAD_HOURS):
            close_at, floor_warning = _find_close_moment(
                hourly_forecast, open_moment, indoor_temp_f, prefs, comfort_floor, schedule,
            )
            warn_list: list[str] = []
            if floor_warning:
                warn_list.append(f"Close by {close_at:%H:%M} — {floor_warning}.")
            # If the open opportunity is so short it'd close immediately, skip.
            if close_at <= open_moment.timestamp:
                return Recommendation(
                    "no_change", [], None, None,
                    "Open window would overshoot the comfort floor immediately.",
                )
            return Recommendation(
                action="open",
                eligible_windows=[w.id for w in eligible],
                open_at=open_moment.timestamp,
                close_at=close_at,
                reason=(
                    f"Outdoor {open_moment.temperature_f:.1f}°F at "
                    f"{open_moment.timestamp:%H:%M}, indoor {indoor_temp_f:.1f}°F "
                    f"(target {today.target_f:.0f} °F). Close by {close_at:%H:%M}."
                ),
                warnings=warn_list,
            )

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
    *,
    schedule: DailySchedule | None = None,
    prefs: Prefs | None = None,
) -> bool:
    """Apply dedup + quiet-hours + daily-schedule rules to a recommendation.

    - Never notify the same action twice in a row (dedup).
    - Quiet hours suppress OPEN but NOT CLOSE.
    - CLOSE only fires if the user was previously told to OPEN.
    - On weekdays with a `leave_at` set, CLOSE notifications fired before
      that time are suppressed (the user closes on the way out).
    """
    schedule = schedule or DailySchedule()
    prefs = prefs or Prefs()

    if rec.action in ("no_change", "summary"):
        return False
    if rec.action == last_action:
        return False
    if rec.action == "open":
        if is_quiet_hours(now, prefs):
            return False
        return True
    if rec.action == "close":
        if last_action != "open":
            return False
        today = _today_schedule(now, schedule)
        # If the user has a typical leave time today and it's still ahead of
        # us, they'll close on the way out — no need to ping.
        if today.leave_at is not None and not today.home_all_day:
            leave_dt = now.replace(
                hour=today.leave_at.hour, minute=today.leave_at.minute,
                second=0, microsecond=0,
            )
            if now < leave_dt:
                return False
        return True
    return False


def summarize_missed_opportunity(
    forecast: list[HourlyForecast],
    indoor_temp_f: float,
    prefs: Prefs,
    target_f: float,
    warnings: WarningPrefs,
) -> Recommendation | None:
    """Build a 'you missed a cooling window' summary for the morning. Returns
    None when nothing notable happened."""
    cool_hours = [
        h for h in forecast
        if _hour_passes_open_criteria(h, indoor_temp_f, prefs, target_f, warnings)
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
