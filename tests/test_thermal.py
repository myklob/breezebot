"""Tests for the SQLite data log and the α/β/γ regression."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from nightcool.thermal import (
    MIN_SAMPLES,
    Observation,
    connect,
    estimate_savings,
    fit_model,
    load_observations,
    log_observation,
)


def test_log_and_load_roundtrip(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    obs = Observation(
        ts=base,
        indoor_f=72.0,
        outdoor_f=60.0,
        wind_mph=5.0,
        rain_pct=0.0,
        action="open",
        windows_open=True,
        hvac_active=False,
        indoor_source="ManualSource",
    )
    log_observation(conn, obs)
    rows = load_observations(conn)
    assert len(rows) == 1
    assert rows[0].indoor_f == 72.0
    assert rows[0].windows_open is True


def test_duplicate_timestamp_upserts(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    log_observation(conn, Observation(ts=base, indoor_f=70.0, outdoor_f=60.0,
                                       wind_mph=0, rain_pct=0, action="x",
                                       windows_open=False, hvac_active=False))
    log_observation(conn, Observation(ts=base, indoor_f=99.0, outdoor_f=60.0,
                                       wind_mph=0, rain_pct=0, action="x",
                                       windows_open=False, hvac_active=False))
    rows = load_observations(conn)
    assert len(rows) == 1
    assert rows[0].indoor_f == 99.0


def test_fit_model_returns_none_when_undersampled(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    for i in range(10):
        log_observation(conn, Observation(
            ts=base + timedelta(minutes=15 * i),
            indoor_f=72.0, outdoor_f=60.0,
            wind_mph=0, rain_pct=0, action="x",
            windows_open=False, hvac_active=False,
        ))
    assert fit_model(load_observations(conn)) is None


def test_fit_model_recovers_synthetic_coefficients(tmp_path):
    # Generate samples from a process where all three features carry signal.
    random.seed(0)
    rows: list[Observation] = []
    base = datetime(2024, 6, 15, tzinfo=timezone.utc)
    indoor = 72.0
    for i in range(MIN_SAMPLES + 40):
        outdoor = 60.0 + 8.0 * math.sin(i / 6.0)
        windows_open = (i % 8) < 4
        hvac = (i % 20) < 5  # Forced periodic HVAC cycling, independent of indoor.
        vent = (1.0 if windows_open else 0.0) * (outdoor - indoor)
        solar = 50.0 - outdoor
        # 15-min step; coefficients are in °F/hr so multiply by 0.25.
        dT = (1.6 * vent + 0.1 * solar + (-5.0 if hvac else 0.0)) * 0.25
        indoor += dT + random.gauss(0, 0.05)
        rows.append(Observation(
            ts=base + timedelta(minutes=15 * i),
            indoor_f=indoor,
            outdoor_f=outdoor,
            wind_mph=0, rain_pct=0, action="open" if windows_open else "no_change",
            windows_open=windows_open, hvac_active=hvac,
        ))
    model = fit_model(rows)
    assert model is not None
    assert model.r_squared > 0.5
    # Coefficients should be in the right ballpark.
    assert model.alpha_ventilation > 0  # ventilation cools when outdoor < indoor.
    assert model.gamma_hvac < 0          # HVAC removes heat.


def test_estimate_savings_scales_with_hours():
    kwh, dollars = estimate_savings(None, hours_avoided=2.0, price_per_kwh=0.20)
    assert kwh > 0
    assert dollars == kwh * 0.20

    kwh_zero, _ = estimate_savings(None, hours_avoided=-1.0)
    assert kwh_zero == 0.0
