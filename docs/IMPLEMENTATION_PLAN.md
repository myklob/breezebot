# BreezeBot Implementation Plan

This document is the working brief for the next phase of BreezeBot development. It captures architectural decisions, sequences the work into phases, and notes what must NOT change. Hand this file (or sections of it) to Claude Code or any contributor as their working brief.

## 0. Context: where we are today

BreezeBot is a Python (~89%) night-cooling decision engine with an optional PWA frontend. It:

- Pulls forecasts from the free NWS API using US Census geocoding (no API key)
- Decides per-window whether to open or close based on outdoor temp, indoor temp, and the user's schedule
- Optionally warns on rain, wind gusts, and bad wind direction
- Ships notifications via web push, ntfy.sh, Pushover, or console
- Runs as a local daemon (Pi, VPS, or laptop) with a localhost web UI
- Has a savings dashboard with a small thermal model (alpha/beta/gamma parameters)
- Stores everything locally. **"No cloud account" is an explicit design principle.**

The engine is documented as a pure function in `src/nightcool/engine.py`, which makes it portable.

## 1. Where we are going

A **hybrid** product:

- **Hosted website** at, e.g., breezebot.app, with real user accounts. Decisions run on a serverless backend. The user opens the PWA, gets notifications, never installs anything.
- **Self-host path** stays alive. Privacy-conscious users keep running the Python daemon locally. The frontend code is the same; only the backend URL differs.
- **Three login methods** for the hosted version: Google OAuth, Apple OAuth, and magic-link email.
- **Two big content features** the current repo admits are missing: dew point gating and air quality (AQI) gating.

## 2. Architectural decisions

| Decision | Choice | Rationale |
|---|---|---|
| Hosted backend stack | Cloudflare Workers + KV + D1 | Cheapest at low scale, cron triggers built in, no servers to manage. Alternative: AWS Lambda + DynamoDB if you prefer Python end-to-end. |
| Engine language | Keep Python locally; either port to TypeScript for Workers OR run Python on AWS Lambda | The engine is small enough to port. Decide based on which cloud you want to operate. |
| Frontend | Static SPA (vanilla JS or Astro), points at either backend URL or localhost | Same code, two deployments. |
| Auth library | Lucia Auth (TS) or Authlib (Python) depending on backend stack | Both support Google/Apple OIDC and magic links. |
| Magic link delivery | Resend | Simple API, generous free tier, no SMTP misery. |
| AQI providers | AirNow (primary) + PurpleAir (fallback) | AirNow is the EPA's official feed. PurpleAir fills coverage gaps. |
| Geocoding | US Census (kept) + Mapbox or Google Places for address autocomplete in the UI | Census stays for the actual lookup; autocomplete is a UX-only addition. |

## 3. What must NOT change

1. **The self-host path stays first-class.** If you can't run the Python daemon on a Pi after this work, we did it wrong.
2. **NWS API stays free and key-free.** Don't replace it with a paid provider unless there's a real failure case.
3. **The existing thermal model in the savings dashboard.** It's good. Leave it.
4. **The "no cloud account" option.** Self-hosters never need an account.

## 4. Phased work plan

### Phase 1: Quick wins inside the existing repo (1 to 2 weekends)

These add real user value without committing to the cloud rewrite.

#### 1.1 Dew point gate

- The NWS gridpoints response already includes `dewpoint`. We're not using it.
- Add config field: `gating.max_dew_point_f` (default: disabled).
- In the engine, after the outdoor-temp check, reject open if `outdoor_dew_point > max_dew_point_f`.
- Add tests: muggy summer night (low temp, high dew point) should refuse to open even though raw temp is favorable.
- New copy in the UI: "Humid air" toggle on the warnings screen with a one-line explanation of why a 70°F night at 70°F dew point is miserable.

#### 1.2 AQI provider

