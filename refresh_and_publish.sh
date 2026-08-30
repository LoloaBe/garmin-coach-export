#!/usr/bin/env bash
# One command: refresh Garmin data -> condense -> render the site page -> commit + push.
#
# Run it any time:   bash refresh_and_publish.sh
# Or let the daily launchd job run it (see SETUP.md).
#
# Uses only credentials already on this machine (your cached Garmin login in
# ~/.garminconnect, your existing git credentials). Nothing new is stored anywhere.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Use the project venv (Python 3.13 + garminconnect). The system python3 on macOS
# does NOT have garminconnect installed, so falling back to it will fail at step 1 --
# we check explicitly and say so rather than failing with an opaque import error.
PYTHON="$HERE/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: $PYTHON not found."
  echo "Recreate the venv with:"
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

LOG="garmin_data/refresh.log"
mkdir -p garmin_data

# Always show the tail of the log when we exit, success or failure.
trap 'echo; echo "--- last 25 log lines ---"; tail -25 "$LOG"' EXIT

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

  echo "-- 1/4 Garmin export --"
  "$PYTHON" garmin_export.py --since-last --profile full --out garmin_data || exit 1

  echo "-- 2/4 Condense for the dashboard --"
  "$PYTHON" build_dashboard_data.py || exit 1

  echo "-- 3/4 Render the static site page --"
  "$PYTHON" render_site.py || exit 1

  echo "-- 4/4 Commit + push --"
  # site/index.html is the rendered console; dashboard_data.json is the condensed
  # data (~100 KB) kept in git as the historical record. The 38 MB latest.json
  # stays ignored -- see .gitignore.
  git add site/index.html garmin_data/dashboard_data.json
  if git diff --cached --quiet; then
    echo "No changes to commit -- data was already current."
  else
    git commit -m "Data refresh $(date '+%Y-%m-%d %H:%M')"
    if git push; then
      echo "Pushed. Once hosting is connected, this triggers a site redeploy."
    else
      echo "WARNING: commit succeeded but push failed (auth or network)."
      echo "Your work is saved locally -- run 'git push' by hand when convenient."
    fi
  fi

  echo "Done."
  echo
} >> "$LOG" 2>&1
