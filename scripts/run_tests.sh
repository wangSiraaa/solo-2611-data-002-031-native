#!/usr/bin/env bash
# Run the full test suite against a disposable database, creating it if absent.
set -euo pipefail
cd "$(dirname "$0")/.."

export LEDGER_TEST_DSN="${LEDGER_TEST_DSN:-host=/tmp/ledger-pg-sock port=55432 dbname=ledger_itest user=$(id -un)}"
DBNAME="$(printf '%s' "$LEDGER_TEST_DSN" | sed -n 's/.*dbname=\([^ ]*\).*/\1/p')"
HOST="$(printf '%s' "$LEDGER_TEST_DSN" | sed -n 's/.*host=\([^ ]*\).*/\1/p')"
PORT="$(printf '%s' "$LEDGER_TEST_DSN" | sed -n 's/.*port=\([^ ]*\).*/\1/p')"
if ! psql -h "$HOST" -p "$PORT" -d "$DBNAME" -Atc 'select 1' >/dev/null 2>&1; then
  createdb -h "$HOST" -p "$PORT" "$DBNAME"
fi

".venv/bin/python" -m pytest -q "$@"