- New module: `src/nightcool/providers/aqi.py`.
- Implement two backends behind an interface: AirNow (requires free API key) and PurpleAir (free, no key for read-only).
- Config: `gating.max_aqi` (default disabled), `gating.aqi_provider`.
- In the engine, reject open if AQI exceeds threshold.
- Cache responses for 30 minutes (AQI doesn't change that fast).
- Tests: mock response with AQI 150 should block open.

#### 1.3 Comfort presets

- New file: `src/nightcool/presets.py` (or `presets.yaml`).
- Four presets:
  - `comfort_first`: ASHRAE 55 standard, target band 70 to 74°F
  - `energy_saver`: DOE recommended, 68°F winter target / 78°F summer cap
  - `cool_sleeper`: 65°F overnight, 70°F daytime
  - `custom`: no preset applied; user defines every value
- CLI: `nightcool init --preset cool_sleeper` writes a starter config.
- UI: comfort picker on onboarding, matching the mockup at `/docs/onboarding-mockup.html`.

#### 1.4 PWA redesign

Apply the design committed in the onboarding mockup:

- **Palette**: deep navy backgrounds, warm dawn (`#f4c47a`) accent, cool grays for secondary text. Variables in `web/static/css/theme.css`.
- **Type**: Fraunces (display, serif) + Geist (body, sans) + Geist Mono (data labels). Self-host or use Google Fonts.
- **Status pill** as the primary dashboard element. "Open the windows" stated as a verb, with the reasoning chip directly below.
- **Onboarding** rebuilt as a 6-step flow: welcome/sign-in, address, comfort preset, weekly schedule, notification channels, optional warnings.
- **Warnings page** surfaces the new dew point and AQI toggles alongside rain/gusts/wind sector. Default off, with one-line plain-language explanations.

Acceptance: a brand-new user can land on the PWA, complete onboarding in under two minutes, and see a meaningful dashboard.

### Phase 2: Engine as pure, portable module

Goal: the same engine code can run inside the Python daemon OR be ported to TypeScript and run in Cloudflare Workers, with identical behavior.

- Audit `src/nightcool/engine.py`. Remove any direct I/O (HTTP calls, file reads, time.now()). Inject everything.
- Engine signature becomes roughly: `decide(now, schedule, indoor, outdoor, gating) -> Decision`.
- All weather providers, indoor temp sources, and notifiers behind clean interfaces under `providers/` and `notifiers/`.
- Property-based tests using `hypothesis`: for any reasonable input combination, decision is one of {OPEN, CLOSE, WAIT_UNTIL}.
- Acceptance: 100% line coverage on engine.py, and the function has zero imports from `requests`, `aiohttp`, `time`, or anything I/O-related.

### Phase 3: Serverless backend (hosted product)

- Set up a `workers/` directory at repo root.
- `workers/api/` is a Cloudflare Worker exposing REST endpoints:
  - `POST /auth/google`, `POST /auth/apple`, `POST /auth/magic-link/send`, `POST /auth/magic-link/verify`
  - `GET /preferences`, `PUT /preferences`
  - `GET /status` (returns current decision for the user)
  - `GET /history?days=30`
- `workers/cron/` is a Worker with a Cron Trigger that runs every 15 minutes:
  - Lists active users from KV
  - For each, fetches weather + AQI, runs engine, compares to last decision
  - On state change, dispatches push notification via Web Push
- Storage:
  - **D1 (SQLite)** for users, sessions, decision history
  - **KV** for hot cache of weather data, keyed by `geohash:15min`
- Engine port: rewrite `engine.py` as `workers/engine/engine.ts`, mirroring the Python tests one-for-one. Both implementations must produce identical decisions on the test corpus.

Acceptance: deploy to Cloudflare, run for a week against your own house, compare to the local daemon's decisions, log any divergences.

### Phase 4: Authentication

- Library: Lucia Auth (if TypeScript backend) or Authlib (Python).
- Google: standard OIDC, requires creating a project at console.cloud.google.com and registering OAuth client.
- Apple: requires Apple Developer account, more setup overhead. Add this last.
- Magic link: Resend API, 24-hour single-use tokens stored in D1.
- Sessions: HTTP-only secure cookies, 30-day expiry, sliding renewal.
- Account linking: if a Google-authed user later signs in with the same email via Apple, prompt to link instead of creating a duplicate.

### Phase 5: Production frontend

- Convert the mockup at `docs/onboarding-mockup.html` into a real SPA.
- Recommendation: Astro (static-first, minimal JS) or vanilla JS with web components. Avoid heavy frameworks for a UI this size.
- Environment variable `BREEZEBOT_API_URL` controls which backend it talks to. Default: hosted URL. Self-hosters override to `http://localhost:8765`.
- Deploy to Cloudflare Pages or Vercel.
- PWA manifest + service worker for "Add to Home Screen" on iOS and Android.

### Phase 6: Self-host installer polish

- Finish `packaging/nightcool.spec` (PyInstaller) for Windows.
- Add `briefcase` or `py2app` for macOS `.app` bundle.
- AppImage and `.deb` for Linux.
- Single `install.sh` for Linux/Mac CLI users.
- GitHub Actions release pipeline: on tag push, build all four artifacts and attach to the release.

## 5. Open questions for the repo owner

Decide these before Phase 3 starts:

1. **Will you operate the hosted version yourself, or stay self-host-only?** If self-host only, skip phases 3-5 except the frontend rebuild.
2. **Backend language**: TypeScript on Cloudflare Workers, or Python on AWS Lambda? Workers is cheaper and faster but requires a port; Lambda lets you ship the existing engine almost as-is.
3. **AQI provider priority**: AirNow first (official EPA, slower updates) or PurpleAir first (community sensors, faster but noisier)?
4. **Domain and hosting account**: who owns breezebot.app (or whatever domain), who pays the $5/month?

## 6. How to run this plan with Claude Code

```bash
git clone https://github.com/myklob/breezebot.git
cd breezebot
claude
```

Then paste:

> Read README.md, scan the repo structure, then read docs/IMPLEMENTATION_PLAN.md. Confirm you understand the current state and the plan. Then start with Phase 1.1 (dew point gate). Show me your plan before editing any files, then implement it with tests. Do not move on to 1.2 until 1.1 is merged.

Work one sub-phase at a time. Don't let it batch unrelated changes.
