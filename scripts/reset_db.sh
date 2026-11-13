#!/usr/bin/env bash
# Reset the schema to exactly the seeded fixture state.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PG_DSN:?set PG_DSN first, e.g. export PG_DSN=\"host=/tmp/pgsock_$(id -un) dbname=taskdb\"}"
psql "$PG_DSN" -qAtc "DROP SCHEMA IF EXISTS partition_input CASCADE"
psql "$PG_DSN" -q -v ON_ERROR_STOP=1 -f fixtures/seed.sql
echo "schema reset, seed loaded"
