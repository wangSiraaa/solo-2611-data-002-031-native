#!/usr/bin/env bash
# 初始化本地 PostgreSQL 实例并载入 fixtures/seed.sql。
# 数据目录、socket、日志都放在 .runtime/（已被 .gitignore 忽略）。
set -euo pipefail
cd "$(dirname "$0")/.."

PGDATA="$PWD/.runtime/pgdata"
SOCKDIR="/tmp/pg_task_sock"
DBNAME="taskdb"

if [ ! -d "$PGDATA" ]; then
  initdb -D "$PGDATA" --auth-local=trust --auth-host=scram-sha-256 >.runtime/initdb.log 2>&1
fi
mkdir -p "$SOCKDIR"
# 已在运行则跳过启动
if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" -l "$PWD/.runtime/postgres.log" \
    -o "-k $SOCKDIR -c listen_addresses=''" -w start
fi
createdb -h "$SOCKDIR" "$DBNAME" 2>/dev/null || true
psql -h "$SOCKDIR" -d "$DBNAME" -v ON_ERROR_STOP=1 -f fixtures/seed.sql
echo "seed 已载入：psql -h $SOCKDIR -d $DBNAME"
