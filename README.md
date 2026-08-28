# Garmin → AI coach exporter

Downloads your Garmin Connect data on any laptop and writes it out as a single **JSON** file:
a flat, unit-consistent digest of your days, weeks, activities and per-sport volume, plus every
untouched Garmin payload underneath it.

Built on [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
which is the most maintained of the community Garmin projects. It talks to Garmin's private
Connect API using the same mobile SSO flow as the official Android app — no browser, no
scraping, and MFA is supported.

> There is no public/official Garmin Connect REST API for personal data. Garmin's official
> developer programs (Health API, Activity API) are B2B and require a company agreement.
> Every practical personal-data tool, this one included, uses the app's own endpoints.

---

## Requirements

- **Python 3.12 or newer** (the `garminconnect` library requires it)
- A Garmin Connect account

macOS ships an older Python — install a current one with `brew install python@3.14`.

## Install

```bash
cd garmin-coach-export && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

If your default `python3` is older than 3.12, point the venv at a newer one:

```bash
/opt/homebrew/bin/python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## First run

```bash
.venv/bin/python garmin_export.py --days 30
```

It asks for your Garmin email, password, and MFA code if your account has 2FA. Those go
straight to Garmin and are never written to disk. What *is* saved is the OAuth token bundle
in `~/.garminconnect` — it carries a refresh token, so **every later run is silent**. That
is what makes scheduling work.

## Regular use

```bash
.venv/bin/python garmin_export.py --since-last --profile full
```

`--since-last` picks up where the previous run stopped, re-pulling the last 2 days because
Garmin backfills sleep and HRV scores hours after the night itself.

### Schedule it (macOS, launchd)

Save as `~/Library/LaunchAgents/com.garmin.export.plist`, editing the path:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.garmin.export</string>
  <key>ProgramArguments</key>
  <array><string>/FULL/PATH/TO/garmin-coach-export/run_export.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.garmin.export.plist
```

### Schedule it (Linux, cron)

```bash
30 7 * * * /full/path/to/garmin-coach-export/run_export.sh
```

Both use `run_export.sh`, which runs incrementally and appends to `garmin_data/export.log`.

---

## What it downloads

**Every day in the window**

| Group | Metrics |
| --- | --- |
| Sleep | duration, score and qualifier, deep/light/REM/awake, bed and wake time, overnight HRV, restless moments, sleep respiration, SpO2, body-battery change |
| HRV | last-night average, 5-min high, 7-day average, status, personal baseline range |
| Readiness | score, level, recovery time, sleep-score factor, HRV factor, acute load |
| Heart | resting HR, min/max HR |
| Stress & energy | average and max stress, body battery high/low/charged/drained |
| Volume | steps and goal, distance, calories (total/active/BMR), floors, intensity minutes |
| Fitness | VO2 max (run and bike), fitness age, training status, weekly load, load balance |
| Body | weight, body fat |
| `--profile full` adds | SpO2, respiration, hydration, floors, per-day training status and VO2 max |

**Every activity**: type, name, distance, duration, moving time, pace or speed, avg/max HR,
calories, elevation, aerobic and anaerobic training effect, training load, power (avg / normalised
/ max), cadence, stride length, ground contact, vertical oscillation, HR time-in-zone, location.

**Once per export**: profile, devices, HR zones, personal records, Garmin's race predictions,
endurance score, hill score, gear, active goals.

## Output

```
garmin_data/
├── latest.json            # newest export, stable path — point your coach here
├── json/garmin_2026-07-30_to_2026-08-28.json
├── .sync_state.json       # where --since-last resumes from
└── export.log             # scheduled-run log
```

Each file has five top-level keys:

| Key | |
| --- | --- |
| `meta` | window, profile, generation time, API call count |
| `athlete` | profile, devices, HR zones, personal records (labelled), race predictions, VO2 max, training status, endurance and hill score, gear |
| `days` | one flat record per calendar day — sleep, HRV, readiness, heart, stress, body battery, volume, fitness markers |
| `weeks` / `sports` | ISO-week rollup, and volume split per sport |
| `activities` | one record per session |
| `problems` | endpoints that returned nothing, and why |
| `raw` | every untouched Garmin payload, so nothing is lost if you later want a field the digest skips |

Use `--no-raw` for a much smaller file — `raw` is the bulk of it (a 30-day export is ~11 MB
with it, ~200 KB without).

Values are `null` where Garmin had no data. Treat those as *unknown*, never as zero — a missing
HRV night is not an HRV of 0.

## Feeding your AI coach

Read `latest.json` and use the `days`, `weeks`, `activities` and `sports` arrays. They are
already flat, unit-consistent and null-where-missing, so they go into a prompt or a dataframe
without further shaping. `athlete` gives the standing context. `raw` is there when you need a
field the digest doesn't surface.

```bash
.venv/bin/python garmin_export.py --since-last --no-raw
```

## Options

| Flag | Purpose |
| --- | --- |
| `--days N` | last N days (default 30) |
| `--start` / `--end` | explicit `YYYY-MM-DD` window |
| `--since-last` | resume from the previous run (with `--overlap N` days of re-pull) |
| `--profile core\|full` | `core` is the coaching essentials; `full` adds the long tail |
| `--activity-details` | splits, weather and HR zones per activity (slow) |
| `--intraday` | minute-level HR and step arrays (large) |
| `--download tcx\|gpx\|original\|csv\|kml` | also save each activity's file |
| `--no-raw` | drop raw payloads from the JSON |
| `--no-latest` | skip writing `latest.json` |
| `--out DIR` | output directory (default `./garmin_data`) |
| `--login` / `--logout` | force a fresh sign-in / delete saved tokens |
| `--pause SECONDS` | delay between API calls (default 0.4) |
| `-v` | log every endpoint call |

## Credentials and safety

- Your password is sent only to Garmin, is dropped from memory right after login, and is
  never written to disk.
- Tokens live in `~/.garminconnect`. Anyone with that directory can read your Garmin data —
  `--logout` deletes them.
- For unattended machines you can set `GARMIN_EMAIL` / `GARMIN_PASSWORD` to skip the prompts,
  but the token store already makes that unnecessary after the first run.
- `garmin_data/` and `.venv/` are gitignored: the exports contain your health data.

## Troubleshooting

**`Missing dependency 'garminconnect'` or a Python-version complaint** — the venv is on
Python < 3.12. Rebuild it against 3.12+ (see Install).

**Rate limited (HTTP 429)** — Garmin throttles per account. Wait ~15 minutes, then use a
smaller window and a larger `--pause`. Large first backfills are best split into chunks:

```bash
.venv/bin/python garmin_export.py --start 2026-01-01 --end 2026-03-31 --pause 1.0
```

**Entries under `problems`** — normal. Many metrics depend on your watch model, whether the
feature is enabled, and whether the watch synced. The export continues regardless; one dead
endpoint never costs you the rest of the run.

**A field is null across every day** — check `raw` before assuming Garmin has no data. Some
metrics are only computed on qualifying days (VO2 max needs an outdoor run), and some are only
fetched under `--profile full`.

**Login stops working** — Garmin occasionally invalidates tokens. Run with `--login`.

**Empty or partial days** — Garmin backfills sleep, HRV and readiness for several hours after
waking. Run in the late morning, and keep the `--since-last` overlap.
