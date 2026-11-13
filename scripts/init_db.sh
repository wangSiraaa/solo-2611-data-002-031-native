#!/usr/bin/env bash
# 初始化专用本地 PostgreSQL 16 实例与合成数据（可重复执行）。
# 所有产物位于 .runtime/，已被 .gitignore 忽略。
set -euo pipefail

cd "$(dirname "$0")/.."
RUNTIME="$PWD/.runtime"
# 数据目录与 socket 均放本地磁盘（virtiofs 等网络挂载不支持二者）
PGDATA="${QGATEWAY_PGDATA:-/tmp/qgateway-pgdata}"
SOCKET_DIR="$PGDATA"
SEED_ROWS="${SEED_ROWS:-120000}"

mkdir -p "$RUNTIME" "$PGDATA"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "[initdb] 创建数据目录 $PGDATA"
  initdb -D "$PGDATA" --auth-local=trust --auth-host=scram-sha-256
fi

if [ ! -S "$SOCKET_DIR/.s.PGSQL.5432" ]; then
  echo "[postgres] 启动本地专用实例（仅 Unix socket，不监听 TCP）"
  pg_ctl -D "$PGDATA" -l "$RUNTIME/postgres.log" \
    -o "-k $SOCKET_DIR -c listen_addresses='' -c fsync=off -c synchronous_commit=off" \
    -w start
fi

if ! psql -h "$SOCKET_DIR" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='taskdb'" | grep -q 1; then
  echo "[createdb] 创建 taskdb"
  createdb -h "$SOCKET_DIR" taskdb
fi

echo "[dbinit] 生成 schema 与 $SEED_ROWS 行合成数据"
.venv/bin/python -m qgateway.dbinit --seed-rows "$SEED_ROWS"

echo "[ok] 数据库就绪，DSN: host=$SOCKET_DIR user=$(whoami) dbname=taskdb"
