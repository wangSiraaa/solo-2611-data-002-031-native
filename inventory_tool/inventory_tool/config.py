"""数据库连接配置.

连接参数解析顺序:
1. 环境变量 INV_DB_DSN (完整 libpq DSN, 优先级最高)
2. PGHOST / PGPORT / PGUSER / PGDATABASE
3. 默认值: 本机用户自建集群的 Unix socket 目录 + 独立测试库 inventory_test
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_NAME = "inventory_test"
DEFAULT_SOCKET_DIR = str(Path.home() / "inventory-tool-runtime" / "pgsocket")


def resolve_dsn() -> str:
    """返回 libpq 可接受的 DSN 字符串."""
    explicit = os.environ.get("INV_DB_DSN")
    if explicit:
        return explicit

    host = os.environ.get("PGHOST", DEFAULT_SOCKET_DIR)
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER") or os.environ.get("USER", "node")
    dbname = os.environ.get("PGDATABASE", DEFAULT_DB_NAME)
    return f"host={host} port={port} user={user} dbname={dbname}"
