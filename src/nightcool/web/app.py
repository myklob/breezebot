"""FastAPI app exposing the engine to the PWA.

  GET  /api/state         current rec + indoor temp + today's target + forecast head
  GET  /api/forecast      full hourly forecast
  GET  /api/schedule      full 7-day schedule
  POST /api/schedule/{day}  update one day's target/leave_at/home_all_day
  POST /api/indoor-temp   user updates indoor temp (manual source)
  POST /api/geocode       resolve an address to coordinates and persist
  GET  /api/vapid-public  VAPID public key (for the PWA's subscribe step)
  POST /api/subscribe     register a browser push subscription
  POST /api/unsubscribe   remove one
  GET  /api/savings       model + savings estimate (or `{model: null}`)
  GET  /                  the PWA itself
  GET  /static/*          PWA assets
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import WEEKDAY_KEYS, AppConfig, DaySchedule
from ..daemon import format_notification, read_indoor_temp, resolve_coordinates
from ..engine import decide_actions, summarize_missed_opportunity
from ..geocode import GeocodeError, geocode as do_geocode
from ..state import (
    add_subscription,
    list_subscriptions,
    read_state,
    remove_subscription,
    set_indoor_temp,
    write_state,
)
from ..thermal import connect, estimate_savings, fit_model, load_observations
from ..weather import NWSProvider, WeatherProvider


logger = logging.getLogger("nightcool.web")
STATIC_DIR = Path(__file__).parent / "static"


class IndoorTempIn(BaseModel):
    temperature_f: float = Field(..., gt=-50, lt=150)


class GeocodeIn(BaseModel):
    address: str = Field(..., min_length=3)


class DayIn(BaseModel):
    target_f: float | None = Field(default=None, gt=40, lt=100)
    leave_at: str | None = None  # "HH:MM" or null
    home_all_day: bool | None = None


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: dict[str, str]
    expirationTime: int | None = None


class UnsubscribeIn(BaseModel):
    endpoint: str


def create_app(
    cfg: AppConfig,
    state_path: Path,
    *,
    config_path: Path | None = None,
    provider: WeatherProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="NightCool", version="0.3.0")

    def get_provider() -> WeatherProvider:
        if provider is not None:
            return provider
        state = read_state(state_path)
        lat, lon = resolve_coordinates(cfg, state)
        write_state(state_path, state)
        return NWSProvider(lat, lon)

    def now_local() -> datetime:
        return datetime.now(ZoneInfo(cfg.location.timezone))

    def _save_config() -> None:
        """Persist the current cfg back to config.yaml. Requires config_path."""
        if config_path is None:
            raise HTTPException(409, "Server started without a config path; cannot persist.")
        data = cfg.model_dump(mode="json", exclude_none=True)
        config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        state = read_state(state_path)
        indoor, source_name = read_indoor_temp(cfg, state)
        forecast = get_provider().hourly_forecast(hours=12)
        now = now_local()
        rec = decide_actions(
            indoor, forecast, cfg.windows, now,
            schedule=cfg.schedule, prefs=cfg.prefs,
            comfort_floor=cfg.comfort_floor, warnings=cfg.warnings,
        )
        title, body = format_notification(rec)
        today = cfg.schedule.for_weekday(now.weekday())
        return {
            "now": now.isoformat(),
            "indoor_f": indoor,
            "indoor_source": source_name,
            "today": {
                "weekday": WEEKDAY_KEYS[now.weekday()],
                "target_f": today.target_f,
                "leave_at": today.leave_at.isoformat() if today.leave_at else None,
                "home_all_day": today.home_all_day,
            },
            "recommendation": {
                "action": rec.action,
                "title": title,
                "body": body,
                "windows": rec.eligible_windows,
                "open_at": rec.open_at.isoformat() if rec.open_at else None,
                "close_at": rec.close_at.isoformat() if rec.close_at else None,
                "warnings": rec.warnings or [],
            },
            "forecast_head": [
                {
                    "ts": h.timestamp.isoformat(),
                    "temp_f": h.temperature_f,
                    "wind_mph": h.wind_speed_mph,
                    "rain_pct": h.rain_chance_pct,
                }
                for h in forecast[:6]
            ],
            "location": {
                "address": cfg.location.address,
                "latitude": cfg.location.latitude,
                "longitude": cfg.location.longitude,
            },
        }

    @app.get("/api/forecast")
    def get_forecast() -> dict[str, Any]:
        hours = get_provider().hourly_forecast(hours=12)
        return {
            "hours": [
                {
                    "ts": h.timestamp.isoformat(),
                    "temp_f": h.temperature_f,
                    "wind_mph": h.wind_speed_mph,
                    "gust_mph": h.wind_gust_mph,
                    "wind_dir_deg": h.wind_direction_deg,
                    "rain_pct": h.rain_chance_pct,
                }
                for h in hours
            ]
        }

    @app.get("/api/schedule")
    def get_schedule() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in WEEKDAY_KEYS:
            day = getattr(cfg.schedule, key)
            out[key] = {
                "target_f": day.target_f,
                "leave_at": day.leave_at.isoformat() if day.leave_at else None,
                "home_all_day": day.home_all_day,
            }
        return out

    @app.post("/api/schedule/{day}")
    def post_schedule_day(day: str, payload: DayIn) -> dict[str, Any]:
        if day not in WEEKDAY_KEYS:
            raise HTTPException(400, f"unknown day {day!r}")
        current = getattr(cfg.schedule, day)
        new_target = payload.target_f if payload.target_f is not None else current.target_f
        new_home = payload.home_all_day if payload.home_all_day is not None else current.home_all_day
        if payload.leave_at is None:
            # Only clear leave_at when home_all_day is being explicitly set to True;
            # sending home_all_day=false (or omitting it) must preserve the current value.
            new_leave = current.leave_at if payload.home_all_day is not True else None
        elif payload.leave_at == "":
            new_leave = None
        else:
            try:
                new_leave = time.fromisoformat(payload.leave_at)
            except ValueError:
                raise HTTPException(400, f"invalid leave_at {payload.leave_at!r}; expected HH:MM")
        if new_home:
            new_leave = None
        setattr(cfg.schedule, day, DaySchedule(
            target_f=new_target,
            leave_at=new_leave,
            home_all_day=new_home,
        ))
        _save_config()
        return {"ok": True, "day": day}

    @app.post("/api/indoor-temp")
    def post_indoor_temp(payload: IndoorTempIn) -> dict[str, Any]:
        st = read_state(state_path)
        set_indoor_temp(st, payload.temperature_f, now_local())
        write_state(state_path, st)
        return {"ok": True, "indoor_f": payload.temperature_f}

    @app.post("/api/geocode")
    def post_geocode(payload: GeocodeIn) -> dict[str, Any]:
        try:
            result = do_geocode(payload.address)
        except GeocodeError as e:
            raise HTTPException(400, str(e))
        cfg.location.address = result.matched_address
        cfg.location.latitude = result.latitude
        cfg.location.longitude = result.longitude
        if config_path is not None:
            _save_config()
        # Clear the cache so the next poll re-resolves.
        st = read_state(state_path)
        st.pop("location_cache", None)
        write_state(state_path, st)
        return {
            "ok": True,
            "matched_address": result.matched_address,
            "latitude": result.latitude,
            "longitude": result.longitude,
        }

    @app.get("/api/vapid-public")
    def get_vapid_public() -> dict[str, Any]:
        wp = cfg.notifications.web_push
        if not wp or not wp.vapid_public_key:
            raise HTTPException(status_code=404, detail="web push not configured")
        return {"public_key": wp.vapid_public_key}

    @app.post("/api/subscribe")
    def post_subscribe(sub: SubscriptionIn) -> dict[str, Any]:
        st = read_state(state_path)
        added = add_subscription(st, sub.model_dump(exclude_none=True))
        if added:
            write_state(state_path, st)
        return {"ok": True, "added": added, "total": len(list_subscriptions(st))}

    @app.post("/api/unsubscribe")
    def post_unsubscribe(payload: UnsubscribeIn) -> dict[str, Any]:
        st = read_state(state_path)
        removed = remove_subscription(st, payload.endpoint)
        if removed:
            write_state(state_path, st)
        return {"ok": True, "removed": removed}

    @app.get("/api/savings")
    def get_savings() -> dict[str, Any]:
        path = Path(cfg.web.data_log_path)
        if not path.exists():
            return {"model": None, "reason": "no data yet"}
        conn = connect(path)
        try:
            obs = load_observations(conn)
        finally:
            conn.close()
        model = fit_model(obs)
        if model is None:
            return {"model": None, "samples": len(obs), "reason": "insufficient data"}
        open_actions = sum(1 for o in obs if o.action == "open")
        kwh, dollars = estimate_savings(model, hours_avoided=open_actions * 6.0)
        return {
            "model": {
                "alpha_ventilation": model.alpha_ventilation,
                "beta_solar": model.beta_solar,
                "gamma_hvac": model.gamma_hvac,
                "r_squared": model.r_squared,
                "sample_count": model.sample_count,
                "duration_hours": model.duration_hours,
            },
            "kwh_saved": kwh,
            "dollars_saved": dollars,
            "open_events_counted": open_actions,
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/sw.js")
        def service_worker() -> FileResponse:
            return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

        @app.get("/manifest.webmanifest")
        def manifest() -> FileResponse:
            return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def run_server(cfg: AppConfig, state_path: Path, config_path: Path | None = None) -> None:
    import uvicorn
    app = create_app(cfg, state_path, config_path=config_path)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="info")
