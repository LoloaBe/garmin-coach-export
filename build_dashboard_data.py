#!/usr/bin/env python3
"""Condense garmin_data/latest.json into garmin_data/dashboard_data.json —
a small summary file sized for embedding in Luca's Claude Artifact dashboard.
Run this after garmin_export.py / run_export.sh refreshes latest.json.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "garmin_data" / "latest.json"
OUT = HERE / "garmin_data" / "dashboard_data.json"

with open(SRC) as f:
    data = json.load(f)

athlete = data["athlete"]
prs_wanted = {
    "5 km (time)", "10 km (time)", "Half marathon (time)", "Marathon (time)",
    "Longest run (distance)", "Longest ride (distance)", "1 km (time)", "1 mile (time)",
    "Biggest ascent",
}
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

day_fields = [
    "date", "weekday", "steps", "resting_hr", "vo2max_running", "vo2max_cycling",
    "stress_avg", "body_battery_high", "body_battery_low",
]
days_out = []
for d in data["days"]:
    row = {k: d.get(k) for k in day_fields}
    hrv = d.get("hrv") or {}
    readiness = d.get("readiness") or {}
    sleep = d.get("sleep") or {}
    row["hrv_ms"] = hrv.get("weekly_avg_ms") or hrv.get("last_night_avg_ms")
    row["hrv_status"] = hrv.get("status")
    row["readiness_score"] = readiness.get("score")
    row["readiness_level"] = readiness.get("level")
    row["sleep_score"] = sleep.get("score")
    row["sleep_duration_s"] = sleep.get("duration_s")
    days_out.append(row)

act_fields = [
    "date", "start_local", "name", "type", "duration_s", "distance_km", "pace",
    "avg_hr", "max_hr", "calories", "elevation_gain_m",
    "training_effect_aerobic", "training_effect_anaerobic", "training_effect_label",
    "training_load", "avg_power_w", "norm_power_w", "avg_cadence_spm", "location",
    "moderate_intensity_min", "vigorous_intensity_min", "hr_zones_min",
]
activities_out = [{k: a.get(k) for k in act_fields} for a in data["activities"]]

out = {
    "meta": data["meta"],
    "athlete": athlete_out,
    "weeks": data["weeks"],
    "sports": data["sports"],
    "days": days_out,
    "activities": activities_out,
    "problems": data.get("problems", []),
}

OUT.write_text(json.dumps(out, default=str))
print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
