# NightCool

A small Python service that tells you when (and which) windows to open for free
overnight cooling, then nudges you to close them before the morning sun cooks
the gains away. It polls the National Weather Service every 15 minutes,
compares the forecast to your indoor temperature, and sends a push
notification when there's a "free cooling" opportunity that won't blow rain
in, fly papers around, or roll the smell of the landfill through your
bedroom.

No cloud account, no smart-thermostat OAuth, no ML. Runs on a Raspberry Pi, an
old laptop, or a $5 VPS.

## Quickstart

```bash
git clone <this-repo>
cd breezebot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Edit config.yaml for your house (windows, location, ntfy topic).
$EDITOR config.yaml

# Tell it your current indoor temp (one-time; daemon will keep using it).
nightcool set-indoor 71

# Sanity check.
nightcool check
nightcool forecast

# Send a test notification.
nightcool test-notify

# Run the loop.
nightcool daemon
```

## How to find your lat/lon

Open [Google Maps](https://maps.google.com), right-click your house, and copy
the lat/lon that appears at the top of the menu. Drop those values into
`config.yaml` under `location.latitude` / `location.longitude`. NWS only
covers the US — for other countries, you'll need a different provider (v2).

Sanity check the NWS gridpoint exists:

```bash
curl -H "User-Agent: nightcool/0.1" "https://api.weather.gov/points/39.7392,-104.9903"
```

## How to set up ntfy notifications

ntfy.sh is free and requires no signup. Pick a topic name nobody else will
guess (it's effectively your password) — for example
`nightcool-jane-doe-3kh2g7q`.

1. In `config.yaml`, set `notifications.service: ntfy` and
   `notifications.ntfy_topic: <your-topic>`.
2. Install the [ntfy app](https://ntfy.sh/) on your phone.
3. In the app, subscribe to your topic.
4. Run `nightcool test-notify`. Your phone should buzz.

For Pushover instead: set `service: pushover` and fill in
`pushover_user_key` and `pushover_app_token`.

## Indoor temperature

v1 expects you to enter the indoor temperature manually:

```bash
nightcool set-indoor 72.5
```

The value is stored in `state.json` and used until you update it. For
something less tedious, point `indoor_temp.source: sensor_file` and
`indoor_temp.sensor_file_path` at a file your sensor writes a single float
to (e.g., a Govee H5075 BLE reader). Real thermostat integration is a v2
task.

## Configuration reference

See `config.yaml` for an annotated example. The non-obvious bits:

- `bad_wind_sector_deg: [lo, hi]` — wind directions you don't want blowing
  through the house (e.g., from the landfill). Wraps around: `[350, 10]`
  covers the 20° arc around true north.
- `windows[].exposure: exposed | covered | tiled` — controls rain
  sensitivity. Tiled (bathrooms) ignore rain entirely.
- `windows[].security: secure | unsecure` — unsecure windows get closed
  out of the recommendation when forecast gusts exceed `max_gust_mph`.
- `windows[].on_bad_wind_sector: true` — this window faces the bad sector;
  blocked when wind blows from there.
- `quiet_hours_*` suppress only OPEN notifications. CLOSE wakes you to
  save the cool air.

## Decision logic in one paragraph

Every 15 minutes, scan the next 12 hours of NWS hourly forecast. The
"open" moment is the first hour where outdoor temperature is at least
`hysteresis_f` below indoor temperature, at or above
`min_tolerable_outdoor_f`, no more than `sleep_target_f + 5` (so opening
is actually worth it), and wind isn't blowing from the bad sector. If
such a moment exists within the next two hours and at least one window
passes its eligibility check (rain, gusts, sector), send one OPEN
notification listing the eligible windows and a close-by time (the first
later hour where outdoor warms back above the threshold, or
`morning_close_time`, whichever is earlier). When outdoor crosses back
above the threshold, send one CLOSE notification. Don't repeat the same
notification twice. Quiet hours suppress OPEN but not CLOSE.

## systemd install

```bash
sudo useradd -r -s /usr/sbin/nologin nightcool
sudo mkdir -p /opt/nightcool
sudo chown nightcool:nightcool /opt/nightcool
sudo -u nightcool git clone <this-repo> /opt/nightcool
sudo -u nightcool python3 -m venv /opt/nightcool/.venv
sudo -u nightcool /opt/nightcool/.venv/bin/pip install -e /opt/nightcool

sudo cp deploy/systemd/nightcool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nightcool

# Watch logs.
journalctl -u nightcool -f
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The core gate logic lives in `src/nightcool/engine.py` as a pure function.
Test it against your house by feeding fake forecasts through
`MockWeatherProvider` and walking around with a thermometer for a week
before trusting it unattended.

## What this app does NOT do in v1

These are good ideas, but they belong in v2 or later:

- Dew point / condensation calculation
- Per-window cross-ventilation scoring by orientation degrees
- Suggested opening width in inches
- Thermal-mass modeling
- Energy savings estimates in kWh / dollars / CO₂
- Smart thermostat OAuth integration
- Motorized window control
- Air quality (AQI / wildfire smoke)

Resist adding them until you've lived with v1 for a couple of weeks.
