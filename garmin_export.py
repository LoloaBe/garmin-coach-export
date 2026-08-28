#!/usr/bin/env python3
"""Garmin Connect -> JSON exporter, built to feed an AI coach.

Wraps cyberjunky/python-garminconnect (https://github.com/cyberjunky/python-garminconnect),
which speaks the same mobile SSO flow as the official Garmin Connect Android app.

One file per run: <out>/json/garmin_<start>_to_<end>.json - a flat, unit-consistent digest
(days, weeks, activities, per-sport volume) plus every untouched Garmin payload under "raw".

Credentials are asked once, interactively. After that an OAuth token bundle lives in
~/.garminconnect and is refreshed silently, so scheduled runs need no password.

Quick start:
    python garmin_export.py --days 30
    python garmin_export.py --since-last --profile full     # incremental, everything

Environment (all optional):
    GARMIN_EMAIL, GARMIN_PASSWORD   skip the interactive prompts
    GARMINTOKENS                    token directory (default ~/.garminconnect)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any, Callable

try:
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
except ImportError:  # pragma: no cover - startup guard
    sys.exit(
        "Missing dependency 'garminconnect'.\n"
        "  pip install -r requirements.txt\n"
        "or:\n"
        "  pip install garminconnect\n"
        "Note: garminconnect requires Python 3.12+ (you are on "
        f"{sys.version_info.major}.{sys.version_info.minor})."
    )

SCHEMA_VERSION = "1.0"
DEFAULT_TOKENSTORE = "~/.garminconnect"
STATE_FILENAME = ".sync_state.json"

# The library is chatty on endpoints Garmin no longer serves; we do our own reporting.
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
log = logging.getLogger("garmin_export")


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    """Walk dicts/lists by key or index, returning `default` on any miss.

    Garmin's payloads change shape between firmware versions and account tiers, so every
    read goes through here rather than chained .get() calls that explode on None.
    """
    cur = obj
    for key in path:
        if cur is None:
            return default
        if isinstance(key, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= key < len(cur):
                cur = cur[key]
            else:
                return default
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return default if cur is None else cur


def as_dict(value: Any) -> dict[str, Any]:
    """Coerce to a dict, or {}.

    Garmin sometimes answers with a bare string or a list where its own schema says
    object - an error body, an empty-account placeholder - and every reader below
    assumes .get() works.
    """
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """Coerce to a list, or []. Same reasoning as as_dict()."""
    return value if isinstance(value, list) else []


def first(seq: Any, default: Any = None) -> Any:
    """First element of a list, or `default`. Several endpoints return a 1-item list."""
    if isinstance(seq, (list, tuple)) and seq:
        return seq[0]
    return default


def num(value: Any) -> float | None:
    """Coerce to float, or None. Garmin mixes ints, floats, strings and nulls."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None




def fmt_pace(speed_mps: Any) -> str:
    """Average speed in m/s -> '5:07/km'."""
    v = num(speed_mps)
    if v is None or v <= 0:
        return "-"
    sec_per_km = 1000.0 / v
    if sec_per_km > 3600:
        return "-"
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"



CYCLING_HINTS = ("cycling", "ride", "biking", "ebike", "handcycl")
SWIM_HINTS = ("swim",)


def fmt_speed(type_key: Any, speed_mps: Any) -> str:
    """Pace or speed in the unit the sport is actually coached in.

    min/km reads as nonsense on a bike leg and per-100m is the only useful swim unit,
    so branch on the activity type rather than printing min/km for everything.
    """
    v = num(speed_mps)
    if v is None or v <= 0:
        return "-"
    key = str(type_key or "").lower()
    if any(hint in key for hint in CYCLING_HINTS):
        return f"{v * 3.6:.1f} km/h"
    if any(hint in key for hint in SWIM_HINTS):
        m, s = divmod(int(round(100.0 / v)), 60)
        return f"{m}:{s:02d}/100m"
    return fmt_pace(v)



