"""pytest 夹具: 使用独立测试数据库 inventory_test, 每个测试前清空业务表."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证 `import inventory_tool` 可用 (项目根 = 本文件上两级)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from inventory_tool import repository as repo  # noqa: E402
from inventory_tool.config import resolve_dsn  # noqa: E402
from inventory_tool.service import connect  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    conn = connect(resolve_dsn())
    try:
        repo.run_migrations(conn)
    finally:
        conn.close()


@pytest.fixture()
def dsn() -> str:
    return resolve_dsn()


@pytest.fixture()
def db(dsn):
    """已清空业务表的连接, 测试结束关闭."""
    conn = connect(dsn)
    repo.reset_all(conn)
    yield conn
    conn.close()


def seed_stock(conn, mapping: dict[str, int]) -> None:
    repo.load_initial_stock(conn, list(mapping.items()))

