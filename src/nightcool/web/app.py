"""FastAPI app exposing the engine to the PWA.

The daemon is the source of truth for notifications; this server provides:

  GET  /api/state         current rec + indoor temp + profile + forecast head
  GET  /api/forecast      full hourly forecast
  POST /api/indoor-temp   user updates indoor temp (manual source)
  POST /api/profile       switch schedule profile
  GET  /api/vapid-public  VAPID public key (for the PWA's subscribe step)
  POST /api/subscribe     register a browser push subscription
  POST /api/unsubscribe   remove one
  GET  /api/savings       model + savings estimate (or `{model: null}`)
  GET  /                  the PWA itself
  GET  /static/*          PWA assets

The server reuses the same `config.yaml` + `state.json` files as the
daemon; running both in parallel is fine because state.json writes are
small and rare.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import AppConfig, ProfileName
from ..daemon import format_notification, read_indoor_temp
from ..engine import decide_actions, summarize_missed_opportunity
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


class ProfileIn(BaseModel):
    profile: ProfileName


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
    """Build the FastAPI app. `provider` is injectable for tests."""
    app = FastAPI(title="NightCool", version="0.2.0")

    def get_provider() -> WeatherProvider:
        return provider or NWSProvider(cfg.location.latitude, cfg.location.longitude)

    def now_local() -> datetime:
        return datetime.now(ZoneInfo(cfg.location.timezone))

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        state = read_state(state_path)
        indoor, source_name = read_indoor_temp(cfg, state)
        forecast = get_provider().hourly_forecast(hours=12)
        prefs = cfg.effective_prefs()
        profile = prefs.resolved_profile()
        rec = decide_actions(indoor, forecast, cfg.windows, prefs, now_local(), profile=profile)
        title, body = format_notification(rec)
        summary = None
        if profile.defer_overnight_opens_to_summary:
            s = summarize_missed_opportunity(forecast, indoor, prefs)
            if s is not None:
                summary = {"open_at": s.open_at.isoformat() if s.open_at else None, "reason": s.reason}
        return {
            "now": now_local().isoformat(),
            "indoor_f": indoor,
            "indoor_source": source_name,
            "profile": profile.name.value,
            "recommendation": {
                "action": rec.action,
                "title": title,
                "body": body,
                "windows": rec.eligible_windows,
                "open_at": rec.open_at.isoformat() if rec.open_at else None,
                "close_at": rec.close_at.isoformat() if rec.close_at else None,
            },
            "morning_summary": summary,
            "forecast_head": [
                {
                    "ts": h.timestamp.isoformat(),
                    "temp_f": h.temperature_f,
                    "wind_mph": h.wind_speed_mph,
                    "rain_pct": h.rain_chance_pct,
                }
                for h in forecast[:6]
            ],
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

    @app.post("/api/indoor-temp")
    def post_indoor_temp(payload: IndoorTempIn) -> dict[str, Any]:
        st = read_state(state_path)
        set_indoor_temp(st, payload.temperature_f, now_local())
        write_state(state_path, st)
        return {"ok": True, "indoor_f": payload.temperature_f}

    @app.post("/api/profile")
    def post_profile(payload: ProfileIn) -> dict[str, Any]:
        if config_path is None:
            raise HTTPException(
                status_code=409,
                detail="Server started without a config path; profile changes not persisted.",
            )
        # Rewrite config.yaml in place, preserving everything else by reading
        # raw YAML rather than round-tripping through the model.
        import yaml
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raw.setdefault("user_prefs", {})["profile"] = payload.profile.value
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        cfg.user_prefs.profile = payload.profile
        return {"ok": True, "profile": payload.profile.value}

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
        # Heuristic: count every "open" recommendation as ~6 hours of avoided
        # AC runtime. Replace with a model-derived estimate once we have
        # window-confirmation telemetry from the user.
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
            # Service workers must be served from the same scope they control,
            # so it can't live under /static/.
            return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

        @app.get("/manifest.webmanifest")
        def manifest() -> FileResponse:
            return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def run_server(cfg: AppConfig, state_path: Path, config_path: Path | None = None) -> None:
    """Block forever serving the API + PWA on the configured host/port."""
    import uvicorn
    app = create_app(cfg, state_path, config_path=config_path)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="info")
