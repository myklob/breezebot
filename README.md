# NightCool

A small Python service that tells you when (and which) windows to open for free
overnight cooling, then nudges you to close them before the morning sun cooks
the gains away. It polls the National Weather Service every 15 minutes,
compares the forecast to your indoor temperature, and sends a push
notification when there's a "free cooling" opportunity that won't blow rain
in, fly papers around, or roll the smell of the landfill through your
bedroom.

Runs on a Raspberry Pi, an old laptop, or a $5 VPS. Ships with a Progressive
Web App so you can install it on your phone like a native app — no App Store
review, no Apple developer fee, one codebase.

## Quickstart

```bash
git clone <this-repo>
cd breezebot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Edit config.yaml for your house (windows, location, profile).
$EDITOR config.yaml

# Tell it your current indoor temp (one-time; daemon will keep using it).
nightcool set-indoor 71

# Sanity check.
nightcool check
nightcool forecast

# Send a test notification.
nightcool test-notify

# Run the polling loop (decisions + notifications).
nightcool daemon

# In another terminal, serve the PWA + HTTP API.
nightcool serve
# → open http://127.0.0.1:8765
```

## Schedule profiles

Pick the preset that matches your life under `user_prefs.profile`:

| Profile          | Best for                | Notable behavior                                                  |
|------------------|-------------------------|-------------------------------------------------------------------|
| `commuter`       | Weekday office worker   | Skips weekday morning CLOSE — you close on the way out.           |
| `wfh`            | Work-from-home/retiree  | Pings for midday cooling windows too.                             |
| `night_shift`    | Sleeps during the day   | Flip `quiet_hours_*` to your sleep window.                        |
| `light_sleeper`  | No pings overnight      | Defers quiet-hours OPENs to a morning summary.                    |
| `aggressive`     | Maximalist              | Hysteresis 1 °F; opens for any small edge.                        |
| `conservative`   | Set-and-forget          | Hysteresis 5 °F; requires 3+ consecutive cool hours.              |
| `custom`         | Roll your own           | Behavior follows explicit `profile_overrides` in config.          |

You can also flip individual behavior flags under
`user_prefs.profile_overrides` — for example, a commuter who *does* want a
weekday CLOSE alert:

```yaml
user_prefs:
  profile: commuter
  profile_overrides:
    weekday_morning_close_notify: true
```

Switch profiles at any time:

```bash
nightcool set-profile wfh
```

…or pick a different one in the web UI's Profile card; the server rewrites
`config.yaml` for you.

## The PWA

`nightcool serve` hosts both the JSON API and a small Progressive Web App. On
phone or desktop, browse to the URL, then add to home screen. You'll get:

- The current OPEN/CLOSE recommendation
- A 6-hour forecast preview
- A one-tap "set indoor temperature" input
- A profile picker
- A "Enable push notifications" button (web push via VAPID — works on iOS
  16.4+ and Android/Chrome/Firefox)
- A savings tile (populates once the thermal model has enough data)

### Setting up web push

```bash
nightcool web-push-keys
# Paste the printed YAML block into config.yaml under notifications:
#   service: web_push
#   web_push:
#     vapid_public_key: "…"
#     vapid_private_key: |
#       -----BEGIN PRIVATE KEY-----
#       …
```

Then `nightcool serve`, open the PWA, hit **Enable push notifications**. The
daemon's notifications will arrive on every subscribed device.

## Indoor temperature sources

Set `indoor_temp.source` in `config.yaml`:

- `manual` — typed in via `nightcool set-indoor` or the PWA.
- `sensor_file` — a separate process writes a single float to
  `sensor_file_path`. Pairs well with a tiny BLE reader script.
- `nest` — Google Smart Device Management API. Needs a Device Access
  project ($5 one-time), an OAuth client, and a long-lived refresh
  token. See `src/nightcool/sources.py` for the trait we read.
- `ble` — a Bluetooth thermometer (Govee, SwitchBot, ThermoPro, Inkbird).
  We don't embed a Bluetooth stack; point `ble.cache_file` at a path
  your reader keeps fresh.

If the configured source fails (file missing, OAuth expired), NightCool
falls back to `manual_default_f` and logs a warning rather than crashing
the daemon.

## Thermal model + savings

Every poll cycle, NightCool logs `(timestamp, indoor, outdoor, action,
source)` to `data_log.sqlite`. After roughly a day of samples,
`GET /api/savings` returns a fitted α/β/γ coefficient set:

```
dT_indoor/dt = α · ventilation·(T_out − T_in)
             + β · (solar_load − T_in)
             + γ · HVAC_active
```

…plus a rough kWh + dollar savings estimate. The first few days the model
will be noisy; live with it for a couple weeks before trusting the
numbers.

## How to find your lat/lon

Open [Google Maps](https://maps.google.com), right-click your house, and copy
the lat/lon that appears at the top of the menu. NWS only covers the US — for
other countries, you'll need a different provider (v2).

Sanity check the NWS gridpoint exists:

```bash
curl -H "User-Agent: nightcool/0.1" "https://api.weather.gov/points/39.7392,-104.9903"
```

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
  save the cool air (unless your profile says otherwise).

## Decision logic in one paragraph

Every 15 minutes, scan the next 12 hours of NWS hourly forecast. The
"open" moment is the first hour where outdoor temperature is at least
`hysteresis_f` below indoor temperature, at or above
`min_tolerable_outdoor_f`, no more than `sleep_target_f + 5` (so opening
is actually worth it), and wind isn't blowing from the bad sector — and
the run of such hours is at least `profile.sustained_hours_required`
long. If such a moment exists within the next two hours and at least one
window passes its eligibility check (rain, gusts, sector), send one OPEN
notification listing the eligible windows and a close-by time (the first
later hour where outdoor warms back above the threshold, or
`morning_close_time`, whichever is earlier). When outdoor crosses back
above the threshold, send one CLOSE notification (commuters skip the
weekday morning ping). Don't repeat the same notification twice. Quiet
hours suppress OPEN but not CLOSE; light sleepers see a morning summary
instead.

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
- Motorized window control
- Air quality (AQI / wildfire smoke)
- True multi-zone thermal model (per-room coefficients)
- Window-confirmation telemetry ("did you actually open them?")
- Polished icons (the bundled PNGs are placeholders)
