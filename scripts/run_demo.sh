#!/usr/bin/env bash
# 一键可重复演示：自动拉起数据库与网关，执行全部并发/取消/断连/背压/核对/停机验收。
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
bash scripts/init_db.sh
exec .venv/bin/python -m qgateway.cli demo --pool-size 3 --wait-queue-limit 2 --port 8080