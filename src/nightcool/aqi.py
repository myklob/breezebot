"""Air quality providers.

AirNowProvider hits api.airnowapi.org (free key at airnowapi.org).
PurpleAirProvider hits api.purpleair.com (free key at develop.purpleair.com).
CachedAQIProvider wraps either and prevents redundant calls for 30 minutes.
MockAQIProvider is for tests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import AppConfig


AIRNOW_BASE = "https://www.airnowapi.org/aq/observation/latLong/current/"
PURPLEAIR_BASE = "https://api.purpleair.com/v1/sensors"
HTTP_TIMEOUT_S = 10.0

# EPA PM2.5 24-hour average AQI breakpoints: (c_lo, c_hi, aqi_lo, aqi_hi).
_PM25_BREAKPOINTS: list[tuple[float, float, int, int]] = [
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]


def _pm25_to_aqi(pm25: float) -> int:
    """Convert PM2.5 concentration (µg/m³) to AQI using EPA piecewise-linear breakpoints.

    Returns 500 for concentrations above the highest breakpoint.
    """
    for c_lo, c_hi, aqi_lo, aqi_hi in _PM25_BREAKPOINTS:
        if c_lo <= pm25 <= c_hi:
            return round((aqi_hi - aqi_lo) / (c_hi - c_lo) * (pm25 - c_lo) + aqi_lo)
    return 500


class AQIProvider(ABC):
    """Return current AQI for a lat/lon coordinate."""

    @abstractmethod
    def current_aqi(self, latitude: float, longitude: float) -> int | None:
        """Return the current AQI, or None if the data is unavailable."""


class MockAQIProvider(AQIProvider):
    """Fixed AQI value. Used by tests."""

    def __init__(self, aqi: int | None = None) -> None:
        self._aqi = aqi

    def current_aqi(self, latitude: float, longitude: float) -> int | None:
        return self._aqi


class CachedAQIProvider(AQIProvider):
    """Wraps another provider, returning the cached value for `cache_minutes` minutes.

    AQI data is updated hourly or less frequently; caching avoids hitting the
    API on every 15-minute daemon cycle.
    """

    def __init__(self, provider: AQIProvider, cache_minutes: int = 30) -> None:
        self._provider = provider
        self._ttl = timedelta(minutes=cache_minutes)
        self._value: int | None = None
        self._fetched_at: datetime | None = None

    def current_aqi(self, latitude: float, longitude: float) -> int | None:
        now = datetime.now()
        if self._fetched_at is not None and (now - self._fetched_at) < self._ttl:
            return self._value
        self._value = self._provider.current_aqi(latitude, longitude)
        self._fetched_at = now
        return self._value


class AirNowProvider(AQIProvider):
    """EPA AirNow current-conditions observations.

    Free API key at https://www.airnowapi.org — no billing required.
    Returns the highest AQI across all reported parameters (PM2.5, O3, PM10)
    within `distance_miles` of the coordinate.
    """

    def __init__(
        self,
        api_key: str,
        *,
        distance_miles: int = 25,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.distance_miles = distance_miles
        self._client = client

    def current_aqi(self, latitude: float, longitude: float) -> int | None:
        params = {
            "format": "application/json",
            "latitude": latitude,
            "longitude": longitude,
            "distance": self.distance_miles,
            "API_KEY": self.api_key,
        }
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        own = self._client is None
        try:
            r = client.get(AIRNOW_BASE, params=params)
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            aqis = [item["AQI"] for item in data if item.get("AQI") is not None]
            return max(aqis) if aqis else None
        except Exception:
            return None
        finally:
            if own:
                client.close()


class PurpleAirProvider(AQIProvider):
    """PurpleAir community sensor network.

    API key at https://develop.purpleair.com — free tier available.
    Averages PM2.5 readings from nearby outdoor sensors and converts to AQI
    using the EPA PM2.5 breakpoints.
    """

    def __init__(
        self,
        api_key: str,
        *,
        radius_degrees: float = 0.5,
        max_age_seconds: int = 3600,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self._radius = radius_degrees
        self._max_age = max_age_seconds
        self._client = client

    def current_aqi(self, latitude: float, longitude: float) -> int | None:
        params = {
            "fields": "pm2.5_atm",
            "location_type": "0",  # outdoor sensors only
            "nwlng": longitude - self._radius,
            "nwlat": latitude + self._radius,
            "selng": longitude + self._radius,
            "selat": latitude - self._radius,
            "max_age": self._max_age,
        }
        headers = {"X-API-Key": self.api_key}
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        own = self._client is None
        try:
            r = client.get(PURPLEAIR_BASE, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            fields: list[str] = data.get("fields", [])
            sensors: list[list] = data.get("data", [])
            if "pm2.5_atm" not in fields or not sensors:
                return None
            idx = fields.index("pm2.5_atm")
            values = [s[idx] for s in sensors if s[idx] is not None]
            if not values:
                return None
            return _pm25_to_aqi(sum(values) / len(values))
        except Exception:
            return None
        finally:
            if own:
                client.close()


def make_aqi_provider(cfg: AppConfig) -> AQIProvider | None:
    """Build a CachedAQIProvider from AppConfig.aqi, or None if AQI is not configured."""
    aqi_cfg = cfg.aqi
    if aqi_cfg.provider == "airnow":
        if not aqi_cfg.airnow_api_key:
            return None
        return CachedAQIProvider(AirNowProvider(aqi_cfg.airnow_api_key))
    if aqi_cfg.provider == "purpleair":
        if not aqi_cfg.purpleair_api_key:
            return None
        return CachedAQIProvider(PurpleAirProvider(aqi_cfg.purpleair_api_key))
    return None
