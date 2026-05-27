# NightCool

When the outdoor air is cooler than your house, free cooling is sitting
right outside. NightCool watches the local weather forecast and tells you
when to open the windows and when to close them so the house stays at the
temperature you want — without running the AC.

Works as a phone app (installable Progressive Web App), a Windows desktop
app, or a small service on a home server. Pick whichever path is easier.

## What it does

1. Looks up the forecast for your address every 15 minutes.
2. Compares the outdoor temperature to the indoor temperature and to your
   target for the day.
3. If opening windows would let the house reach (but not overshoot) your
   target, sends you an OPEN notification.
4. When outside warms back up, sends a CLOSE notification — except on
   weekday mornings, when it assumes you'll close on the way out the door.
5. Optionally warns you about rain, wind gusts, or wind from a specific
   direction if you turn those alerts on.

That's the whole product.

## Quickstart — Windows / Mac / Linux desktop

Easiest path for a non-technical user. Coming in the next release: a
single-file installer built from `packaging/nightcool.spec` (PyInstaller).
Double-click `nightcool.exe`, it opens the PWA in your browser, runs in
the background. Until that's published, use one of the paths below.

## Quickstart — Python (any OS)

```bash
git clone <this-repo>
cd breezebot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Edit config.yaml — at minimum the address and the windows list.
$EDITOR config.yaml

# Tell it your current indoor temperature.
nightcool set-indoor 72

# Sanity check.
nightcool check

# Start the polling loop (decisions + notifications):
nightcool daemon

# In another terminal, start the web UI:
nightcool serve --open-browser
# → http://127.0.0.1:8765
```

## Setting your daily schedule

Per day, give NightCool two things:

- `target_f` — the temperature you'd like the house to be at.
- Either a `leave_at` time (your typical departure) or `home_all_day: true`.

If a CLOSE recommendation falls *before* your `leave_at` on a weekday,
NightCool stays quiet on the assumption that you'll close on the way out.
On `home_all_day` days, every CLOSE pings.

You can edit the schedule three ways:

1. Open the web UI's **Weekly schedule** card and change values inline.
2. `nightcool set-target weekdays 68` / `nightcool set-leave-time fri 16:30`.
3. Edit `config.yaml` directly:

```yaml
schedule:
  mon: { target_f: 68, leave_at: "07:30" }
  tue: { target_f: 68, leave_at: "07:30" }
  wed: { target_f: 68, leave_at: "07:30" }
  thu: { target_f: 68, leave_at: "07:30" }
  fri: { target_f: 68, leave_at: "16:30" }   # leave early on Fridays
  sat: { target_f: 65, home_all_day: true }
  sun: { target_f: 65, home_all_day: true }
```

## Address-based setup

You don't need lat/lon. Put your street address in `config.yaml` and
NightCool will geocode it for you (US Census Geocoder — free, no API key,
US only):

```yaml
location:
  address: "1234 Main St, Denver CO 80218"
  timezone: "America/Denver"
```

Or, in the web UI, type your address into the **Address** card and click
Save. CLI equivalent:

```bash
nightcool geocode "1234 Main St, Denver CO" --config config.yaml
```

