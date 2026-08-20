"""Weather providers.

NWSProvider hits api.weather.gov; MockWeatherProvider returns canned data for tests.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx

from .engine import HourlyForecast


NWS_BASE = "https://api.weather.gov"
HTTP_TIMEOUT_S = 10.0
HTTP_MAX_ATTEMPTS = 2  # One try plus one retry.

WIND_DIRECTIONS_DEG: dict[str, float] = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
    "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}


def parse_wind_speed_mph(s: str | None) -> float:
    """Parse a NWS wind string like '10 mph' or '5 to 10 mph' to a float in mph.

    Returns the upper bound when a range is given (worst case for our gate logic).
    """
    if not s:
        return 0.0
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return 0.0
    return float(nums[-1])


def parse_wind_direction_deg(s: str | None) -> float | None:
    """Convert a cardinal-direction string like 'NNW' to degrees.

    NWS emits an empty string for calm/variable hours. Return None for that
    and for anything unrecognized — "unknown", not "due north" — so the
    bad-wind-sector gate treats it as a pass-through rather than falsely
    matching a sector centered on 0°.
    """
    if not s:
        return None
    return WIND_DIRECTIONS_DEG.get(s.strip().upper())


class WeatherProvider(ABC):
    """Abstract base; implementations return a normalized hourly forecast."""

    @abstractmethod
    def hourly_forecast(self, hours: int = 12) -> list[HourlyForecast]:
        """Return up to `hours` HourlyForecast records starting from now."""


class MockWeatherProvider(WeatherProvider):
    """Returns a pre-baked list of HourlyForecast objects. Used by tests."""

    def __init__(self, hours: list[HourlyForecast]) -> None:
        self._hours = list(hours)

    def hourly_forecast(self, hours: int = 12) -> list[HourlyForecast]:
        return list(self._hours[:hours])


class NWSProvider(WeatherProvider):
    """National Weather Service hourly forecast.

    NWS requires a two-step lookup: first resolve the gridpoint URL from
    lat/lon, then fetch the hourly forecast. NWS asks API consumers to send
    a descriptive User-Agent header.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        user_agent: str = "nightcool/0.1 (https://github.com/myklob/breezebot)",
        client: httpx.Client | None = None,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.user_agent = user_agent
        self._client = client
        self._forecast_url: str | None = None

    def _get(self, url: str) -> dict[str, Any]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json"}
        last_exc: Exception | None = None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers)
        own_client = self._client is None
        try:
            for _ in range(HTTP_MAX_ATTEMPTS):
                try:
                    r = client.get(url, headers=headers)
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPError as e:
                    last_exc = e
            if last_exc is None:
                raise RuntimeError("HTTP_MAX_ATTEMPTS must be > 0")
            raise last_exc
        finally:
            if own_client:
                client.close()

    def _resolve_forecast_url(self) -> str:
        if self._forecast_url is not None:
            return self._forecast_url
        meta = self._get(f"{NWS_BASE}/points/{self.latitude},{self.longitude}")
        url = meta["properties"]["forecastHourly"]
        self._forecast_url = url
        return url

    def hourly_forecast(self, hours: int = 12) -> list[HourlyForecast]:
        data = self._get(self._resolve_forecast_url())
        periods = data["properties"]["periods"][:hours]
        return [self._parse_period(p) for p in periods]

    @staticmethod
    def _parse_period(period: dict[str, Any]) -> HourlyForecast:
        ts = datetime.fromisoformat(period["startTime"])
        temp = float(period["temperature"])
        if period.get("temperatureUnit") == "C":
            temp = temp * 9.0 / 5.0 + 32.0

        rain_obj = period.get("probabilityOfPrecipitation") or {}
        rain_val = rain_obj.get("value")
        rain_pct = float(rain_val) if rain_val is not None else 0.0

        wind_mph = parse_wind_speed_mph(period.get("windSpeed"))
        gust_str = period.get("windGust")
        gust_mph = parse_wind_speed_mph(gust_str) if gust_str else wind_mph
        wind_dir = parse_wind_direction_deg(period.get("windDirection"))

        dew_point_f = _parse_dewpoint_f(period.get("dewpoint"))

        return HourlyForecast(
            timestamp=ts,
            temperature_f=temp,
            wind_speed_mph=wind_mph,
            wind_gust_mph=gust_mph,
            wind_direction_deg=wind_dir,
            rain_chance_pct=rain_pct,
            dew_point_f=dew_point_f,
        )


def _parse_dewpoint_f(dewpoint: dict[str, Any] | None) -> float | None:
    """Extract NWS dew point and normalize to °F. NWS reports dewpoint as
    `{"unitCode": "wmoUnit:degC", "value": 12.2}` — degrees Celsius by
    convention even when the rest of the forecast is in Fahrenheit. Older
    fixtures omit the field; treat that as unknown rather than 0.
    """
    if not dewpoint:
        return None
    value = dewpoint.get("value")
    if value is None:
        return None
    unit_code = (dewpoint.get("unitCode") or "").lower()
    val_f = float(value)
    if "degc" in unit_code or unit_code == "":
        # NWS default unit is Celsius; assume C when unit is missing.
        val_f = val_f * 9.0 / 5.0 + 32.0
    return val_f
