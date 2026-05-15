"""Typer CLI: check, forecast, daemon, set-indoor, test-notify."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from .config import AppConfig, load_config
from .daemon import format_notification, read_indoor_temp, run_daemon, run_once
from .engine import decide_actions
from .notifier import make_notifier
from .state import read_state, set_indoor_temp, write_state
from .weather import NWSProvider


app = typer.Typer(help="NightCool: free overnight cooling assistant.", no_args_is_help=True)

DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_STATE = Path("state.json")


def _load(path: Path) -> AppConfig:
    if not path.exists():
        raise typer.BadParameter(f"Config file not found: {path}")
    return load_config(path)


@app.command()
def check(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
) -> None:
    """Run one decision cycle without sending notifications and print the result."""
    cfg = _load(config)
    tz = ZoneInfo(cfg.location.timezone)
    now = datetime.now(tz)
    st = read_state(state)
    indoor = read_indoor_temp(cfg, st)
    provider = NWSProvider(cfg.location.latitude, cfg.location.longitude)
    forecast = provider.hourly_forecast(hours=12)
    rec = decide_actions(indoor, forecast, cfg.windows, cfg.user_prefs, now)
    title, body = format_notification(rec)
    typer.echo(f"Indoor: {indoor:.1f}°F")
    typer.echo(f"Action: {rec.action}")
    typer.echo(f"Title:  {title}")
    typer.echo(f"Reason: {body}")
    if rec.eligible_windows:
        typer.echo(f"Windows: {', '.join(rec.eligible_windows)}")
    if rec.open_at:
        typer.echo(f"Open at:  {rec.open_at}")
    if rec.close_at:
        typer.echo(f"Close at: {rec.close_at}")


@app.command()
def forecast(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c")) -> None:
    """Print the 12-hour NWS forecast as a plain table."""
    cfg = _load(config)
    provider = NWSProvider(cfg.location.latitude, cfg.location.longitude)
    hours = provider.hourly_forecast(hours=12)
    typer.echo(f"{'Time':<25} {'Temp°F':>7} {'Wind':>6} {'Gust':>6} {'Dir°':>5} {'Rain%':>6}")
    for h in hours:
        typer.echo(
            f"{h.timestamp.strftime('%Y-%m-%d %H:%M %Z'):<25} "
            f"{h.temperature_f:>7.1f} "
            f"{h.wind_speed_mph:>6.1f} "
            f"{h.wind_gust_mph:>6.1f} "
            f"{h.wind_direction_deg:>5.0f} "
            f"{h.rain_chance_pct:>6.0f}"
        )


@app.command()
def daemon(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the polling loop until interrupted."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load(config)
    run_daemon(cfg, state)


@app.command()
def poll_once(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
) -> None:
    """Run a single poll cycle (including notification) and exit. Useful for cron."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = _load(config)
    rec = run_once(cfg, state)
    typer.echo(f"Action: {rec.action}")


@app.command("set-indoor")
def set_indoor(
    temp: float = typer.Argument(..., help="Indoor temperature in °F"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
) -> None:
    """Record the current indoor temperature."""
    st = read_state(state)
    set_indoor_temp(st, temp, datetime.now())
    write_state(state, st)
    typer.echo(f"Indoor temp set to {temp:.1f}°F")


@app.command("test-notify")
def test_notify(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c")) -> None:
    """Send a test notification through the configured backend."""
    cfg = _load(config)
    notifier = make_notifier(cfg.notifications)
    notifier.send("NightCool test", "If you see this, the notification pipe works.")
    typer.echo("Sent.")


if __name__ == "__main__":
    app()
