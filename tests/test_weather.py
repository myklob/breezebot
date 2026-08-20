"""Tests for the NWS parser, using the bundled fixture."""
from __future__ import annotations

import json
from pathlib import Path

from nightcool.weather import (
    NWSProvider,
    parse_wind_direction_deg,
    parse_wind_speed_mph,
)


FIXTURE = Path(__file__).parent / "fixtures" / "nws_sample.json"


def test_parse_wind_speed_single_value():
    assert parse_wind_speed_mph("8 mph") == 8.0


def test_parse_wind_speed_range_returns_upper():
    # Range strings like "5 to 10 mph" — we take the upper bound (worst case).
    assert parse_wind_speed_mph("5 to 10 mph") == 10.0


def test_parse_wind_speed_empty():
    assert parse_wind_speed_mph(None) == 0.0
    assert parse_wind_speed_mph("") == 0.0


def test_parse_wind_direction_known_and_unknown():
    assert parse_wind_direction_deg("N") == 0.0
    assert parse_wind_direction_deg("ESE") == 112.5
    assert parse_wind_direction_deg("nw") == 315.0
    assert parse_wind_direction_deg("WAT") is None  # unknown → None
    assert parse_wind_direction_deg("") is None  # calm/variable → None


def test_nws_period_parser_handles_fixture():
    data = json.loads(FIXTURE.read_text())
    hours = [NWSProvider._parse_period(p) for p in data["properties"]["periods"]]
    assert len(hours) == 5

    first = hours[0]
    assert first.temperature_f == 68.0
    assert first.wind_speed_mph == 8.0
    assert first.wind_direction_deg == 315.0  # NW
    assert first.rain_chance_pct == 5.0

    # "5 to 10 mph" → upper bound 10.
    assert hours[3].wind_speed_mph == 10.0
    # "S" → 180°.
    assert hours[3].wind_direction_deg == 180.0
    # null probabilityOfPrecipitation → 0.
    assert hours[4].rain_chance_pct == 0.0
    # Fixture predates the dewpoint field; parser should report None rather
    # than fabricating a value.
    assert all(h.dew_point_f is None for h in hours)


def test_nws_period_parser_dewpoint_celsius_to_fahrenheit():
    period = {
        "startTime": "2024-06-15T22:00:00-06:00",
        "temperature": 68,
        "temperatureUnit": "F",
        "probabilityOfPrecipitation": {"value": 5},
        "windSpeed": "8 mph",
        "windDirection": "NW",
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": 15.0},
    }
    hour = NWSProvider._parse_period(period)
    # 15 °C → 59 °F.
    assert hour.dew_point_f == 59.0


def test_nws_period_parser_dewpoint_missing_value_is_none():
    period = {
        "startTime": "2024-06-15T22:00:00-06:00",
        "temperature": 68,
        "temperatureUnit": "F",
        "probabilityOfPrecipitation": {"value": 5},
        "windSpeed": "8 mph",
        "windDirection": "NW",
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": None},
    }
    hour = NWSProvider._parse_period(period)
    assert hour.dew_point_f is None
