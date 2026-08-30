#!/usr/bin/env python3
"""Merge every Garmin export in garmin_data/json/ into one accumulating history,
then condense it into garmin_data/dashboard_data.json for the dashboard.

Why it reads the whole json/ folder rather than just latest.json:
garmin_export.py --since-last writes ONLY the days it just fetched. latest.json is
therefore the most recent slice (often 2-3 days), not the running history -- it is
replaced on every run, not appended to. Each run also drops a dated archive file in
garmin_data/json/, and those together are the real history. Merging them here means
the record grows every day instead of collapsing to the last few days, and it keeps
growing past any single export window.

Overlapping dates are expected (the exporter re-syncs the last 2 days because Garmin
backfills sleep and HRV hours after the fact). Newer files win on conflict.
"""
import json
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
JSON_DIR = HERE / "garmin_data" / "json"
LATEST = HERE / "garmin_data" / "latest.json"
OUT = HERE / "garmin_data" / "dashboard_data.json"


def load_sources():
    """All archive files plus latest.json, oldest-generated first."""
    paths = sorted(JSON_DIR.glob("garmin_*.json")) if JSON_DIR.is_dir() else []
    if LATEST.exists():
        paths.append(LATEST)
    loaded = []
    seen_stamps = set()
    for p in paths:
        try:
            with open(p) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ! skipping {p.name}: {e}")
            continue
        stamp = (d.get("meta") or {}).get("generated_at") or ""
        # latest.json is a copy of the newest archive file -- don't count it twice
        if stamp and stamp in seen_stamps:
            continue
        seen_stamps.add(stamp)
        d.pop("raw", None)          # bulky and unused downstream; free it early
        loaded.append((stamp, p.name, d))
    loaded.sort(key=lambda t: t[0])  # oldest generated_at first, so newest wins merges
    return loaded


def iso_week(datestr):
    y, m, d = (int(x) for x in datestr.split("-")[:3])
    iy, iw, _ = date(y, m, d).isocalendar()
    return f"{iy}-W{iw:02d}"


def mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


sources = load_sources()
if not sources:
    raise SystemExit(f"No Garmin exports found in {JSON_DIR} or {LATEST}")

print(f"Merging {len(sources)} export file(s):")
days_by_date, acts_by_id, athlete = {}, {}, None
for stamp, name, d in sources:
    dd, aa = d.get("days") or [], d.get("activities") or []
    print(f"  {name}: {len(dd)} days, {len(aa)} activities")
    for row in dd:
        if row.get("date"):
            days_by_date[row["date"]] = row
    for a in aa:
        key = a.get("activity_id") or f"{a.get('start_local')}|{a.get('name')}"
        acts_by_id[key] = a
    if d.get("athlete"):
        athlete = d["athlete"]          # newest file wins (list is oldest-first)

days_merged = [days_by_date[k] for k in sorted(days_by_date)]
acts_merged = sorted(
    acts_by_id.values(), key=lambda a: (a.get("date") or "", a.get("start_local") or "")
)
print(f"  -> merged history: {len(days_merged)} days, {len(acts_merged)} activities")

# ---------------------------------------------------------------- athlete
prs_wanted = {
    "5 km (time)", "10 km (time)", "Half marathon (time)", "Marathon (time)",
    "Longest run (distance)", "Longest ride (distance)", "1 km (time)", "1 mile (time)",
    "Biggest ascent",
}
athlete = athlete or {}
prs = [p for p in (athlete.get("personal_records") or []) if p.get("label") in prs_wanted]
athlete_out = {
    "vo2max_running": athlete.get("vo2max_running"),
    "vo2max_cycling": athlete.get("vo2max_cycling"),
    "chronological_age": athlete.get("chronological_age"),
    "fitness_age": athlete.get("fitness_age"),
    "fitness_age_achievable": athlete.get("fitness_age_achievable"),
    "training_status_feedback": athlete.get("training_status_feedback"),
    "endurance_score": athlete.get("endurance_score"),
    "hill_score": athlete.get("hill_score"),
    "race_predictions_s": athlete.get("race_predictions_s"),
    "heart_rate_zones": athlete.get("heart_rate_zones"),
    "personal_records": prs,
    "devices": athlete.get("devices"),
}

