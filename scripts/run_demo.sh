#!/usr/bin/env bash
# 端到端演示：在同一测试库中 导出 -> 事务应用 -> 验证（含冒烟，自动回滚）。
# 用法：scripts/run_demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."

DSN="${DSN:-host=/tmp/pg_task_sock dbname=taskdb user=node}"
PSQL="${PSQL:-psql}"
PSQL_HOST="${PSQL_HOST:-/tmp/pg_task_sock}"
PSQL_DB="${PSQL_DB:-taskdb}"
TARGET_SCHEMA="$(${PYTHON:-./.venv/bin/python} -c 'import json;print(json.load(open("fixtures/request.json"))["target_schema"])')"

echo "==> 清理可能残留的目标 schema（仅演示用）"
"$PSQL" -h "$PSQL_HOST" -d "$PSQL_DB" -c "DROP SCHEMA IF EXISTS \"${TARGET_SCHEMA}\" CASCADE" >/dev/null

echo "==> 1) 从 pg_catalog 导出依赖闭包与 DDL"
./.venv/bin/python -m pgview_export --dsn "$DSN" export

echo
echo "==> 2) 在单事务中应用 DDL（失败整体回滚）"
./.venv/bin/python -m pgview_export --dsn "$DSN" apply

echo
echo "==> 3) 结构对比 + 取号/视图/业务字符串冒烟验证"
./.venv/bin/python -m pgview_export --dsn "$DSN" verify

echo
echo "==> 4) 复位目标 schema，交付干净状态（序列副作用一并清除）"
"$PSQL" -h "$PSQL_HOST" -d "$PSQL_DB" -c "DROP SCHEMA IF EXISTS \"${TARGET_SCHEMA}\" CASCADE" >/dev/null
echo "完成。产物见 out/export_manifest.json 与 out/schema_export.sql"
