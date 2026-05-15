"""Configuration models and YAML loader.

The user describes their house and preferences in a single YAML file. This module
validates it with pydantic v2 and exposes typed objects to the rest of the app.
"""
from __future__ import annotations

from datetime import time
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Exposure(str, Enum):
    """Where rain ends up if it hits this window."""

    EXPOSED = "exposed"   # Rain inside is a problem.
    COVERED = "covered"   # Eave or awning; light rain is fine.
    TILED = "tiled"       # Bathroom; rain is fine.


class Security(str, Enum):
    """Whether contents near this window can blow around."""

    SECURE = "secure"
    UNSECURE = "unsecure"


class Location(BaseModel):
    """House location (used by the NWS provider)."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    timezone: str


class UserPrefs(BaseModel):
    """Comfort/safety thresholds and quiet-hours window."""

    sleep_target_f: float
    min_tolerable_outdoor_f: float
    hysteresis_f: float = 2.5
    max_gust_mph: float = 18.0
    max_rain_chance_pct: float = 20.0
    bad_wind_sector_deg: tuple[float, float] | None = None
    quiet_hours_start: time = time(22, 30)
    quiet_hours_end: time = time(6, 0)
    morning_close_time: time = time(6, 30)  # Approximate "sunrise + 30 min" until v2.

    @field_validator("bad_wind_sector_deg")
    @classmethod
    def _check_sector(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return v
        lo, hi = v
        if not (0 <= lo < 360 and 0 <= hi < 360):
            raise ValueError("bad_wind_sector_deg values must be in [0, 360)")
        return v


class IndoorTempConfig(BaseModel):
    """How to obtain the current indoor temperature.

    `manual`: read from state.json, written via `nightcool set-indoor`.
    `sensor_file`: read a single float (°F) from the configured path.
    """

    source: Literal["manual", "sensor_file"] = "manual"
    manual_default_f: float = 71.0
    sensor_file_path: Path | None = None


class Window(BaseModel):
    """One window in the house."""

    id: str
    name: str
    exposure: Exposure
    security: Security
    on_bad_wind_sector: bool = False


class NotificationConfig(BaseModel):
    """Push notification backend selection."""

    service: Literal["ntfy", "pushover", "console"] = "console"
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    pushover_user_key: str | None = None
    pushover_app_token: str | None = None


class AppConfig(BaseModel):
    """Top-level config: a complete description of the install."""

    location: Location
    user_prefs: UserPrefs
    indoor_temp: IndoorTempConfig
    windows: list[Window]
    notifications: NotificationConfig

    @field_validator("windows")
    @classmethod
    def _windows_unique(cls, v: list[Window]) -> list[Window]:
        ids = [w.id for w in v]
        if len(ids) != len(set(ids)):
            raise ValueError("window ids must be unique")
        return v


def load_config(path: Path) -> AppConfig:
    """Load and validate config.yaml from disk."""
    with Path(path).open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
