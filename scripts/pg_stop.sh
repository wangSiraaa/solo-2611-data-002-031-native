#!/usr/bin/env bash
# Stop the local ledger PostgreSQL cluster.
set -euo pipefail
PGDATA="${LEDGER_PGDATA:-/tmp/ledger-pgdata}"
if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" stop -m fast
else
  echo "cluster not running at $PGDATA"
fi
