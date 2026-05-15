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


class ProfileName(str, Enum):
    """Built-in schedule presets.

    Each preset bundles a set of behavior flags that shape the daily
    notification cadence. A user picks one and optionally overrides
    individual fields under `user_prefs`.
    """

    COMMUTER = "commuter"
    WFH = "wfh"
    NIGHT_SHIFT = "night_shift"
    LIGHT_SLEEPER = "light_sleeper"
    AGGRESSIVE = "aggressive"
    CONSERVATIVE = "conservative"
    CUSTOM = "custom"


class Location(BaseModel):
    """House location (used by the NWS provider)."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    timezone: str


class ScheduleProfile(BaseModel):
    """Behavior flags that shape notification cadence.

    Defaults match the original v1 behavior (close notifications always fire,
    only overnight openings are considered, no morning summary). Presets
    flip these flags; callers can also set them by hand under
    `user_prefs.profile_overrides`.
    """

    name: ProfileName = ProfileName.CUSTOM
    weekday_morning_close_notify: bool = True
    """When false, suppress the weekday CLOSE ping before morning_close_time;
    the commuter closes on the way out anyway."""

    weekend_close_notify: bool = True
    """When false, also suppress weekend CLOSE pings (rare; for night-shift)."""

    daytime_open_check: bool = False
    """When true, scan for daytime cooling opportunities too, not just
    pre-bedtime. Useful for WFH/retiree users home during the day."""

    defer_overnight_opens_to_summary: bool = False
    """Light sleepers: drop OPEN notifications that fire inside quiet hours
    and surface them as a morning summary instead."""

    morning_summary_time: time = time(7, 30)
    """When `defer_overnight_opens_to_summary`, emit the summary at this
    local time."""

    sustained_hours_required: int = 1
    """Conservative users: require at least N consecutive hours of cool
    air in the forecast before alerting."""

    inverted_sleep_schedule: bool = False
    """Night-shift workers sleep during the day; flip the role of quiet
    hours and morning_close_time. (Reserved; engine consumes the same
    quiet_hours fields — set them to match your sleep window.)"""


PROFILE_PRESETS: dict[ProfileName, ScheduleProfile] = {
    ProfileName.COMMUTER: ScheduleProfile(
        name=ProfileName.COMMUTER,
        weekday_morning_close_notify=False,
        weekend_close_notify=True,
        daytime_open_check=False,
    ),
    ProfileName.WFH: ScheduleProfile(
        name=ProfileName.WFH,
        weekday_morning_close_notify=True,
        daytime_open_check=True,
    ),
    ProfileName.NIGHT_SHIFT: ScheduleProfile(
        name=ProfileName.NIGHT_SHIFT,
        weekday_morning_close_notify=True,
        daytime_open_check=True,
        inverted_sleep_schedule=True,
    ),
    ProfileName.LIGHT_SLEEPER: ScheduleProfile(
        name=ProfileName.LIGHT_SLEEPER,
        weekday_morning_close_notify=True,
        defer_overnight_opens_to_summary=True,
    ),
    ProfileName.AGGRESSIVE: ScheduleProfile(
        name=ProfileName.AGGRESSIVE,
        weekday_morning_close_notify=True,
        daytime_open_check=True,
        sustained_hours_required=1,
    ),
    ProfileName.CONSERVATIVE: ScheduleProfile(
        name=ProfileName.CONSERVATIVE,
        weekday_morning_close_notify=True,
        sustained_hours_required=3,
    ),
    ProfileName.CUSTOM: ScheduleProfile(name=ProfileName.CUSTOM),
}


# Per-profile tunings that override UserPrefs numeric defaults. Anything
# the user sets explicitly under `user_prefs` wins over these.
PROFILE_PREFS_OVERRIDES: dict[ProfileName, dict[str, float]] = {
    ProfileName.AGGRESSIVE: {"hysteresis_f": 1.0},
    ProfileName.CONSERVATIVE: {"hysteresis_f": 5.0},
}


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
    profile: ProfileName = ProfileName.CUSTOM
    profile_overrides: dict[str, object] = Field(default_factory=dict)

    @field_validator("bad_wind_sector_deg")
    @classmethod
    def _check_sector(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return v
        lo, hi = v
        if not (0 <= lo < 360 and 0 <= hi < 360):
            raise ValueError("bad_wind_sector_deg values must be in [0, 360)")
        return v

    def resolved_profile(self) -> ScheduleProfile:
        """Return the preset for `self.profile`, with per-field overrides
        from `profile_overrides` applied on top."""
        base = PROFILE_PRESETS[self.profile].model_dump()
        base.update(self.profile_overrides or {})
        return ScheduleProfile.model_validate(base)


class IndoorSourceKind(str, Enum):
    """Where the daemon should read the current indoor temperature from."""

    MANUAL = "manual"
    SENSOR_FILE = "sensor_file"
    NEST = "nest"
    BLE = "ble"


class NestConfig(BaseModel):
    """OAuth + device IDs for Google Smart Device Management.

    See https://developers.google.com/nest/device-access. v1 stores the
    refresh token on disk; production should use a secret manager.
    """

    project_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    device_id: str | None = None


class BLESensorConfig(BaseModel):
    """A Bluetooth Low Energy thermometer (Govee/SwitchBot/ThermoPro/etc.).

    v1 stores the MAC and trusts a separate reader process to write its
    most-recent reading to `cache_file`. The thermal model can also use
    multiple BLE sources by listing extras under `extras`.
    """

    mac: str | None = None
    cache_file: Path | None = None
    extras: list["BLESensorConfig"] = Field(default_factory=list)


class IndoorTempConfig(BaseModel):
    """How to obtain the current indoor temperature.

    `manual`: read from state.json, written via `nightcool set-indoor`.
    `sensor_file`: read a single float (°F) from the configured path.
    `nest`: poll Google SDM API for the configured device.
    `ble`: read the cache file maintained by a separate BLE reader.
    """

    source: IndoorSourceKind = IndoorSourceKind.MANUAL
    manual_default_f: float = 71.0
    sensor_file_path: Path | None = None
    nest: NestConfig | None = None
    ble: BLESensorConfig | None = None


class Window(BaseModel):
    """One window in the house."""

    id: str
    name: str
    exposure: Exposure
    security: Security
    on_bad_wind_sector: bool = False


class WebPushConfig(BaseModel):
    """VAPID keys + admin contact for browser push.

    Generate keys with `nightcool web-push-keys`. The public key is served
    to the PWA; the private key signs each push.
    """

    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:owner@example.invalid"


class NotificationConfig(BaseModel):
    """Push notification backend selection."""

    service: Literal["ntfy", "pushover", "console", "web_push"] = "console"
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    pushover_user_key: str | None = None
    pushover_app_token: str | None = None
    web_push: WebPushConfig | None = None


class WebServerConfig(BaseModel):
    """HTTP API + PWA host/port. Loopback by default; bind 0.0.0.0 only on
    a trusted LAN."""

    host: str = "127.0.0.1"
    port: int = 8765
    data_log_path: Path = Path("data_log.sqlite")


class AppConfig(BaseModel):
    """Top-level config: a complete description of the install."""

    location: Location
    user_prefs: UserPrefs
    indoor_temp: IndoorTempConfig
    windows: list[Window]
    notifications: NotificationConfig
    web: WebServerConfig = Field(default_factory=WebServerConfig)

    @field_validator("windows")
    @classmethod
    def _windows_unique(cls, v: list[Window]) -> list[Window]:
        ids = [w.id for w in v]
        if len(ids) != len(set(ids)):
            raise ValueError("window ids must be unique")
        return v

    def effective_prefs(self) -> UserPrefs:
        """Apply profile preset numeric overrides where the user did not
        already set a value explicitly. Returns a new UserPrefs."""
        overrides = PROFILE_PREFS_OVERRIDES.get(self.user_prefs.profile, {})
        if not overrides:
            return self.user_prefs
        data = self.user_prefs.model_dump()
        explicit = set(self.user_prefs.model_fields_set)
        for key, val in overrides.items():
            if key not in explicit:
                data[key] = val
        return UserPrefs.model_validate(data)


def load_config(path: Path) -> AppConfig:
    """Load and validate config.yaml from disk."""
    with Path(path).open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
