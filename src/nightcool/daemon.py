"""APScheduler-driven polling loop.

Every POLL_MINUTES, read indoor temp + NWS forecast, run the engine, and notify
if the action changed since last time.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import AppConfig
from .engine import Recommendation, decide_actions, should_notify
from .geocode import GeocodeError, geocode
from .notifier import make_notifier
from .sources import make_source, read_indoor_with_fallback
from .state import (
    get_indoor_temp_or_none,
    get_last_action,
    list_subscriptions,
    read_state,
    remove_subscription,
    set_last_action,
    update_state,
)
from .thermal import Observation, connect, log_observation
from .weather import NWSProvider, WeatherProvider


POLL_MINUTES = 15
FORECAST_HOURS = 12

logger = logging.getLogger("nightcool.daemon")


def resolve_coordinates(cfg: AppConfig, state: dict[str, Any]) -> tuple[float, float]:
    """Return (lat, lon) for `cfg.location`. Geocodes the address on demand
    and caches the result in `state` so subsequent calls don't re-hit the API."""
    if cfg.location.latitude is not None and cfg.location.longitude is not None:
        return cfg.location.latitude, cfg.location.longitude

    if not cfg.location.address:
        raise ValueError("location requires either coordinates or an address")

    cache = state.get("location_cache") or {}
    if cache.get("address") == cfg.location.address:
        return float(cache["latitude"]), float(cache["longitude"])

    result = geocode(cfg.location.address)
    state["location_cache"] = {
        "address": cfg.location.address,
        "latitude": result.latitude,
        "longitude": result.longitude,
        "matched_address": result.matched_address,
    }
    return result.latitude, result.longitude


def read_indoor_temp(cfg: AppConfig, state: dict[str, Any]) -> tuple[float, str]:
    """Resolve indoor temperature using the configured source chain."""
    manual_reader = lambda: get_indoor_temp_or_none(state)
    source = make_source(cfg.indoor_temp, manual_reader)
    return read_indoor_with_fallback(source, cfg.indoor_temp.manual_default_f)


def format_notification(rec: Recommendation) -> tuple[str, str]:
    """Build (title, body) for a recommendation."""
    if rec.action == "open":
        title = f"OPEN: {', '.join(rec.eligible_windows)}"
        body = rec.reason
        if rec.warnings:
            body = body + " " + " ".join(rec.warnings)
        return title, body
    if rec.action == "close":
        return "CLOSE windows", rec.reason
    if rec.action == "summary":
        return "Overnight cooling missed", rec.reason
    return "NightCool", rec.reason


def _build_notifier(cfg: AppConfig, state_path: Path):
    def loader() -> list[dict[str, Any]]:
        return list_subscriptions(read_state(state_path))

    def pruner(endpoint: str) -> None:
        update_state(state_path, lambda st: remove_subscription(st, endpoint))

    return make_notifier(
        cfg.notifications,
        state_path=state_path,
        subscription_loader=loader,
        prune_subscription=pruner,
    )


def _make_provider(cfg: AppConfig, state: dict[str, Any]) -> WeatherProvider:
    lat, lon = resolve_coordinates(cfg, state)
    return NWSProvider(lat, lon)


def run_once(
    cfg: AppConfig,
    state_path: Path,
    provider: WeatherProvider | None = None,
) -> Recommendation:
    """Run one poll cycle. Returns the engine recommendation for inspection."""
    tz = ZoneInfo(cfg.location.timezone)
    now = datetime.now(tz)
    state = read_state(state_path)
    indoor, source_name = read_indoor_temp(cfg, state)
    if provider is None:
        try:
            provider = _make_provider(cfg, state)
        except GeocodeError as e:
            logger.error("Could not geocode location: %s", e)
            return Recommendation("no_change", [], None, None, f"Geocoding failed: {e}")
        # Persist any newly-cached coordinates.
        if "location_cache" in state:
            cache = state["location_cache"]
            update_state(state_path, lambda st: st.update({"location_cache": cache}))
    forecast = provider.hourly_forecast(hours=FORECAST_HOURS)
    rec = decide_actions(
        indoor, forecast, cfg.windows, now,
        schedule=cfg.schedule, prefs=cfg.prefs,
        comfort_floor=cfg.comfort_floor, warnings=cfg.warnings,
    )
    last = get_last_action(state)
    if should_notify(rec, last, now, schedule=cfg.schedule, prefs=cfg.prefs):
        notifier = _build_notifier(cfg, state_path)
        title, body = format_notification(rec)
        notifier.send(title, body)
        update_state(state_path, lambda st: set_last_action(st, rec.action, now))
        logger.info("Notified: %s — %s", title, body)
    else:
        logger.debug("No notification: action=%s last=%s", rec.action, last)

    _log_observation(cfg, now, indoor, source_name, forecast, rec.action)
    return rec


def _log_observation(
    cfg: AppConfig,
    now: datetime,
    indoor_f: float,
    source_name: str,
    forecast: list,
    action: str,
) -> None:
    """Append the current poll to the thermal-model data store. Soft-fails."""
    try:
        path = Path(cfg.web.data_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        cur = forecast[0] if forecast else None
        try:
            log_observation(
                conn,
                Observation(
                    ts=now,
                    indoor_f=indoor_f,
                    outdoor_f=cur.temperature_f if cur else None,
                    wind_mph=cur.wind_speed_mph if cur else None,
                    rain_pct=cur.rain_chance_pct if cur else None,
                    action=action,
                    windows_open=None,
                    hvac_active=None,
                    indoor_source=source_name,
                ),
            )
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover — logging must never break the daemon.
        logger.warning("data log write failed: %s", e)


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