(Outside the US, NWS doesn't cover you — drop in a different weather
provider; that's a v2 task.)

## Comfort floor

NightCool predicts how cold the house will get if the windows stay open,
and uses that to shorten the close-by time. So if your floor is 60 °F and
opening would let the house drop to 55 °F, it'll close earlier.

```yaml
comfort_floor:
  min_indoor_f: 60.0
```

## Opt-in warnings

By default, NightCool only thinks about temperature. The rest of the
"what could go wrong with an open window" list is **off** unless you
enable it:

```yaml
warnings:
  warn_on_rain: false            # Drop exposed windows when rain likely.
  max_rain_chance_pct: 20.0
  warn_on_gusts: false           # Skip 'unsecure' windows during gusty wind.
  max_gust_mph: 18.0
  bad_wind_sector_deg: null      # Set [lo, hi] to block wind from a direction.
  max_dew_point_f: null          # Refuse open when outdoor dew point is too high.
  max_aqi: null                  # Refuse open when AQI is unhealthy.
```

`max_dew_point_f` is the muggy-air gate: a 65 °F night sounds great until
the dew point is 70 °F and the air walking in is saturated. Common
thresholds are 60 °F ("comfortable"), 65 °F ("noticeable"), and 70 °F
("oppressive"). Leave `null` to disable.

`max_aqi` is the air quality gate: keeps windows closed when outdoor air
is smoky, smoggy, or otherwise unhealthy. Set to 100 to allow opening only
when the air is Good or Moderate (AQI 0–100), or 50 to require Good air
only. Requires an AQI API key — see the AQI section below.

Use `bad_wind_sector_deg` for whatever bothers your house — neighbor's
smoking, a busy road, a landfill, an allergen source. Mark individual
windows with `on_bad_wind_sector: true` so only those windows get skipped.

## Air quality (AQI) gate

NightCool can refuse to open windows when outdoor air is unhealthy due to
wildfire smoke, smog, or high ozone. To enable:

1. Get a free API key from one of the providers below.
2. Add an `aqi` block to `config.yaml`:

```yaml
aqi:
  provider: "airnow"          # airnow | purpleair
  airnow_api_key: "YOUR_KEY"  # from https://www.airnowapi.org
  # purpleair_api_key: "KEY"  # alternative: https://develop.purpleair.com
```

3. Set a threshold in `warnings`:

```yaml
warnings:
  max_aqi: 100  # block when AQI > 100 (Unhealthy for Sensitive Groups)
```

**AirNow** is the EPA's official feed — data is updated hourly, covers the
US and parts of Canada/Mexico. Free, no billing required.

**PurpleAir** uses crowd-sourced sensors — faster updates (roughly every
2 minutes), denser coverage in some areas, but noisier data. Free API key
available at develop.purpleair.com.

NightCool caches AQI readings for 30 minutes so a 15-minute polling cycle
doesn't hammer the API. If the AQI fetch fails (network error, rate limit),
the gate passes through rather than refusing to open — better to let in
some cool air than to silently block forever on a transient error.

## Indoor temperature: where it comes from

Set `indoor_temp.source` in `config.yaml`:

- `manual` — entered via `nightcool set-indoor` or the web UI.
- `sensor_file` — a separate process writes a float (°F) to a file.
- `nest` — Google Smart Device Management API (Device Access project,
  OAuth, refresh token).
- `ble` — read the cache file maintained by a separate BLE reader.

If the configured source fails (file missing, OAuth expired), NightCool
falls back to the last manual value rather than crashing the daemon.

## Notifications

Pick one under `notifications.service`:

- `console` — print to stdout. Useful for testing.
- `web_push` — VAPID push to the installed PWA. Generate keys with
  `nightcool web-push-keys`, paste the YAML block into `config.yaml`,
  open the PWA, click **Enable push notifications**.
- `ntfy` — ntfy.sh, no signup required.
- `pushover` — needs both a user key and an app token.

## Savings dashboard

Every poll cycle is logged to `data_log.sqlite`. After a day or so of
data, the web UI's **Savings** card shows a fitted α/β/γ thermal model
and a rough kWh + dollar estimate of what overnight cooling has saved you
in AC runtime.

The first few days the model will be noisy. Live with it for a couple of
weeks before trusting the numbers.

## Distribution paths (what works on which device)

| Device                     | Path                                        |
|----------------------------|---------------------------------------------|
| iPhone / Android phone     | PWA (open the web URL, "Add to home screen") |
| Windows desktop            | `nightcool.exe` built from `packaging/nightcool.spec` |
| Mac laptop                 | Same spec; flip `console=False`, add a `BUNDLE` |
| Linux                      | Same spec, or `pip install` + systemd       |
| Raspberry Pi               | `pip install` + systemd (always-on home server) |
| $5 VPS                     | `pip install` + systemd                     |

The web UI is the same in every case — only the host process differs.

## What this app does NOT do (yet)

- Motorized window control
- Per-window cross-ventilation scoring
- Coverage outside the US (NWS only)
- Window-confirmation telemetry ("did you actually open them?")

## Development

```bash
pip install -e ".[dev]"
pytest
```

The core gate logic lives in `src/nightcool/engine.py` as a pure function
— easy to test against real and fake forecasts. Walk around with a
thermometer for a week before trusting the recommendations unattended.
