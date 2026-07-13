"""Thermal model + per-poll data logging.

We log every poll cycle to a tiny SQLite database, then fit a three-coefficient
linear model when enough samples accumulate:

    dT_indoor/dt = α · (T_out - T_in)     # ventilation when windows are open
                 + β · (solar_load - T_in) # passive solar gain/loss
                 + γ · HVAC_active         # AC effect when running

`fit_model()` returns `None` until there are at least `MIN_SAMPLES`
observations spanning at least a day; everything downstream that prices
savings must check for None first.

The schema is deliberately denormalized — the whole point is to make ad-hoc
SQL queries trivial when debugging "why didn't it open last night?".
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


logger = logging.getLogger("nightcool.thermal")

# Need this many rows before a fit is worth attempting.
MIN_SAMPLES = 96  # roughly 24 hours at 15-minute polls.
# How long the data has to span before we trust the fit.
MIN_DURATION_HOURS = 18.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    ts            TEXT    NOT NULL PRIMARY KEY,
    indoor_f      REAL    NOT NULL,
    outdoor_f     REAL,
    wind_mph      REAL,
    rain_pct      REAL,
    action        TEXT,
    windows_open  INTEGER,
    hvac_active   INTEGER,
    indoor_source TEXT
);

CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations (ts);
"""


@dataclass(frozen=True)
class Observation:
    ts: datetime
    indoor_f: float
    outdoor_f: float | None
    wind_mph: float | None
    rain_pct: float | None
    action: str | None
    windows_open: bool | None
    hvac_active: bool | None
    indoor_source: str | None = None


@dataclass(frozen=True)
class ThermalModel:
    """Fitted coefficients in units of °F/hour per °F driving delta."""

    alpha_ventilation: float
    beta_solar: float
    gamma_hvac: float
    sample_count: int
    duration_hours: float
    r_squared: float


def connect(path: Path) -> sqlite3.Connection:
    """Open (and lazily initialize) the SQLite store at `path`."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def log_observation(conn: sqlite3.Connection, obs: Observation) -> None:
    """Insert one row. The PK is the timestamp, so duplicate polls upsert."""
    conn.execute(
        """
        INSERT OR REPLACE INTO observations
          (ts, indoor_f, outdoor_f, wind_mph, rain_pct, action,
           windows_open, hvac_active, indoor_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            obs.ts.isoformat(),
            obs.indoor_f,
            obs.outdoor_f,
            obs.wind_mph,
            obs.rain_pct,
            obs.action,
            int(obs.windows_open) if obs.windows_open is not None else None,
            int(obs.hvac_active) if obs.hvac_active is not None else None,
            obs.indoor_source,
        ),
    )
    conn.commit()


def load_observations(conn: sqlite3.Connection) -> list[Observation]:
    """Return all rows in chronological order."""
    cur = conn.execute(
        """
        SELECT ts, indoor_f, outdoor_f, wind_mph, rain_pct, action,
               windows_open, hvac_active, indoor_source
        FROM observations
        ORDER BY ts ASC
        """
    )
    out: list[Observation] = []
    for row in cur.fetchall():
        out.append(
            Observation(
                ts=datetime.fromisoformat(row[0]),
                indoor_f=row[1],
                outdoor_f=row[2],
                wind_mph=row[3],
                rain_pct=row[4],
                action=row[5],
                windows_open=bool(row[6]) if row[6] is not None else None,
                hvac_active=bool(row[7]) if row[7] is not None else None,
                indoor_source=row[8],
            )
        )
    return out


