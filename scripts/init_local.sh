#!/usr/bin/env bash
# Initialize a local PostgreSQL instance for the executor, load the fixtures,
# and start the products HTTP fixture. Everything lands in .runtime/ (ignored).
#
# Usage:  bash scripts/init_local.sh          # init db + load data
#         bash scripts/init_local.sh --api    # also start the HTTP fixture
#
# Overridable: PGDATA, PGSOCK, PGDB, PGPORT_API, VENV
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/usr/lib/postgresql/16/bin:${PATH}"

PGDATA="${PGDATA:-$ROOT/.runtime/pgdata}"
# PostgreSQL cannot create a Unix socket on some virtiofs mounts, so default the
# socket to a local /tmp directory while keeping the data dir inside the repo.
PGSOCK="${PGSOCK:-/tmp/pg-task-$(id -un)}"
PGDB="${PGDB:-taskdb}"
PGPORT_API="${PGPORT_API:-8107}"
VENV="${VENV:-$ROOT/.venv}"

start_db() {
  if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    return
  fi
  pg_ctl -D "$PGDATA" -l "$ROOT/.runtime/postgres.log" \
    -o "-k $PGSOCK -c listen_addresses=''" start
}

mkdir -p "$ROOT/.runtime" "$PGSOCK"
chmod 700 "$PGSOCK"

if [ ! -d "$PGDATA" ]; then
  initdb -D "$PGDATA" --auth-local=trust --auth-host=scram-sha-256 >/dev/null
fi

start_db
sleep 1

if ! psql -h "$PGSOCK" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1; then
  createdb -h "$PGSOCK" "$PGDB"
fi
# Re-seed deterministically: this is a disposable runtime database.
psql -h "$PGSOCK" -d "$PGDB" -v ON_ERROR_STOP=1 \
  -c 'DROP SCHEMA IF EXISTS fed_input CASCADE' >/dev/null
psql -h "$PGSOCK" -d "$PGDB" -v ON_ERROR_STOP=1 -f "$ROOT/fixtures/orders.sql" >/dev/null

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" -q install 'psycopg[binary]>=3.1'

echo "PostgreSQL ready."
echo "  DSN: host=$PGSOCK dbname=$PGDB"
echo "  export FEDQUERY_DSN=\"host=$PGSOCK dbname=$PGDB\""

if [ "${1:-}" = "--api" ]; then
  echo "Starting products fixture on port $PGPORT_API ..."
  "$VENV/bin/python" "$ROOT/fixtures/products_api.py" \
    --port "$PGPORT_API" --audit "$ROOT/products-requests.jsonl"
fi
