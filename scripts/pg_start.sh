#!/usr/bin/env bash
# Start (or create) the local, single-user PostgreSQL 16 cluster used by the
# ledger.  The data directory lives outside the repository by default because
# some container filesystems (FUSE/network mounts) do not support the file
# permission semantics PostgreSQL needs.  Override with LEDGER_PGDATA/PGHOST.
set -euo pipefail

PGDATA="${LEDGER_PGDATA:-/tmp/ledger-pgdata}"
PGHOST="${LEDGER_PGHOST:-/tmp/ledger-pg-sock}"
PGPORT="${LEDGER_PGPORT:-55432}"
DBNAME="${LEDGER_PGDATABASE:-ledger}"

mkdir -p "$PGHOST"
chmod 700 "$PGHOST"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "initializing cluster at $PGDATA"
  initdb -D "$PGDATA" --auth-local=trust --auth-host=scram-sha-256 \
         --no-locale --encoding=UTF8
fi

if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  echo "cluster already running at $PGDATA"
else
  pg_ctl -D "$PGDATA" -l /tmp/ledger-pg.log \
    -o "-k $PGHOST -c listen_addresses='' -p $PGPORT" start
fi

until psql -h "$PGHOST" -p "$PGPORT" -d postgres -Atc 'select 1' >/dev/null 2>&1; do
  sleep 0.2
done
psql -h "$PGHOST" -p "$PGPORT" -d postgres -Atc \
  "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 \
  || createdb -h "$PGHOST" -p "$PGPORT" "$DBNAME"

echo "ready: host=$PGHOST port=$PGPORT db=$DBNAME"
