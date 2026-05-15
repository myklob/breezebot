"""APScheduler-driven polling loop.

Every POLL_MINUTES, read indoor temp + NWS forecast, run the engine, and notify
if the action changed since last time.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import AppConfig
from .engine import Recommendation, decide_actions, should_notify
from .notifier import make_notifier
from .state import (
    get_indoor_temp,
    get_last_action,
    read_state,
    set_last_action,
    write_state,
)
from .weather import NWSProvider, WeatherProvider


POLL_MINUTES = 15
FORECAST_HOURS = 12

logger = logging.getLogger("nightcool.daemon")


def read_indoor_temp(cfg: AppConfig, state: dict) -> float:
    """Resolve indoor temperature based on cfg.indoor_temp.source."""
    if cfg.indoor_temp.source == "sensor_file" and cfg.indoor_temp.sensor_file_path:
        p = Path(cfg.indoor_temp.sensor_file_path)
        if p.exists():
            try:
                return float(p.read_text(encoding="utf-8").strip())
            except ValueError:
                logger.warning("sensor_file %s did not contain a float; falling back", p)
    return get_indoor_temp(state, cfg.indoor_temp.manual_default_f)


def format_notification(rec: Recommendation) -> tuple[str, str]:
    """Build (title, body) for a recommendation."""
    if rec.action == "open":
        title = f"OPEN: {', '.join(rec.eligible_windows)}"
        return title, rec.reason
    if rec.action == "close":
        return "CLOSE windows", rec.reason
    return "NightCool", rec.reason


def run_once(
    cfg: AppConfig,
    state_path: Path,
    provider: WeatherProvider | None = None,
) -> Recommendation:
    """Run one poll cycle. Returns the engine recommendation for inspection."""
    tz = ZoneInfo(cfg.location.timezone)
    now = datetime.now(tz)
    state = read_state(state_path)
    indoor = read_indoor_temp(cfg, state)
    provider = provider or NWSProvider(cfg.location.latitude, cfg.location.longitude)
    forecast = provider.hourly_forecast(hours=FORECAST_HOURS)
    rec = decide_actions(indoor, forecast, cfg.windows, cfg.user_prefs, now)
    last = get_last_action(state)
    if should_notify(rec, last, now, cfg.user_prefs):
        notifier = make_notifier(cfg.notifications)
        title, body = format_notification(rec)
        notifier.send(title, body)
        set_last_action(state, rec.action, now)
        write_state(state_path, state)
        logger.info("Notified: %s — %s", title, body)
    else:
        logger.debug("No notification: action=%s last=%s", rec.action, last)
    return rec


def run_daemon(cfg: AppConfig, state_path: Path) -> None:
    """Block forever, running run_once every POLL_MINUTES."""
    tz = ZoneInfo(cfg.location.timezone)
    scheduler = BlockingScheduler(timezone=cfg.location.timezone)
    scheduler.add_job(
        run_once,
        "interval",
        minutes=POLL_MINUTES,
        args=[cfg, state_path],
        next_run_time=datetime.now(tz),
        max_instances=1,
        coalesce=True,
    )
    logger.info("Starting daemon: poll every %d minutes", POLL_MINUTES)
    scheduler.start()
