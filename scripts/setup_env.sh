#!/usr/bin/env bash
# One-time environment setup: Python venv + dependencies, local PostgreSQL 16
# instance (data in .runtime/, socket in $SOCK_DIR), and the task database.
# Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

RUNTIME_DIR="${RUNTIME_DIR:-$PWD/.runtime}"
# The workspace filesystem may not support Unix sockets; /tmp always does.
SOCK_DIR="${SOCK_DIR:-/tmp/pgsock_$(id -un)}"
DB_NAME="${DB_NAME:-taskdb}"

if [ ! -x .venv/bin/python ]; then
  python3.11 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q "psycopg[binary]" pytest

mkdir -p "$RUNTIME_DIR" "$SOCK_DIR"
if [ ! -s "$RUNTIME_DIR/pgdata/PG_VERSION" ]; then
  initdb -D "$RUNTIME_DIR/pgdata" --auth-local=trust --auth-host=scram-sha-256
fi
if ! pg_ctl -D "$RUNTIME_DIR/pgdata" status >/dev/null 2>&1; then
  pg_ctl -D "$RUNTIME_DIR/pgdata" -l "$RUNTIME_DIR/postgres.log" \
    -o "-k $SOCK_DIR -c listen_addresses=''" start
fi
createdb -h "$SOCK_DIR" "$DB_NAME" 2>/dev/null || true

cat <<EOF

Environment ready.
  export PG_DSN="host=$SOCK_DIR dbname=$DB_NAME"
  scripts/reset_db.sh                 # load fixtures/seed.sql
  scripts/run_demo.sh                 # full reproduction (plan, writer, apply)
  .venv/bin/python -m pytest tests    # test suite (needs PG_DSN)
Stop the database with:
  pg_ctl -D "$RUNTIME_DIR/pgdata" stop
EOF
