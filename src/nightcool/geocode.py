"""Address → (latitude, longitude) using the US Census Geocoder.

The Census Geocoder is free, requires no API key, and matches NWS's
US-only coverage. For non-US addresses we'd want a different provider
(Nominatim, Mapbox, etc.) — that's a v2 task.

Docs: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.pdf
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
HTTP_TIMEOUT_S = 10.0


class GeocodeError(RuntimeError):
    """The address could not be resolved."""


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    matched_address: str


def geocode(address: str, *, client: httpx.Client | None = None) -> GeocodeResult:
    """Resolve `address` to coordinates. Raises GeocodeError on no match.

    Picks the first match the Census API returns; if you live somewhere
    with an ambiguous address (e.g., on a county line), you may need to
    spell out the ZIP.
    """
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    own = client is None
    c = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
    try:
        r = c.get(CENSUS_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise GeocodeError(f"Census Geocoder request failed: {e}") from e
    finally:
        if own:
            c.close()

    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        raise GeocodeError(f"No match for address: {address!r}")
    m = matches[0]
    coords = m.get("coordinates") or {}
    try:
        return GeocodeResult(
            latitude=float(coords["y"]),
            longitude=float(coords["x"]),
            matched_address=m.get("matchedAddress", address),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise GeocodeError(f"Malformed Census response: {data!r}") from e
