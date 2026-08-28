#!/usr/bin/env bash
# Wrapper for scheduled runs (launchd / cron). Logs to garmin_data/export.log.
#
# Runs incrementally: each run continues from where the last one stopped, with a
# 2-day overlap because Garmin backfills sleep and HRV hours after the fact.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$HERE/.venv/bin/python"
OUT="${GARMIN_OUT_DIR:-$HERE/garmin_data}"

[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
mkdir -p "$OUT"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  "$PYTHON" "$HERE/garmin_export.py" --since-last --profile full --out "$OUT" "$@"
  echo
} >> "$OUT/export.log" 2>&1
