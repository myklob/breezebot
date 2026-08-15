"""Tests for the US Census geocoder integration."""
from __future__ import annotations

import httpx
import pytest

from nightcool.geocode import GeocodeError, GeocodeUnavailable, geocode


def test_geocode_parses_first_match():
    payload = {
        "result": {
            "addressMatches": [
                {
                    "matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
                    "coordinates": {"x": -77.0365, "y": 38.8977},
                }
            ]
        }
    }
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = geocode("1600 Pennsylvania Ave NW", client=client)
    assert r.latitude == pytest.approx(38.8977)
    assert r.longitude == pytest.approx(-77.0365)
    assert "PENNSYLVANIA" in r.matched_address


def test_geocode_no_match_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"addressMatches": []}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GeocodeError, match="No match"):
        geocode("nowhere at all", client=client)


def test_geocode_5xx_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GeocodeUnavailable, match="unavailable"):
        geocode("anywhere", client=client)


def test_geocode_4xx_raises_plain_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GeocodeError, match="failed"):
        geocode("anywhere", client=client)


def test_geocode_network_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GeocodeUnavailable, match="unreachable"):
        geocode("anywhere", client=client)
