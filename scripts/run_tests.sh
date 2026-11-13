#!/usr/bin/env bash
# 一键准备并运行全部测试（幂等）。
# 用法: scripts/run_tests.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PY=.venv/bin/python
DSN_TEST="${INCJOIN_TEST_DSN:-postgresql://127.0.0.1:55432/taskdb_test}"

if [ ! -x "$PY" ]; then
    python3.11 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet "psycopg[binary]"
fi

# 数据库未启动时，按 environment/README.md 的方式拉起本机独立实例
if ! "$PY" -c "import psycopg; psycopg.connect('$DSN_TEST').close()" 2>/dev/null; then
    if [ ! -d .runtime/pgdata ]; then
        mkdir -p .runtime
        initdb -D "$PWD/.runtime/pgdata" --auth-local=trust --auth-host=trust
    fi
    pg_ctl -D "$PWD/.runtime/pgdata" -l "$PWD/.runtime/postgres.log" \
        -o "-k /tmp -c listen_addresses=127.0.0.1 -p 55432" start
    sleep 1
fi

INCJOIN_TEST_DSN="$DSN_TEST" "$PY" -m unittest discover -s tests -t . -v
