"""Typer CLI: check, forecast, daemon, set-indoor, test-notify, serve, …"""
from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
import yaml

from .config import WEEKDAY_KEYS, AppConfig, load_config
from .daemon import (
    _build_notifier,
    format_notification,
    read_indoor_temp,
    resolve_coordinates,
    run_daemon,
    run_once,
)
from .engine import decide_actions
from .geocode import GeocodeError, geocode as do_geocode
from .notifier import generate_vapid_keys
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
    indoor, source_name = read_indoor_temp(cfg, st)
    lat, lon = resolve_coordinates(cfg, st)
    write_state(state, st)
    provider = NWSProvider(lat, lon)
    forecast = provider.hourly_forecast(hours=12)
    rec = decide_actions(
        indoor, forecast, cfg.windows, now,
        schedule=cfg.schedule, prefs=cfg.prefs,
        comfort_floor=cfg.comfort_floor, warnings=cfg.warnings,
    )
    title, body = format_notification(rec)
    today = cfg.schedule.for_weekday(now.weekday())
    typer.echo(f"Indoor: {indoor:.1f}°F (source: {source_name})")
    typer.echo(f"Today's target: {today.target_f:.0f}°F"
               f"{', home all day' if today.home_all_day else ''}"
               f"{f', leave at {today.leave_at}' if today.leave_at else ''}")
    typer.echo(f"Action: {rec.action}")
    typer.echo(f"Title:  {title}")
    typer.echo(f"Reason: {body}")
    if rec.eligible_windows:
        typer.echo(f"Windows: {', '.join(rec.eligible_windows)}")
    if rec.open_at:
        typer.echo(f"Open at:  {rec.open_at}")
    if rec.close_at:
        typer.echo(f"Close at: {rec.close_at}")
    for w in rec.warnings or []:
        typer.echo(f"Warning: {w}")


@app.command()
def forecast(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
) -> None:
    """Print the 12-hour NWS forecast as a plain table."""
    cfg = _load(config)
    st = read_state(state)
    lat, lon = resolve_coordinates(cfg, st)
    write_state(state, st)
    provider = NWSProvider(lat, lon)
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
def test_notify(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
) -> None:
    """Send a test notification through the configured backend."""
    cfg = _load(config)
    notifier = _build_notifier(cfg, state)
    notifier.send("NightCool test", "If you see this, the notification pipe works.")
    typer.echo("Sent.")


@app.command()
def serve(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    state: Path = typer.Option(DEFAULT_STATE, "--state", "-s"),
    open_browser: bool = typer.Option(
        False, "--open-browser", help="Open the PWA in the default browser after start."
    ),
) -> None:
    """Run the HTTP API + PWA host. Pair with `daemon` in a separate process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = _load(config)
    from .web import run_server
    if open_browser:
        import threading, time as _time, webbrowser
        def _later() -> None:
            _time.sleep(1.0)
            webbrowser.open(f"http://{cfg.web.host}:{cfg.web.port}")
        threading.Thread(target=_later, daemon=True).start()
    run_server(cfg, state, config_path=config)


@app.command()
def geocode(
    address: str = typer.Argument(..., help='Street address, e.g. "1234 Main St, Denver CO"'),
    config: Path = typer.Option(None, "--config", "-c", help="If set, write the result back into config.yaml."),
) -> None:
    """Resolve an address to latitude/longitude via the US Census Geocoder.

    With --config, updates config.yaml's location section in place.
    """
    try:
        result = do_geocode(address)
    except GeocodeError as e:
        raise typer.Exit(f"Geocode failed: {e}")
    typer.echo(f"Matched: {result.matched_address}")
    typer.echo(f"Latitude:  {result.latitude}")
    typer.echo(f"Longitude: {result.longitude}")
    if config is not None and config.exists():
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw.setdefault("location", {})
        raw["location"]["address"] = result.matched_address
        raw["location"]["latitude"] = result.latitude
        raw["location"]["longitude"] = result.longitude
        config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        typer.echo(f"Wrote coordinates into {config}.")


@app.command("set-target")
def set_target(
    day: str = typer.Argument(..., help="mon/tue/wed/thu/fri/sat/sun, or 'all'"),
    target_f: float = typer.Argument(..., help="Desired indoor temperature in °F"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Set the target indoor temperature for a day (or all days)."""
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    sched = raw.setdefault("schedule", {})
    days = WEEKDAY_KEYS if day == "all" else (day,)
    for d in days:
        if d not in WEEKDAY_KEYS:
            raise typer.BadParameter(f"unknown day {d!r}; pick from {WEEKDAY_KEYS} or 'all'")
        sched.setdefault(d, {})["target_f"] = target_f
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    typer.echo(f"Set target_f={target_f} for {', '.join(days)}.")


@app.command("set-leave-time")
def set_leave_time(
    day: str = typer.Argument(..., help="mon/tue/wed/thu/fri/sat/sun, or 'weekdays'"),
    leave_at: str = typer.Argument(..., help='"HH:MM" or "home" for home_all_day'),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Set your typical departure time for a day so CLOSE pings know when to back off."""
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    sched = raw.setdefault("schedule", {})
    days = ("mon", "tue", "wed", "thu", "fri") if day == "weekdays" else (day,)
    for d in days:
        if d not in WEEKDAY_KEYS:
            raise typer.BadParameter(f"unknown day {d!r}")
        entry = sched.setdefault(d, {})
        if leave_at.lower() == "home":
            entry["home_all_day"] = True
            entry.pop("leave_at", None)
        else:
            time.fromisoformat(leave_at)  # Validate format.
            entry["leave_at"] = leave_at
            entry["home_all_day"] = False
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    typer.echo(f"Updated leave time for {', '.join(days)}.")


@app.command("web-push-keys")
def web_push_keys() -> None:
    """Generate a fresh VAPID keypair. Paste into config.yaml under notifications.web_push."""
    public, private = generate_vapid_keys()
    typer.echo("Paste into config.yaml under notifications.web_push:")
    typer.echo("")
    typer.echo("notifications:")
    typer.echo("  service: web_push")
    typer.echo("  web_push:")
    typer.echo(f"    vapid_public_key: \"{public}\"")
    typer.echo(f"    vapid_private_key: |")
    for line in private.splitlines():
        typer.echo(f"      {line}")
    typer.echo("    vapid_subject: \"mailto:you@example.com\"")


if __name__ == "__main__":
    app()