def _looks_like_date(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-"


def clean_ts(value: Any) -> str:
    """'2026-08-27T06:14:00.0' -> '2026-08-27 06:14'."""
    if not isinstance(value, str) or not value:
        return "-"
    text = value.replace("T", " ")
    return text[:16] if len(text) >= 16 else text


def parse_day(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--{label} must be YYYY-MM-DD, got {value!r}")


def day_span(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


def authenticate(tokenstore: str, force_login: bool = False) -> Garmin:
    """Return a logged-in client, reusing saved OAuth tokens when possible.

    First run asks for email / password / MFA code. The resulting token bundle carries a
    refresh token, so every later run - including cron - reconnects without a prompt.
    """
    token_path = str(Path(tokenstore).expanduser())

    if not force_login:
        try:
            api = Garmin()
            api.login(token_path)
            print(f"Connected as {api.get_full_name() or 'Garmin user'} (saved tokens).")
            return api
        except GarminConnectTooManyRequestsError as err:
            raise SystemExit(f"Garmin is rate-limiting this account: {err}\nWait ~15 min.")
        except (GarminConnectAuthenticationError, GarminConnectConnectionError):
            print("No usable saved session - signing in.")

    print(
        "\nGarmin Connect sign-in. Credentials are sent straight to Garmin and never\n"
        f"stored by this script; only the OAuth tokens are written to {token_path}.\n"
    )

    while True:
        email = os.getenv("GARMIN_EMAIL") or input("  Garmin email: ").strip()
        password = os.getenv("GARMIN_PASSWORD") or getpass("  Garmin password: ")
        if not email or not password:
            raise SystemExit("Email and password are both required.")

        try:
            api = Garmin(
                email=email,
                password=password,
                prompt_mfa=lambda: input("  MFA / 2FA code: ").strip(),
            )
            password = None  # drop the plaintext as soon as the client holds it
            api.login(token_path)
            print(f"Signed in as {api.get_full_name() or email}. Tokens saved to {token_path}.")
            return api
        except GarminConnectTooManyRequestsError as err:
            raise SystemExit(f"Garmin is rate-limiting this account: {err}\nWait ~15 min.")
        except GarminConnectAuthenticationError:
            if os.getenv("GARMIN_EMAIL") or os.getenv("GARMIN_PASSWORD"):
                raise SystemExit("Rejected credentials from GARMIN_EMAIL / GARMIN_PASSWORD.")
            print("  Rejected by Garmin - check the email/password and try again.\n")
        except GarminConnectConnectionError as err:
            raise SystemExit(f"Could not reach Garmin Connect: {err}")
        except KeyboardInterrupt:
            raise SystemExit("\nCancelled.")


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


@dataclass
class Fetcher:
    """Calls Garmin endpoints, swallowing per-endpoint failures into a report.

    A single dead endpoint - Garmin retires them regularly, and several are gated on
    device model or account region - must never cost you the rest of the export.
    """

    api: Garmin
    pause: float = 0.4
    verbose: bool = False
    calls: int = 0
    problems: list[dict[str, str]] = field(default_factory=list)

    def get(self, label: str, method: str, *args: Any, **kwargs: Any) -> Any:
        fn: Callable[..., Any] | None = getattr(self.api, method, None)
        if fn is None:
            self.problems.append({"item": label, "reason": f"{method}() not in this garminconnect version"})
            return None

        self.calls += 1
        try:
            result = fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError as err:
            self.problems.append({"item": label, "reason": f"rate limited: {err}"})
            print(f"  ! rate limited on {label} - backing off 30s", file=sys.stderr)
            time.sleep(30)
            return None
        except GarminConnectAuthenticationError as err:
            raise SystemExit(f"Session expired mid-export ({label}): {err}\nRe-run with --login.")
        except Exception as err:  # noqa: BLE001 - one endpoint must not sink the run
            self.problems.append({"item": label, "reason": f"{type(err).__name__}: {err}"})
            if self.verbose:
                print(f"  ! {label}: {err}", file=sys.stderr)
            return None
        finally:
            if self.pause:
                time.sleep(self.pause)

        if self.verbose:
            print(f"  . {label}")
        return result


# Per-day endpoints. "core" is the AI-coach essentials; "full" adds the long tail.
DAILY_CORE: list[tuple[str, str]] = [
    ("summary", "get_user_summary"),
    ("sleep", "get_sleep_data"),
    ("hrv", "get_hrv_data"),
    ("readiness", "get_training_readiness"),
    ("stress", "get_all_day_stress"),
]

DAILY_FULL: list[tuple[str, str]] = DAILY_CORE + [
    ("training_status", "get_training_status"),
    ("max_metrics", "get_max_metrics"),
    ("spo2", "get_spo2_data"),
    ("respiration", "get_respiration_data"),
    ("intensity_minutes", "get_intensity_minutes_data"),
    ("floors", "get_floors"),
    ("hydration", "get_hydration_data"),
    ("body_battery_events", "get_body_battery_events"),
]


def collect(fetch: Fetcher, days: list[date], cfg: "Config") -> dict[str, Any]:
    """Pull everything for the window into one nested dict of raw Garmin payloads."""
    start, end = days[0].isoformat(), days[-1].isoformat()
    daily_endpoints = DAILY_FULL if cfg.profile == "full" else DAILY_CORE

    print(f"\nFetching {len(days)} day(s): {start} -> {end}  [profile: {cfg.profile}]")

    # --- one-shot snapshots -----------------------------------------------------------
    print("  athlete snapshot ...")
    snapshot = {
        "profile": fetch.get("user profile", "get_user_profile"),
        "settings": fetch.get("profile settings", "get_userprofile_settings"),
        "devices": fetch.get("devices", "get_devices"),
        "heart_rate_zones": fetch.get("HR zones", "get_heart_rate_zones"),
        "personal_records": fetch.get("personal records", "get_personal_record"),
        "race_predictions": fetch.get("race predictions", "get_race_predictions"),
        "training_status": fetch.get("training status", "get_training_status", end),
        "max_metrics": fetch.get("VO2 max", "get_max_metrics", end),
        "fitness_age": fetch.get("fitness age", "get_fitnessage_data", end),
        "endurance_score": fetch.get("endurance score", "get_endurance_score", end),
        "hill_score": fetch.get("hill score", "get_hill_score", end),
        "goals": fetch.get("goals", "get_goals", "active"),
        "gear": None,
    }
    display_name = dig(snapshot, "profile", "displayName") or dig(snapshot, "profile", "userName")
    if display_name:
        snapshot["gear"] = fetch.get("gear", "get_gear", str(display_name))

    # --- range endpoints: one call covers the whole window -----------------------------
    print("  range series ...")
    ranges = {
        "daily_steps": fetch.get("daily steps", "get_daily_steps", start, end),
        "resting_hr": fetch.get("resting HR series", "get_rhr_daily", start, end),
        "calories": fetch.get("calories series", "get_calories_daily", start, end),
        "body_battery": fetch.get("body battery series", "get_body_battery", start, end),
        "sleep_summary": fetch.get("sleep series", "get_sleep_daily", start, end),
        "hrv": fetch.get("HRV series", "get_hrv_data_range", start, end),
        "max_metrics": fetch.get("VO2 max series", "get_max_metrics_range", start, end),
        "weigh_ins": fetch.get("weigh-ins", "get_weigh_ins", start, end),
        "progress_summary": fetch.get(
            "progress summary", "get_progress_summary_between_dates", start, end
        ),
    }

    # --- per-day loop ------------------------------------------------------------------
    daily: dict[str, dict[str, Any]] = {}
    for idx, day in enumerate(days, start=1):
        iso = day.isoformat()
        print(f"  [{idx}/{len(days)}] {iso}", end="\r", flush=True)
        bucket: dict[str, Any] = {}
        for key, method in daily_endpoints:
            bucket[key] = fetch.get(f"{iso} {key}", method, iso)
        if cfg.intraday:
            bucket["heart_rate_intraday"] = fetch.get(f"{iso} HR intraday", "get_heart_rates", iso)
            bucket["steps_intraday"] = fetch.get(f"{iso} steps intraday", "get_steps_data", iso)
        daily[iso] = bucket
    print(" " * 60, end="\r")

    # --- activities --------------------------------------------------------------------
    print("  activities ...")
    activities = fetch.get("activity list", "get_activities_by_date", start, end, None, "asc") or []
    if cfg.activity_details and activities:
        for idx, act in enumerate(activities, start=1):
            act_id = act.get("activityId")
            if act_id is None:
                continue
            print(f"  activity detail [{idx}/{len(activities)}]", end="\r", flush=True)
            act["_splits"] = fetch.get(f"splits {act_id}", "get_activity_splits", act_id)
            act["_weather"] = fetch.get(f"weather {act_id}", "get_activity_weather", act_id)
            act["_hr_zones"] = fetch.get(f"hr zones {act_id}", "get_activity_hr_in_timezones", act_id)
            act["_exercise_sets"] = fetch.get(f"sets {act_id}", "get_activity_exercise_sets", act_id)
        print(" " * 60, end="\r")

    return {
        "snapshot": snapshot,
        "ranges": ranges,
        "daily": daily,
        "activities": activities,
    }


def download_activity_files(fetch: Fetcher, activities: list[dict], out_dir: Path, fmt: str) -> int:
    """Save original FIT/TCX/GPX/CSV files next to the export, for deeper offline analysis."""
    target = out_dir / "activity_files"
    target.mkdir(parents=True, exist_ok=True)
    dl_fmt = getattr(Garmin.ActivityDownloadFormat, fmt.upper())
    suffix = {"ORIGINAL": "zip", "TCX": "tcx", "GPX": "gpx", "KML": "kml", "CSV": "csv"}[fmt.upper()]

    saved = 0
    for act in activities:
        act_id = act.get("activityId")
        if act_id is None:
            continue
        stamp = act.get("startTimeLocal")
        stamp = stamp[:10] if _looks_like_date(stamp) else "undated"
        path = target / f"{stamp}_{act_id}.{suffix}"
        if path.exists():
            continue
        data = fetch.get(f"download {act_id}", "download_activity", str(act_id), dl_fmt)
        if data:
            path.write_bytes(data)
            saved += 1
    return saved


# --------------------------------------------------------------------------------------
# Distillation: raw Garmin payloads -> a compact, coach-readable digest
# --------------------------------------------------------------------------------------

DATE_KEYS = (
    "calendarDate",
    "date",
    "summaryDate",
    "statisticsStartDate",
    "startDate",
    "wellnessStartDate",
)


def index_by_date(payload: Any) -> dict[str, Any]:
    """Best-effort {'YYYY-MM-DD': entry} index over Garmin's many dated-list shapes."""
    out: dict[str, Any] = {}

    def date_of(item: dict[str, Any]) -> str | None:
        for key in DATE_KEYS:
            if _looks_like_date(item.get(key)):
                return str(item[key])[:10]
        return None

    def consider(item: Any) -> None:
        if not isinstance(item, dict):
            return
        stamp = date_of(item)
        if stamp is None:
            # Some series (max metrics) carry the date one level down, inside the
            # metric object rather than on the row itself.
            for value in item.values():
                if isinstance(value, dict):
                    stamp = date_of(value)
                    if stamp:
                        break
        if stamp:
            out.setdefault(stamp, item)

    if isinstance(payload, list):
        for item in payload:
            consider(item)
    elif isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                for item in value:
                    consider(item)
        consider(payload)
    return out


def digest_activity(act: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Garmin activity record to the fields a coach actually reasons about."""
    act = as_dict(act)
    duration = num(act.get("duration"))
    distance = num(act.get("distance"))
    zones = {}
    for i in range(1, 6):
        secs = num(act.get(f"hrTimeInZone_{i}"))
        if secs:
            zones[f"z{i}_min"] = round(secs / 60, 1)

    return {
        "activity_id": act.get("activityId"),
        "date": (lambda s: s[:10] if _looks_like_date(s) else None)(act.get("startTimeLocal")),
        "start_local": clean_ts(act.get("startTimeLocal")),
        "name": act.get("activityName"),
        "type": dig(act, "activityType", "typeKey"),
        "duration_s": duration,
        "moving_s": num(act.get("movingDuration")),
        "distance_m": distance,
        "distance_km": round(distance / 1000, 2) if distance else None,
        # Pre-rendered in the sport's own unit: min/km, km/h or min/100m.
        "pace": fmt_speed(dig(act, "activityType", "typeKey"), act.get("averageSpeed")),
        "avg_speed_mps": num(act.get("averageSpeed")),
        "max_speed_mps": num(act.get("maxSpeed")),
        "avg_hr": num(act.get("averageHR")),
        "max_hr": num(act.get("maxHR")),
        "calories": num(act.get("calories")),
        "elevation_gain_m": num(act.get("elevationGain")),
        "elevation_loss_m": num(act.get("elevationLoss")),
        "training_effect_aerobic": num(act.get("aerobicTrainingEffect")),
        "training_effect_anaerobic": num(act.get("anaerobicTrainingEffect")),
        "training_effect_label": act.get("trainingEffectLabel"),
        "training_load": num(act.get("activityTrainingLoad")),
        "avg_power_w": num(act.get("avgPower")),
        "norm_power_w": num(act.get("normPower")),
        "max_power_w": num(act.get("maxPower")),
        "avg_cadence_spm": num(act.get("averageRunningCadenceInStepsPerMinute"))
        or num(act.get("averageBikingCadenceInRevPerMinute")),
        "avg_stride_length_cm": num(act.get("avgStrideLength")),
        "ground_contact_ms": num(act.get("avgGroundContactTime")),
        "vertical_oscillation_cm": num(act.get("avgVerticalOscillation")),
        "vo2max": num(act.get("vO2MaxValue")),
        "steps": num(act.get("steps")),
        "moderate_intensity_min": num(act.get("moderateIntensityMinutes")),
        "vigorous_intensity_min": num(act.get("vigorousIntensityMinutes")),
        "hr_zones_min": zones or None,
        "location": act.get("locationName"),
        "temperature_c": num(act.get("maxTemperature")),
        "description": act.get("description"),
        "has_splits": bool(act.get("_splits")),
    }


def digest_day(iso: str, bucket: dict[str, Any], series: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fold every source for one calendar day into a single flat record."""
    bucket = as_dict(bucket)
    summary = as_dict(bucket.get("summary"))
    sleep_raw = as_dict(bucket.get("sleep"))
    sleep_dto = as_dict(dig(sleep_raw, "dailySleepDTO"))
    hrv_raw = as_dict(bucket.get("hrv"))
    readiness_src = bucket.get("readiness")
    readiness = as_dict(first(readiness_src) if isinstance(readiness_src, list) else readiness_src)
    stress = as_dict(bucket.get("stress"))

    def row(name: str) -> dict[str, Any]:
        return as_dict(as_dict(series.get(name)).get(iso))

    steps_row = row("daily_steps")
    rhr_row = row("resting_hr")
    cal_row = row("calories")
    bb_row = row("body_battery")
    hrv_row = row("hrv")
    vo2_row = row("max_metrics")
    weight_row = row("weigh_ins")

    # Training status hides the interesting bits under a per-device map.
    ts_raw = as_dict(bucket.get("training_status"))
    ts_device = as_dict(first(list(as_dict(dig(ts_raw, "mostRecentTrainingStatus", "latestTrainingStatusData")).values())))
    load_device = as_dict(first(list(as_dict(dig(ts_raw, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")).values())))

    max_metrics = as_dict(first(bucket.get("max_metrics")))

    sleep_seconds = num(sleep_dto.get("sleepTimeSeconds"))
    weight_g = num(dig(weight_row, "allWeightMetrics", 0, "weight")) or num(weight_row.get("weight"))

    return {
        "date": iso,
        "weekday": datetime.strptime(iso, "%Y-%m-%d").strftime("%A"),
        # --- activity volume ---
        "steps": num(summary.get("totalSteps")) or num(steps_row.get("totalSteps")),
        "step_goal": num(summary.get("dailyStepGoal")) or num(steps_row.get("stepGoal")),
        "distance_m": num(summary.get("totalDistanceMeters")) or num(steps_row.get("totalDistance")),
        "floors_up": num(summary.get("floorsAscended")),
        "calories_total": num(summary.get("totalKilocalories")) or num(cal_row.get("total")),
        "calories_active": num(summary.get("activeKilocalories")) or num(cal_row.get("active")),
        "calories_bmr": num(summary.get("bmrKilocalories")) or num(cal_row.get("resting")),
        "intensity_min_moderate": num(summary.get("moderateIntensityMinutes")),
        "intensity_min_vigorous": num(summary.get("vigorousIntensityMinutes")),
        # --- heart ---
        "resting_hr": num(summary.get("restingHeartRate")) or num(rhr_row.get("value")),
        "min_hr": num(summary.get("minHeartRate")),
        "max_hr": num(summary.get("maxHeartRate")),
        # --- sleep ---
        "sleep": {
            "duration_s": sleep_seconds,
            "score": num(dig(sleep_dto, "sleepScores", "overall", "value")),
            "quality": dig(sleep_dto, "sleepScores", "overall", "qualifierKey"),
            "deep_s": num(sleep_dto.get("deepSleepSeconds")),
            "light_s": num(sleep_dto.get("lightSleepSeconds")),
            "rem_s": num(sleep_dto.get("remSleepSeconds")),
            "awake_s": num(sleep_dto.get("awakeSleepSeconds")),
            "bed_time": clean_ts(sleep_dto.get("sleepStartTimestampLocal")),
            "wake_time": clean_ts(sleep_dto.get("sleepEndTimestampLocal")),
            "avg_overnight_hrv": num(sleep_raw.get("avgOvernightHrv")),
            "hrv_status": sleep_raw.get("hrvStatus"),
            "resting_hr": num(sleep_raw.get("restingHeartRate")),
            "avg_spo2": num(sleep_raw.get("averageSpO2Value")),
            "avg_respiration": num(sleep_raw.get("avgSleepRespirationValue")),
            "restless_moments": num(sleep_raw.get("restlessMomentsCount")),
            "body_battery_change": num(sleep_raw.get("bodyBatteryChange")),
            "sleep_need_s": num(dig(sleep_raw, "sleepNeed", "actual")),
            "feedback": sleep_raw.get("sleepScoreFeedback") or sleep_raw.get("sleepScoreInsight"),
        },
        # --- HRV ---
        "hrv": {
            "last_night_avg_ms": num(dig(hrv_raw, "hrvSummary", "lastNightAvg")) or num(hrv_row.get("lastNightAvg")),
            "last_night_5min_high": num(dig(hrv_raw, "hrvSummary", "lastNight5MinHigh")) or num(hrv_row.get("lastNight5MinHigh")),
            "weekly_avg_ms": num(dig(hrv_raw, "hrvSummary", "weeklyAvg")) or num(hrv_row.get("weeklyAvg")),
            "status": dig(hrv_raw, "hrvSummary", "status") or hrv_row.get("status"),
            "baseline_low": num(dig(hrv_raw, "hrvSummary", "baseline", "balancedLow")),
            "baseline_high": num(dig(hrv_raw, "hrvSummary", "baseline", "balancedUpper")),
            "feedback": dig(hrv_raw, "hrvSummary", "feedbackPhrase"),
        },
        # --- readiness / recovery ---
        "readiness": {
            "score": num(readiness.get("score")),
            "level": readiness.get("level"),
            "feedback": readiness.get("feedbackLong") or readiness.get("feedbackShort"),
            "sleep_score": num(readiness.get("sleepScore")),
            "recovery_time_min": num(readiness.get("recoveryTime")),
            "hrv_factor_pct": num(readiness.get("hrvFactorPercent")),
            "acute_load": num(readiness.get("acuteLoad")),
            "stress_history_pct": num(readiness.get("stressHistoryFactorPercent")),
            "sleep_history_pct": num(readiness.get("sleepHistoryFactorPercent")),
        },
        # --- stress & body battery ---
        "stress_avg": num(summary.get("averageStressLevel")) or num(stress.get("avgStressLevel")),
        "stress_max": num(summary.get("maxStressLevel")) or num(stress.get("maxStressLevel")),
        "body_battery_high": num(summary.get("bodyBatteryHighestValue")) or num(bb_row.get("charged")),
        "body_battery_low": num(summary.get("bodyBatteryLowestValue")),
        "body_battery_charged": num(summary.get("bodyBatteryChargedValue")) or num(bb_row.get("charged")),
        "body_battery_drained": num(summary.get("bodyBatteryDrainedValue")) or num(bb_row.get("drained")),
        # --- fitness markers ---
        "training_status": {
            "status": ts_device.get("trainingStatus") or ts_device.get("trainingStatusFeedbackPhrase"),
            "feedback": ts_device.get("trainingStatusFeedbackPhrase"),
            "weekly_load": num(ts_device.get("weeklyTrainingLoad")),
            "load_trend": ts_device.get("loadLevelTrend"),
            "acute_load": num(dig(ts_raw, "mostRecentTrainingStatus", "acuteTrainingLoadDTO", "acwrPercent")),
            "load_balance_feedback": load_device.get("trainingBalanceFeedbackPhrase"),
            "load_aerobic_low": num(load_device.get("monthlyLoadAerobicLow")),
            "load_aerobic_high": num(load_device.get("monthlyLoadAerobicHigh")),
            "load_anaerobic": num(load_device.get("monthlyLoadAnaerobic")),
        },
        "vo2max_running": num(dig(max_metrics, "generic", "vo2MaxPreciseValue"))
        or num(dig(max_metrics, "generic", "vo2MaxValue"))
        or num(dig(vo2_row, "generic", "vo2MaxPreciseValue"))
        or num(dig(vo2_row, "generic", "vo2MaxValue")),
        "vo2max_cycling": num(dig(max_metrics, "cycling", "vo2MaxPreciseValue"))
        or num(dig(vo2_row, "cycling", "vo2MaxPreciseValue")),
        "fitness_age": num(dig(max_metrics, "generic", "fitnessAge"))
        or num(dig(vo2_row, "generic", "fitnessAge")),
        # --- misc wellness ---
        "spo2_avg": num(dig(bucket, "spo2", "averageSpO2")),
        "respiration_avg": num(dig(bucket, "respiration", "avgSleepRespirationValue"))
        or num(dig(bucket, "respiration", "avgWakingRespirationValue")),
        "hydration_ml": num(dig(bucket, "hydration", "valueInML")),
        "weight_kg": round(weight_g / 1000, 2) if weight_g else None,
        "body_fat_pct": num(dig(weight_row, "allWeightMetrics", 0, "bodyFat")),
    }


# Garmin exposes personal records as numeric type ids. 1-6 are the well-known running
# distances; anything else falls back to whatever label the payload carries.
PR_TYPES = {
    1: "1 km (time)",
    2: "1 mile (time)",
    3: "5 km (time)",
    4: "10 km (time)",
    5: "Half marathon (time)",
    6: "Marathon (time)",
    7: "Longest run (distance)",
    8: "Longest ride (distance)",
    9: "Biggest ascent",
    12: "Most steps in a day",
    13: "Most steps in a week",
    14: "Most steps in a month",
}


def digest_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """One-time athlete context: who they are, what gear, what the watch currently thinks."""
    snapshot = as_dict(snapshot)
    profile = as_dict(snapshot.get("profile"))
    settings = as_dict(snapshot.get("settings"))
    max_metrics = as_dict(first(snapshot.get("max_metrics")))
    ts_raw = as_dict(snapshot.get("training_status"))
    ts_device = as_dict(first(list(as_dict(dig(ts_raw, "mostRecentTrainingStatus", "latestTrainingStatusData")).values())))

    predictions_src = snapshot.get("race_predictions")
    predictions = as_dict(predictions_src if isinstance(predictions_src, dict) else first(predictions_src))

    records = []
    for rec in as_list(snapshot.get("personal_records")):
        if not isinstance(rec, dict):
            continue
        type_id = rec.get("typeId")
        records.append(
            {
                "type_id": type_id,
                "label": PR_TYPES.get(type_id) or rec.get("activityName") or rec.get("activityType"),
                "value": num(rec.get("value")),
                "date": clean_ts(rec.get("prStartTimeLocal") or rec.get("prTypeLabelKey")),
                "activity_id": rec.get("activityId"),
            }
        )

    devices = []
    for dev in as_list(snapshot.get("devices")):
        if not isinstance(dev, dict):
            continue
        devices.append(
            {
                "name": dev.get("displayName") or dev.get("productDisplayName"),
                "model": dev.get("productDisplayName") or dev.get("partNumber"),
                "serial_suffix": str(dev.get("serialNumber", ""))[-4:] or None,
                "software": dev.get("softwareVersion"),
            }
        )

    return {
        "name": profile.get("fullName") or profile.get("displayName"),
        "display_name": profile.get("displayName"),
        "location": profile.get("location"),
        "unit_system": dig(settings, "userData", "measurementSystem") or profile.get("measurementSystem"),
        "gender": dig(settings, "userData", "gender"),
        "birth_date": dig(settings, "userData", "birthDate"),
        "height_cm": num(dig(settings, "userData", "height")),
        "weight_kg": (lambda w: round(w / 1000, 1) if w else None)(num(dig(settings, "userData", "weight"))),
        "activity_level": dig(settings, "userData", "activityLevel"),
        "vo2max_running": num(dig(max_metrics, "generic", "vo2MaxPreciseValue"))
        or num(dig(max_metrics, "generic", "vo2MaxValue")),
        "vo2max_cycling": num(dig(max_metrics, "cycling", "vo2MaxPreciseValue")),
        "chronological_age": num(dig(snapshot, "fitness_age", "chronologicalAge")),
        "fitness_age": num(dig(snapshot, "fitness_age", "fitnessAge"))
        or num(dig(max_metrics, "generic", "fitnessAge")),
        "fitness_age_achievable": num(dig(snapshot, "fitness_age", "achievableFitnessAge")),
        "training_status": ts_device.get("trainingStatus"),
        "training_status_feedback": ts_device.get("trainingStatusFeedbackPhrase"),
        "weekly_training_load": num(ts_device.get("weeklyTrainingLoad")),
        "vo2max_from_status": num(ts_raw.get("mostRecentVO2Max"))
        or num(dig(ts_raw, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue")),
        "race_predictions_s": {
            "5k": num(predictions.get("time5K")),
            "10k": num(predictions.get("time10K")),
            "half_marathon": num(predictions.get("timeHalfMarathon")),
            "marathon": num(predictions.get("timeMarathon")),
        },
        "endurance_score": num(dig(snapshot, "endurance_score", "overallScore"))
        or num(dig(snapshot, "endurance_score", "avg")),
        "hill_score": num(dig(snapshot, "hill_score", "overallScore"))
        or num(dig(snapshot, "hill_score", "hillScore")),
        "heart_rate_zones": snapshot.get("heart_rate_zones"),
        "devices": devices,
        "personal_records": records,
        "gear": [
            {
                "name": g.get("displayName") or g.get("customMakeModel"),
                "type": g.get("gearTypeName"),
                "uuid": g.get("uuid"),
            }
            for g in as_list(snapshot.get("gear"))
            if isinstance(g, dict)
        ],
    }


def sport_totals(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Volume per sport. A single mixed distance total would compare a swim to a bike leg."""
    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0.0, "distance_km": 0.0, "duration_s": 0.0, "load": 0.0, "calories": 0.0}
    )
    for act in activities:
        b = buckets[act.get("type") or "other"]
        b["sessions"] += 1
        b["distance_km"] += act.get("distance_km") or 0.0
        b["duration_s"] += act.get("duration_s") or 0.0
        b["load"] += act.get("training_load") or 0.0
        b["calories"] += act.get("calories") or 0.0

    return [
        {
            "sport": sport,
            "sessions": int(b["sessions"]),
            "distance_km": round(b["distance_km"], 1),
            "duration_s": round(b["duration_s"]),
            "training_load": round(b["load"]),
            "calories": round(b["calories"]),
        }
        for sport, b in sorted(buckets.items(), key=lambda kv: -kv[1]["duration_s"])
    ]


def weekly_rollup(days: list[dict[str, Any]], activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate the daily digest into ISO weeks - the unit a coach plans in."""
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "sessions": 0,
            "distance_km": 0.0,
            "duration_s": 0.0,
            "load": 0.0,
            "rhr": [],
            "sleep_s": [],
            "sleep_score": [],
            "hrv": [],
            "readiness": [],
            "steps": [],
        }
    )

    def week_of(iso: Any) -> str | None:
        try:
            year, week, _ = datetime.strptime(str(iso), "%Y-%m-%d").date().isocalendar()
        except ValueError:
            return None
        return f"{year}-W{week:02d}"

    for day in days:
        week = week_of(day.get("date"))
        if week is None:
            continue
        b = buckets[week]
        for key, value in (
            ("rhr", day.get("resting_hr")),
            ("sleep_s", dig(day, "sleep", "duration_s")),
            ("sleep_score", dig(day, "sleep", "score")),
            ("hrv", dig(day, "hrv", "last_night_avg_ms")),
            ("readiness", dig(day, "readiness", "score")),
            ("steps", day.get("steps")),
        ):
            if value is not None:
                b[key].append(value)

    for act in activities:
        week = week_of(act.get("date"))
        if week is None:
            continue
        b = buckets[week]
        b["sessions"] += 1
        b["distance_km"] += act.get("distance_km") or 0.0
        b["duration_s"] += act.get("duration_s") or 0.0
        b["load"] += act.get("training_load") or 0.0

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    rows = []
    for week in sorted(buckets):
        b = buckets[week]
        rows.append(
            {
                "week": week,
                "sessions": b["sessions"],
                "distance_km": round(b["distance_km"], 1),
                "duration_s": round(b["duration_s"]),
                "training_load": round(b["load"]),
                "avg_resting_hr": mean(b["rhr"]),
                "avg_sleep_s": mean(b["sleep_s"]),
                "avg_sleep_score": mean(b["sleep_score"]),
                "avg_hrv_ms": mean(b["hrv"]),
                "avg_readiness": mean(b["readiness"]),
                "avg_steps": mean(b["steps"]),
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


@dataclass
class Config:
    profile: str = "core"
    intraday: bool = False
    activity_details: bool = False
    pause: float = 0.4
    verbose: bool = False


def read_state(out_dir: Path) -> dict[str, Any]:
    path = out_dir / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(out_dir: Path, state: dict[str, Any]) -> None:
    (out_dir / STATE_FILENAME).write_text(json.dumps(state, indent=2) + "\n")


def resolve_window(args: argparse.Namespace, out_dir: Path) -> tuple[date, date]:
    """Work out which days to pull, honouring --start/--end, --days or --since-last."""
    today = date.today()
    end = parse_day(args.end, "end") if args.end else today

    if args.start:
        start = parse_day(args.start, "start")
    elif args.since_last:
        last = read_state(out_dir).get("last_end_date")
        if last:
            # Re-pull a couple of days: Garmin backfills sleep and HRV hours late.
            start = parse_day(last, "state") - timedelta(days=args.overlap)
            print(f"Incremental run: last export ended {last}, re-syncing from {start}.")
        else:
            start = end - timedelta(days=args.days - 1)
            print(f"No previous sync found — falling back to the last {args.days} days.")
    else:
        start = end - timedelta(days=args.days - 1)

    if start > end:
        raise SystemExit(f"Start date {start} is after end date {end}.")
    if (end - start).days > 730:
        raise SystemExit("Window longer than 2 years; split it into smaller runs.")
    return start, end


def build_payload(raw: dict[str, Any], days: list[date], cfg: Config, problems: list[dict[str, str]], calls: int) -> dict[str, Any]:
    """Assemble the export document: metadata + digest + the untouched raw payloads."""
    series = {name: index_by_date(payload) for name, payload in as_dict(raw.get("ranges")).items()}
    daily_raw = as_dict(raw.get("daily"))
    day_records = [digest_day(d.isoformat(), daily_raw.get(d.isoformat(), {}), series) for d in days]
    activity_records = [digest_activity(a) for a in as_list(raw.get("activities")) if isinstance(a, dict)]
    athlete = digest_snapshot(raw.get("snapshot"))

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "Garmin Connect via python-garminconnect",
            "start_date": days[0].isoformat(),
            "end_date": days[-1].isoformat(),
            "day_count": len(days),
            "profile": cfg.profile,
            "intraday_included": cfg.intraday,
            "activity_details_included": cfg.activity_details,
            "api_calls": calls,
        },
        "athlete": athlete,
        "weeks": weekly_rollup(day_records, activity_records),
        "sports": sport_totals(activity_records),
        "days": day_records,
        "activities": activity_records,
        "problems": problems,
        "raw": raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="garmin_export",
        description="Download Garmin Connect data and export it as JSON for an AI coach.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python garmin_export.py --days 30\n"
            "  python garmin_export.py --start 2026-01-01 --end 2026-03-31 --profile full\n"
            "  python garmin_export.py --since-last          # incremental, for cron\n"
            "  python garmin_export.py --login               # force a fresh sign-in\n"
        ),
    )
    window = parser.add_argument_group("date window")
    window.add_argument("--days", type=int, default=30, help="number of days back from today (default: 30)")
    window.add_argument("--start", help="first day, YYYY-MM-DD")
    window.add_argument("--end", help="last day, YYYY-MM-DD (default: today)")
    window.add_argument("--since-last", action="store_true", help="continue from the last successful run")
    window.add_argument("--overlap", type=int, default=2, help="days re-pulled on --since-last (default: 2)")

    scope = parser.add_argument_group("what to fetch")
    scope.add_argument("--profile", choices=("core", "full"), default="core",
                       help="core = sleep/HRV/readiness/stress/summary; full adds SpO2, respiration, "
                            "training status, hydration, floors, VO2 max per day (default: core)")
    scope.add_argument("--activity-details", action="store_true",
                       help="also fetch splits, weather and HR zones per activity (slow)")
    scope.add_argument("--intraday", action="store_true",
                       help="include minute-level heart-rate and step arrays (large files)")
    scope.add_argument("--download", choices=("original", "tcx", "gpx", "kml", "csv"),
                       help="also save each activity's file (original = FIT zip)")

    output = parser.add_argument_group("output")
    output.add_argument("--out", default="./garmin_data", help="output directory (default: ./garmin_data)")
    output.add_argument("--no-raw", action="store_true", help="omit the raw Garmin payloads from the JSON")
    output.add_argument("--no-latest", action="store_true", help="skip writing latest.json")

    session = parser.add_argument_group("session")
    session.add_argument("--login", action="store_true", help="ignore saved tokens and sign in again")
    session.add_argument("--logout", action="store_true", help="delete saved tokens and exit")
    session.add_argument("--tokens", default=os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE),
                         help=f"token directory (default: {DEFAULT_TOKENSTORE})")
    session.add_argument("--pause", type=float, default=0.4,
                         help="seconds between API calls, to stay under Garmin's rate limit (default: 0.4)")
    session.add_argument("-v", "--verbose", action="store_true", help="log every endpoint call")

    args = parser.parse_args()

    if args.logout:
        token_dir = Path(args.tokens).expanduser()
        removed = 0
        if token_dir.is_dir():
            for item in token_dir.iterdir():
                if item.is_file():
                    item.unlink()
                    removed += 1
        print(f"Removed {removed} token file(s) from {token_dir}.")
        return 0

    if args.days < 1:
        raise SystemExit("--days must be at least 1.")

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    start, end = resolve_window(args, out_dir)
    days = day_span(start, end)
    cfg = Config(
        profile=args.profile,
        intraday=args.intraday,
        activity_details=args.activity_details,
        pause=max(0.0, args.pause),
        verbose=args.verbose,
    )

    api = authenticate(args.tokens, force_login=args.login)
    fetch = Fetcher(api=api, pause=cfg.pause, verbose=cfg.verbose)

    started = time.time()
    raw = collect(fetch, days, cfg)

    if args.download and raw["activities"]:
        print(f"  downloading activity files ({args.download}) ...")
        saved = download_activity_files(fetch, raw["activities"], out_dir, args.download)
        print(f"  saved {saved} new file(s) to {out_dir / 'activity_files'}")

    payload = build_payload(raw, days, cfg, fetch.problems, fetch.calls)
    if args.no_raw:
        payload.pop("raw", None)

    json_dir = out_dir / "json"
    json_dir.mkdir(exist_ok=True)
    document = json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    path = json_dir / f"garmin_{start.isoformat()}_to_{end.isoformat()}.json"
    path.write_text(document)
    written: list[Path] = [path]

    if not args.no_latest:
        latest = out_dir / "latest.json"
        latest.write_text(document)
        written.append(latest)

    write_state(
        out_dir,
        {
            "last_run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_start_date": start.isoformat(),
            "last_end_date": end.isoformat(),
            "last_profile": cfg.profile,
            "schema_version": SCHEMA_VERSION,
        },
    )

    elapsed = time.time() - started
    print(f"\nDone in {elapsed:.0f}s — {fetch.calls} API calls, {len(fetch.problems)} endpoint(s) with no data.")
    print(f"Days: {len(days)}  Activities: {len(payload['activities'])}")
    for path in written:
        size_kb = path.stat().st_size / 1024
        print(f"  {path}  ({size_kb:,.0f} KB)")
    if fetch.problems and not args.verbose:
        print('Run with -v to see which endpoints returned nothing (also under "problems" in the JSON).')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
