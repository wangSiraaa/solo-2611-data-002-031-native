#!/usr/bin/env bash
# Create/start a private local PostgreSQL 16 cluster and databases.
#
# The cluster is placed under /tmp (a local filesystem), NOT under the repo
# directory: this workspace may live on a shared/virtiofs mount where PostgreSQL
# cannot chmod its data files.  Everything here is throwaway dev state.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
PGDATA="${PGDATA:-/tmp/supplier-pgdata}"
PGSOCK="${PGSOCK:-/tmp/supplier-pg-sock}"
PGLOG="${PGLOG:-/tmp/supplier-pg.log}"
PGPORT="${PGPORT:-5432}"

if [[ ! -x "$PY" ]]; then
  echo "creating virtualenv at $ROOT/.venv" >&2
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install --quiet --upgrade pip
  "$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"
  PY="$ROOT/.venv/bin/python"
fi

mkdir -p "$PGSOCK"

if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  echo "initializing cluster at $PGDATA" >&2
  initdb -D "$PGDATA" --auth-local=trust --auth-host=scram-sha-256 >/dev/null
fi

if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  echo "starting PostgreSQL on socket $PGSOCK" >&2
  pg_ctl -D "$PGDATA" -l "$PGLOG" \
    -o "-k $PGSOCK -p $PGPORT -c listen_addresses=''" start
fi

ready() { pg_isready -h "$PGSOCK" -p "$PGPORT" >/dev/null 2>&1; }
for _ in $(seq 1 50); do ready && break && sleep 0.2; done
pg_isready -h "$PGSOCK" -p "$PGPORT"

for db in supplier_migrate supplier_test; do
  if ! psql -h "$PGSOCK" -p "$PGPORT" -d postgres -tAc \
       "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    createdb -h "$PGSOCK" -p "$PGPORT" "$db"
  fi
done

export SUPPLIER_DB_DSN="dbname=supplier_migrate host=$PGSOCK port=$PGPORT"
export SUPPLIER_TEST_DSN="dbname=supplier_test host=$PGSOCK port=$PGPORT"

"$PY" -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" init-db
echo "ready: $SUPPLIER_DB_DSN"
