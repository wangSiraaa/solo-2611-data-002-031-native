#!/usr/bin/env bash
# One-shot end-to-end demo through the real CLI, then an independent raw-SQL
# verification of the same database.  Only synthetic data is used.
set -euo pipefail
cd "$(dirname "$0")/.."

export LEDGER_PGHOST="${LEDGER_PGHOST:-/tmp/ledger-pg-sock}"
export LEDGER_PGPORT="${LEDGER_PGPORT:-55432}"
export LEDGER_PGDATABASE="${LEDGER_PGDATABASE:-ledger}"
PY=".venv/bin/python"

"$PY" -m ledger init-db
# Start from a clean ledger so the demo is repeatable on the same database.
"$PY" -m ledger reset-db --yes >/dev/null
OUT=".runtime/demo"
mkdir -p "$OUT"
"$PY" -m ledger demo-data --period 2026-03 --out-dir "$OUT"
"$PY" -m ledger load-accounts "$OUT/accounts.csv"
for f in "$OUT"/entries/*.json; do
  "$PY" -m ledger post --entry-file "$f" >/dev/null
done
"$PY" -m ledger reverse --reversal-json \
  '{"entry_id":"demo-rev-004","reverses_entry_id":"demo-004","entry_date":"2026-04-02"}' \
  >/dev/null
"$PY" -m ledger close --period 2026-03 >/dev/null
"$PY" -m ledger trial-balance --period 2026-03
"$PY" -m ledger audit
