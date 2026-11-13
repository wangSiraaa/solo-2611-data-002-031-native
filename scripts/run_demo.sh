#!/usr/bin/env bash
# Start the site, run the helper, verify reports, show results.
#
#   scripts/run_demo.sh [variant]
#
# Expects serve-site on the default port 8765.  Set BASE_URL to point elsewhere.
set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT="${1:-a}"
PORT="${PORT:-8765}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
RUNTIME=".runtime"

mkdir -p "$RUNTIME/logs"
# Fresh database for a reproducible demo.
rm -f "$RUNTIME/site_${VARIANT}.sqlite" "$RUNTIME/site_${VARIANT}.sqlite-wal" \
      "$RUNTIME/site_${VARIANT}.sqlite-shm"
rm -rf "$RUNTIME/state" "$RUNTIME/trace"

python3 -m report_site.server --variant "$VARIANT" \
  --db "$RUNTIME/site_${VARIANT}.sqlite" --port "$PORT" --mode unscoped \
  > "$RUNTIME/logs/site_${VARIANT}.log" 2>&1 &
SITE_PID=$!
trap 'kill "$SITE_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if curl -fsS "$BASE_URL/__health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

python3 - "$BASE_URL" "$RUNTIME/demo_config.json" <<'PY'
import json, sys
base, out = sys.argv[1], sys.argv[2]
cfg = json.load(open("config/site.json", encoding="utf-8"))
cfg["base_url"] = base
json.dump(cfg, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

python3 -m report_helper.cli run \
  --config "$RUNTIME/demo_config.json" \
  --tasks config/tasks.example.json \
  --state-dir "$RUNTIME/state" --trace-dir "$RUNTIME/trace" \
  --summary-csv "$RUNTIME/summary.csv"

echo
python3 tools/verify_reports.py --truth "$BASE_URL/__truth.csv" \
  --state-dir "$RUNTIME/state" --summary-csv "$RUNTIME/verified.csv"

echo
python3 -m report_helper.cli status --state-dir "$RUNTIME/state"
