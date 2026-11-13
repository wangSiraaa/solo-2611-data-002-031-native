#!/usr/bin/env bash
# Start the synthetic legacy report site locally.
#
#   scripts/run_site.sh [variant] [port] [mode]
#
#   variant: a | b        (two structurally different page sets)
#   port:    integer      (default 8765)
#   mode:    unscoped | scoped
#            scoped = session invalidation really deletes the server session;
#            unscoped = convenience default used by the happy-path demo.
#
# Database and logs land in .runtime/ (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT="${1:-a}"
PORT="${2:-8765}"
MODE="${3:-unscoped}"

mkdir -p .runtime/logs
echo "Starting site variant=$VARIANT port=$PORT mode=$MODE"
echo "  open http://127.0.0.1:$PORT/  (the login link is on this page)"
echo "  truth export: http://127.0.0.1:$PORT/__truth.csv"
python3 -m report_site.server \
  --variant "$VARIANT" \
  --db ".runtime/site_${VARIANT}.sqlite" \
  --port "$PORT" \
  --mode "$MODE"
