"""Indoor temperature sources.

Each source returns a single float (°F) — `current_temperature()` — or
raises `SourceUnavailable` if the reading is missing/stale. The daemon
chains sources in priority order and falls back to the manual value when
none succeed.

v1 ships working `manual` and `sensor_file` sources. The `nest` and `ble`
sources are stubbed: they hold the config plumbing and document the
integration path without bringing in the OAuth/Bluetooth dependencies at
import time. Drop in the real implementation when you wire your house up.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from .config import (
    BLESensorConfig,
    IndoorSourceKind,
    IndoorTempConfig,
    NestConfig,
)


logger = logging.getLogger("nightcool.sources")
SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"
HTTP_TIMEOUT_S = 10.0


class SourceUnavailable(RuntimeError):
    """The source could not produce a current reading."""


class IndoorTempSource(ABC):
    """A source of current indoor temperature (°F)."""

    @abstractmethod
    def current_temperature(self) -> float:
        ...


class ManualSource(IndoorTempSource):
    """User-entered temperature stored in state.json.

    The caller passes a reader closure rather than the state dict itself so
    that this module stays free of state.json import order issues.
    """

    def __init__(self, reader: "callable[[], float | None]", default_f: float) -> None:
        self._reader = reader
        self._default = default_f

    def current_temperature(self) -> float:
        val = self._reader()
        return float(val) if val is not None else float(self._default)


class SensorFileSource(IndoorTempSource):
    """A separate process writes a float (°F) to `path`; we read it.

    Pairs well with a tiny BLE reader script that polls a Govee H5075 or
    similar and rewrites the file every minute.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def current_temperature(self) -> float:
        if not self.path.exists():
            raise SourceUnavailable(f"sensor file missing: {self.path}")
        try:
            return float(self.path.read_text(encoding="utf-8").strip())
        except ValueError as e:
            raise SourceUnavailable(f"sensor file {self.path} did not contain a float") from e


class NestSource(IndoorTempSource):
    """Google Smart Device Management API.

    Requires a Device Access project ($5 one-time), an OAuth client, and a
    long-lived refresh token. The endpoint we poll is `devices.get` on the
    configured thermostat; the response's
    `sdm.devices.traits.Temperature.ambientTemperatureCelsius` is what we
    convert to °F.

    This implementation is wired but unproven against a live account —
    expect to debug the OAuth handshake the first time you run it.
    """

    def __init__(self, cfg: NestConfig, *, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._client = client
        self._access_token: str | None = None

    def _ensure_complete(self) -> None:
        missing = [
            k for k in ("project_id", "client_id", "client_secret", "refresh_token", "device_id")
            if not getattr(self.cfg, k)
        ]
        if missing:
            raise SourceUnavailable(f"nest config missing fields: {', '.join(missing)}")

    def _refresh_access_token(self) -> str:
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        try:
            r = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.cfg.client_id,
                    "client_secret": self.cfg.client_secret,
                    "refresh_token": self.cfg.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            r.raise_for_status()
            token = r.json()["access_token"]
            self._access_token = token
            return token
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise SourceUnavailable(f"nest token refresh failed: {e}") from e
        finally:
            if self._client is None:
                client.close()

    def current_temperature(self) -> float:
        self._ensure_complete()
        token = self._access_token or self._refresh_access_token()
        url = (
            f"{SDM_BASE}/enterprises/{self.cfg.project_id}"
            f"/devices/{self.cfg.device_id}"
        )
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        try:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                token = self._refresh_access_token()
                r = client.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data: dict[str, Any] = r.json()
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"nest API error: {e}") from e
        finally:
            if self._client is None:
                client.close()

        traits = data.get("traits", {})
        temp_c = traits.get("sdm.devices.traits.Temperature", {}).get(
            "ambientTemperatureCelsius"
        )
        if temp_c is None:
            raise SourceUnavailable("nest response missing ambientTemperatureCelsius")
        return float(temp_c) * 9.0 / 5.0 + 32.0


class BLESource(IndoorTempSource):
    """A BLE thermometer (Govee, SwitchBot, ThermoPro, Inkbird, …).

    v1 does not embed a Bluetooth stack. Instead, a tiny external reader —
    one example below — writes the most recent reading to `cache_file`,
    and we read it.

    Example reader (Govee H5075 with `bleak`):
        async with BleakScanner() as scanner:
            ad = await scanner.find_device_by_address(MAC)
            ...
            cache_file.write_text(f"{temperature_f:.2f}")

    The intent is to keep the daemon process simple and let the reader
    handle reconnect logic, OS permissions, and adapter quirks.
    """

    def __init__(self, cfg: BLESensorConfig) -> None:
        self.cfg = cfg

    def current_temperature(self) -> float:
        if not self.cfg.cache_file:
            raise SourceUnavailable("ble cache_file not configured")
        path = Path(self.cfg.cache_file)
        if not path.exists():
            raise SourceUnavailable(f"ble cache file missing: {path}")
        try:
            return float(path.read_text(encoding="utf-8").strip())
        except ValueError as e:
            raise SourceUnavailable(f"ble cache file {path} not a float") from e


def make_source(
    cfg: IndoorTempConfig,
    manual_reader: "callable[[], float | None]",
) -> IndoorTempSource:
    """Build the configured indoor-temp source."""
    if cfg.source == IndoorSourceKind.SENSOR_FILE:
        if not cfg.sensor_file_path:
            raise ValueError("indoor_temp.sensor_file_path required for source=sensor_file")
        return SensorFileSource(cfg.sensor_file_path)
    if cfg.source == IndoorSourceKind.NEST:
        if not cfg.nest:
            raise ValueError("indoor_temp.nest section required for source=nest")
        return NestSource(cfg.nest)
    if cfg.source == IndoorSourceKind.BLE:
        if not cfg.ble:
            raise ValueError("indoor_temp.ble section required for source=ble")
        return BLESource(cfg.ble)
    return ManualSource(manual_reader, cfg.manual_default_f)


def read_indoor_with_fallback(
    primary: IndoorTempSource,
    fallback_f: float,
) -> tuple[float, str]:
    """Try `primary`; fall back to `fallback_f` on `SourceUnavailable`.

    Returns (value, source_used) so callers can log/show provenance.
    """
    try:
        return primary.current_temperature(), primary.__class__.__name__
    except SourceUnavailable as e:
        logger.warning("Indoor source unavailable (%s); using fallback %.1f°F", e, fallback_f)
        return float(fallback_f), "fallback"