def _solve_least_squares(rows: list[tuple[float, float, float, float]]) -> tuple[tuple[float, float, float], float]:
    """Solve a 3-feature OLS regression by hand, no numpy dependency.

    rows is a list of (dT_dt, vent_driver, solar_driver, hvac_indicator).
    Returns ((alpha, beta, gamma), r_squared).
    """
    n = len(rows)
    # Build X^T X (3x3) and X^T y (3,) by accumulation.
    full_a = [[0.0] * 3 for _ in range(3)]
    full_b = [0.0, 0.0, 0.0]
    y_mean = sum(r[0] for r in rows) / n
    tss = 0.0
    for dy, x1, x2, x3 in rows:
        xs = (x1, x2, x3)
        for i in range(3):
            full_b[i] += xs[i] * dy
            for j in range(3):
                full_a[i][j] += xs[i] * xs[j]
        tss += (dy - y_mean) ** 2

    # A feature whose column is all zeros (e.g. windows_open is never
    # sensed, so the vent driver is 0 everywhere) contributes a zero row and
    # column; drop it and fit the remaining features instead of bailing out.
    active = [i for i in range(3) if full_a[i][i] > 1e-12]
    m = len(active)
    if m == 0:
        return ((0.0, 0.0, 0.0), 0.0)
    a = [[full_a[i][j] for j in active] for i in active]
    b = [full_b[i] for i in active]

    # Solve the reduced m x m system via Gaussian elimination.
    for k in range(m):
        # Partial pivot for numerical stability.
        pivot = max(range(k, m), key=lambda i: abs(a[i][k]))
        if pivot != k:
            a[k], a[pivot] = a[pivot], a[k]
            b[k], b[pivot] = b[pivot], b[k]
        if abs(a[k][k]) < 1e-12:
            return ((0.0, 0.0, 0.0), 0.0)
        for i in range(k + 1, m):
            f = a[i][k] / a[k][k]
            for j in range(k, m):
                a[i][j] -= f * a[k][j]
            b[i] -= f * b[k]
    reduced = [0.0] * m
    for i in range(m - 1, -1, -1):
        reduced[i] = (b[i] - sum(a[i][j] * reduced[j] for j in range(i + 1, m))) / a[i][i]
    coefs = [0.0, 0.0, 0.0]
    for idx, coef in zip(active, reduced):
        coefs[idx] = coef
    alpha, beta, gamma = coefs

    rss = 0.0
    for dy, x1, x2, x3 in rows:
        pred = alpha * x1 + beta * x2 + gamma * x3
        rss += (dy - pred) ** 2
    r2 = 1.0 - rss / tss if tss > 0 else 0.0
    return ((alpha, beta, gamma), r2)


def fit_model(obs: Iterable[Observation]) -> ThermalModel | None:
    """Fit the α/β/γ thermal model. Returns None until enough data exists.

    Uses a crude solar proxy of (50 - outdoor_f) because we don't have a
    pyranometer; ventilation is gated by `windows_open`. This is a v1
    placeholder — refine the feature engineering once you have a few
    weeks of real data.
    """
    pts = list(obs)
    if len(pts) < MIN_SAMPLES:
        return None
    duration = (pts[-1].ts - pts[0].ts).total_seconds() / 3600.0
    if duration < MIN_DURATION_HOURS:
        return None

    rows: list[tuple[float, float, float, float]] = []
    for prev, cur in zip(pts, pts[1:]):
        if cur.outdoor_f is None or prev.outdoor_f is None:
            continue
        dt_hours = (cur.ts - prev.ts).total_seconds() / 3600.0
        if dt_hours <= 0 or dt_hours > 1.0:
            continue
        dT = (cur.indoor_f - prev.indoor_f) / dt_hours
        windows_open = 1.0 if (cur.windows_open or prev.windows_open) else 0.0
        vent = windows_open * (cur.outdoor_f - prev.indoor_f)
        solar = 50.0 - cur.outdoor_f  # rough solar load stand-in.
        hvac = 1.0 if (cur.hvac_active or prev.hvac_active) else 0.0
        rows.append((dT, vent, solar, hvac))

    if len(rows) < MIN_SAMPLES // 2:
        return None
    (alpha, beta, gamma), r2 = _solve_least_squares(rows)
    return ThermalModel(
        alpha_ventilation=alpha,
        beta_solar=beta,
        gamma_hvac=gamma,
        sample_count=len(rows),
        duration_hours=duration,
        r_squared=r2,
    )


def hours_recommended_open(obs: Iterable[Observation]) -> float:
    """Total hours the engine's recommendation stood at "open".

    Each observation is one poll sample, so an "open" row covers the gap to
    the next sample — not a fixed block of time. Gaps over an hour mean the
    daemon was down; don't count them.
    """
    pts = list(obs)
    total = 0.0
    for prev, cur in zip(pts, pts[1:]):
        if prev.action != "open":
            continue
        dt_hours = (cur.ts - prev.ts).total_seconds() / 3600.0
        if 0.0 < dt_hours <= 1.0:
            total += dt_hours
    return total


# ---- Savings ----

# Typical residential split-system AC: ~3.5 kW input for 12k BTU/h cooling.
AC_KW_INPUT = 3.5
# Default electricity price ($/kWh). Override per house.
DEFAULT_PRICE_PER_KWH = 0.17


def estimate_savings(
    model: ThermalModel | None,
    hours_avoided: float,
    price_per_kwh: float = DEFAULT_PRICE_PER_KWH,
) -> tuple[float, float]:
    """Return (kWh saved, dollars saved) for `hours_avoided` of AC runtime.

    Independent of `model` for now — but signature accepts one so the
    caller can pass through richer estimates later (e.g., COP variation
    by outdoor temp).
    """
    kwh = AC_KW_INPUT * max(0.0, hours_avoided)
    return kwh, kwh * price_per_kwh
