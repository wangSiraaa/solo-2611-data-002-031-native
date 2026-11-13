#!/usr/bin/env bash
# 启动查询网关（默认池=3，等待队列=2）。
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m qgateway.cli serve \
  --pool-size "${POOL_SIZE:-3}" \
  --wait-queue-limit "${WAIT_QUEUE_LIMIT:-2}" \
  --frame-queue-limit "${FRAME_QUEUE_LIMIT:-4}" \
  --fetch-block-rows "${FETCH_BLOCK_ROWS:-200}" \
  --port "${PORT:-8080}"
