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
from pydantic import BaseModel, Field, field_validator, model_validator


WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    """Where the house is.

    You can give either an `address` (we'll geocode it via the US Census
    Geocoder) or explicit `latitude`/`longitude`. If you give both, the
    coordinates win and the address is informational.
    """

    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str = "America/Denver"

    @model_validator(mode="after")
    def _require_some_location(self) -> "Location":
        has_coords = self.latitude is not None and self.longitude is not None
        if not has_coords and not self.address:
            raise ValueError(
                "location requires either 'address' or both 'latitude' and 'longitude'"
            )
        return self


class DaySchedule(BaseModel):
    """One day's comfort target and routine.

    `target_f` is the temperature you'd like the house to be at by the
    time you wake up (or, on `home_all_day` days, throughout the day).
    `leave_at` is the time you typically leave the house — if a CLOSE
    opportunity falls before this time on a weekday, NightCool assumes
    you'll close on the way out and skips the notification. Set
    `home_all_day` for weekends, days off, or WFH days; the app then
    also looks for daytime cooling opportunities.
    """

    target_f: float = 65.0
    leave_at: time | None = None
    home_all_day: bool = False


def _default_schedule() -> "DailySchedule":
    """A reasonable starter schedule: 65 °F, 7:30 weekday departures,
    home all weekend."""
    weekday = DaySchedule(target_f=65.0, leave_at=time(7, 30))
    weekend = DaySchedule(target_f=65.0, home_all_day=True)
    return DailySchedule(
        mon=weekday, tue=weekday, wed=weekday, thu=weekday, fri=weekday,
        sat=weekend, sun=weekend,
    )


class DailySchedule(BaseModel):
    """A target + routine for each day of the week."""

    mon: DaySchedule = Field(default_factory=DaySchedule)
    tue: DaySchedule = Field(default_factory=DaySchedule)
    wed: DaySchedule = Field(default_factory=DaySchedule)
    thu: DaySchedule = Field(default_factory=DaySchedule)
    fri: DaySchedule = Field(default_factory=DaySchedule)
    sat: DaySchedule = Field(default_factory=lambda: DaySchedule(home_all_day=True))
    sun: DaySchedule = Field(default_factory=lambda: DaySchedule(home_all_day=True))

    def for_weekday(self, weekday: int) -> DaySchedule:
        """Look up the schedule for Python's `datetime.weekday()` (Mon=0)."""
        return getattr(self, WEEKDAY_KEYS[weekday % 7])


class ComfortFloor(BaseModel):
    """The "don't let the house get too cold" guard.

    When NightCool predicts that opening the windows would let indoor
    temperature drop below `min_indoor_f`, it shortens the close-by
    time so the house stops at the floor — or suppresses the OPEN
    entirely if even a brief opening would overshoot.
    """

    min_indoor_f: float = 60.0


class Prefs(BaseModel):
    """House-wide knobs that don't fit the daily schedule."""

    hysteresis_f: float = 2.5
    """Outdoor must be this much cooler than indoor to qualify as a
    cooling opportunity. Prevents flapping on tiny deltas."""

    min_tolerable_outdoor_f: float = 50.0
    """Hard floor on outdoor temperature regardless of indoor temp;
    nobody wants air this cold blowing in."""

    quiet_hours_start: time = time(22, 30)
    """Suppress OPEN notifications after this local time."""

    quiet_hours_end: time = time(6, 0)
    """Resume OPEN notifications after this local time."""


class WarningPrefs(BaseModel):
    """Opt-in alerts about *secondary* concerns while windows are open.

    All of these default to "off" (None / False) — turn on only the ones
    that matter to your house.
    """

    warn_on_rain: bool = False
    """If true, exposed windows are excluded when rain chance exceeds
    `max_rain_chance_pct`."""

    max_rain_chance_pct: float = 20.0

    warn_on_gusts: bool = False
    """If true, windows marked `unsecure` (loose paperwork, light
    curtains) are excluded when forecast gusts exceed `max_gust_mph`."""

    max_gust_mph: float = 18.0

    bad_wind_sector_deg: tuple[float, float] | None = None
    """Set to [lo, hi] degrees if you have a wind direction you don't
    want air coming from — neighbor's smoking, a landfill, a road, an
    allergen source. Wraps around: [350, 10] covers the 20° arc around
    true north."""

    max_dew_point_f: float | None = None
    """Block opening windows when the outdoor dew point exceeds this value.
    A 70 °F night at 70 °F dew point feels miserable indoors even though the
    raw temperature would normally qualify as a cooling opportunity. 60 °F
    is "comfortable"; 65 °F is "noticeable"; above 70 °F is "oppressive".
    Leave as None to disable the gate."""

    @field_validator("bad_wind_sector_deg")
    @classmethod
    def _check_sector(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return v
        lo, hi = v
        if not (0 <= lo < 360 and 0 <= hi < 360):
            raise ValueError("bad_wind_sector_deg values must be in [0, 360)")
        return v


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
    schedule: DailySchedule = Field(default_factory=_default_schedule)
    comfort_floor: ComfortFloor = Field(default_factory=ComfortFloor)
    prefs: Prefs = Field(default_factory=Prefs)
    warnings: WarningPrefs = Field(default_factory=WarningPrefs)
    indoor_temp: IndoorTempConfig = Field(default_factory=IndoorTempConfig)
    windows: list[Window]
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    web: WebServerConfig = Field(default_factory=WebServerConfig)

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
