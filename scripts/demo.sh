#!/usr/bin/env bash
# One-command demo: local synthetic site group -> full crawl -> export -> verify.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
CONFIG="${CONFIG:-config/demo.json}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
LOG=".runtime/site_access.log"
EXPORT=".runtime/export"

mkdir -p .runtime
: > "$LOG"
echo "== starting synthetic site group (local origins only) =="
"$PYTHON" -m webfrontier serve-sites --log "$LOG" --ready-file .runtime/sites-ready.json > .runtime/sites.out 2>&1 &
SITES_PID=$!
trap 'kill "$SITES_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  [ -f .runtime/sites-ready.json ] && break
  sleep 0.1
done
cat .runtime/sites.out

echo "== full crawl =="
"$PYTHON" -m webfrontier run --reset --config "$CONFIG"

echo "== status =="
"$PYTHON" -m webfrontier status --config "$CONFIG"

echo "== export =="
"$PYTHON" -m webfrontier export --config "$CONFIG" --outdir "$EXPORT"

echo "== verify against server access log =="
"$PYTHON" scripts/verify_demo.py --config "$CONFIG" --log "$LOG" --export "$EXPORT"
