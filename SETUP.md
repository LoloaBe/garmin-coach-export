# Automated dashboard refresh — setup

What this adds to the repo: `dashboard_template.html` (the dashboard's HTML/CSS/JS,
same file Claude edits when your plan changes), `render_site.py` (turns the template +
`garmin_data/dashboard_data.json` into a real standalone page at `site/index.html`),
`refresh_and_publish.sh` (chains export → condense → render → git push), and this file's
sibling `com.lucacriscuolo.garmincoach.plist` (an optional macOS scheduled-job definition).

**Why this exists:** Claude's own sandboxes (both the device bridge and the cloud
workspace) cannot reach Garmin's servers at all — confirmed by testing directly, it's a
network policy, not a credentials problem. So the Garmin pull can only ever run
somewhere with your Mac's own open internet access: your Terminal, run by you (by hand,
or on a schedule via the launchd job below). Claude never needs your Garmin password and
never touches this step.

## 1. One-off: run it by hand

```
cd ~/Documents/GitHub/garmin-coach-export
bash refresh_and_publish.sh
```

This refreshes `garmin_data/latest.json` and `dashboard_data.json`, writes
`site/index.html`, and — if git has something new to commit — commits and pushes it.
Safe to re-run any time; it's incremental (`--since-last`) and does nothing if there's
no new data.

## 2. Optional: make it automatic (no manual step, ever)

```
cp com.lucacriscuolo.garmincoach.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lucacriscuolo.garmincoach.plist
```

Runs daily at 06:15 (edit the `Hour`/`Minute` in the plist first if you want a different
time, then re-run the two commands above — `launchctl bootout gui/$(id -u) ...` first if
it's already loaded). Only runs while your Mac is on; if it's asleep at 06:15, launchd
runs it as soon as it wakes. Logs to `/tmp/garmincoach-launchd.log` and
`garmin_data/refresh.log`.

To remove it later: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lucacriscuolo.garmincoach.plist && rm ~/Library/LaunchAgents/com.lucacriscuolo.garmincoach.plist`

## 3. Hosting `site/index.html` at lucacriscuolo.com

Not done yet — needs an account-level decision and a couple of clicks only you can do
(sign-up, DNS). Recommended: Cloudflare Pages (free), connected to this GitHub repo,
build output directory `site`, no build command (it's already a finished static file).
Every `git push` — from you, or from the scheduled job above — triggers an automatic
redeploy. Point a subdomain (e.g. `coach.lucacriscuolo.com`) at it via a CNAME record in
your IONOS DNS panel. Password-protect it with Cloudflare Access (free tier) once it's
live. Ask Claude when you're ready to do this part together.

## What still needs Claude

Only the *plan itself* — race decisions, equipment calls, weekly structure. When you
tell Claude something that changes the plan, it edits `dashboard_template.html` (both
here and in the project's `claude/dashboard-template.html` doc) and republishes the
Claude Artifact immediately. That change reaches the website too the next time this
script runs (by hand or via the schedule) and pushes.
