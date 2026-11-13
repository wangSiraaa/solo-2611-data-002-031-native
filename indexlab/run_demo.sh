#!/usr/bin/env bash
# indexlab 端到端复现脚本（约 3-5 分钟）
# 前置：PostgreSQL 16 已启动，连接参数见环境变量 PGHOST/PGPORT/PGUSER
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-/workspace/venv/bin/python}

$PY -m indexlab cleanup --all || true   # 幂等：先清掉旧实验对象
rm -rf archive/ixl
$PY -m indexlab init
$PY -m indexlab gen-workload
$PY -m indexlab candidates
$PY -m indexlab run
$PY -m indexlab report
echo
echo "== 归档: archive/ixl/ (report.md / plans / measure.json) =="