# ---------------------------------------------------------------- days
day_fields = [
    "date", "weekday", "steps", "resting_hr", "vo2max_running", "vo2max_cycling",
    "stress_avg", "body_battery_high", "body_battery_low",
]
days_out = []
for d in days_merged:
    row = {k: d.get(k) for k in day_fields}
    hrv = d.get("hrv") or {}
    readiness = d.get("readiness") or {}
    sleep = d.get("sleep") or {}
    # last_night_avg_ms is the true single-night value. weekly_avg_ms is Garmin's OWN
    # 7-day average -- using it as the "daily" series would mean charting a smoothed
    # series and then smoothing it again, which flattens real day-to-day variation.
    row["hrv_ms"] = hrv.get("last_night_avg_ms") or hrv.get("weekly_avg_ms")
    row["hrv_weekly_avg_ms"] = hrv.get("weekly_avg_ms")
    row["hrv_baseline_low"] = hrv.get("baseline_low")
    row["hrv_baseline_high"] = hrv.get("baseline_high")
    row["hrv_status"] = hrv.get("status")
    row["readiness_score"] = readiness.get("score")
    row["readiness_level"] = readiness.get("level")
    row["sleep_score"] = sleep.get("score")
    row["sleep_duration_s"] = sleep.get("duration_s")
    days_out.append(row)

# ---------------------------------------------------------------- activities
act_fields = [
    "activity_id", "date", "start_local", "name", "type", "duration_s", "distance_km",
    "pace", "avg_hr", "max_hr", "calories", "elevation_gain_m",
    "training_effect_aerobic", "training_effect_anaerobic", "training_effect_label",
    "training_load", "avg_power_w", "norm_power_w", "avg_cadence_spm", "location",
    "moderate_intensity_min", "vigorous_intensity_min", "hr_zones_min",
]
activities_out = [{k: a.get(k) for k in act_fields} for a in acts_merged]

# ---------------------------------------------------------------- weeks (recomputed)
# Can't merge the exporters' own week rollups: a week split across two export files
# would appear twice, each covering only part of it. Recompute from merged data.
weeks_map = {}
for d in days_out:
    weeks_map.setdefault(iso_week(d["date"]), {"days": [], "acts": []})["days"].append(d)
for a in activities_out:
    if a.get("date"):
        weeks_map.setdefault(iso_week(a["date"]), {"days": [], "acts": []})["acts"].append(a)

weeks_out = []
for wk in sorted(weeks_map):
    dd, aa = weeks_map[wk]["days"], weeks_map[wk]["acts"]
    weeks_out.append({
        "week": wk,
        "sessions": len(aa),
        "distance_km": round(sum(a.get("distance_km") or 0 for a in aa), 1),
        "duration_s": int(sum(a.get("duration_s") or 0 for a in aa)),
        "training_load": int(sum(a.get("training_load") or 0 for a in aa)),
        "avg_resting_hr": mean([d.get("resting_hr") for d in dd]),
        "avg_sleep_s": mean([d.get("sleep_duration_s") for d in dd]),
        "avg_sleep_score": mean([d.get("sleep_score") for d in dd]),
        "avg_hrv_ms": mean([d.get("hrv_ms") for d in dd]),
        "avg_readiness": mean([d.get("readiness_score") for d in dd]),
        "avg_steps": mean([d.get("steps") for d in dd]),
    })

# ---------------------------------------------------------------- sports (recomputed)
sports_map = {}
for a in activities_out:
    s = sports_map.setdefault(a.get("type") or "other", {
        "sport": a.get("type") or "other", "sessions": 0,
        "distance_km": 0.0, "duration_s": 0, "training_load": 0, "calories": 0,
    })
    s["sessions"] += 1
    s["distance_km"] += a.get("distance_km") or 0
    s["duration_s"] += a.get("duration_s") or 0
    s["training_load"] += a.get("training_load") or 0
    s["calories"] += a.get("calories") or 0
sports_out = sorted(sports_map.values(), key=lambda s: -s["training_load"])
for s in sports_out:
    s["distance_km"] = round(s["distance_km"], 1)
    s["duration_s"] = int(s["duration_s"])
    s["training_load"] = int(s["training_load"])
    s["calories"] = int(s["calories"])

# ---------------------------------------------------------------- meta
newest_meta = sources[-1][2].get("meta") or {}
meta_out = {
    "schema_version": newest_meta.get("schema_version"),
    "generated_at": newest_meta.get("generated_at"),
    "source": newest_meta.get("source"),
    "start_date": days_out[0]["date"] if days_out else None,
    "end_date": days_out[-1]["date"] if days_out else None,
    "day_count": len(days_out),
    "profile": newest_meta.get("profile"),
    "export_files_merged": len(sources),
}

out = {
    "meta": meta_out,
    "athlete": athlete_out,
    "weeks": weeks_out,
    "sports": sports_out,
    "days": days_out,
    "activities": activities_out,
    "problems": sources[-1][2].get("problems", []),
}

OUT.write_text(json.dumps(out, default=str))
print(
    f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB) — "
    f"{meta_out['day_count']} days, {meta_out['start_date']} to {meta_out['end_date']}"
)
