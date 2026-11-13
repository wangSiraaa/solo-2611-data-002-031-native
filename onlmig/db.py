"""连接配置。仅连接本机独立 PostgreSQL，不接触任何真实系统。"""
from __future__ import annotations

import os

import psycopg


def dsn() -> str:
    host = os.environ.get("ONLMIG_PGHOST", "/tmp/onlmig_sock")
    dbname = os.environ.get("ONLMIG_PGDB", "taskdb")
    user = os.environ.get("ONLMIG_PGUSER", "node")
    port = os.environ.get("ONLMIG_PGPORT", "5432")
    return f"host={host} port={port} dbname={dbname} user={user}"


def connect(autocommit: bool = True):
    # autocommit=True：事务边界完全由 engine 用 with conn.transaction() 显式管理，
    # 这样故障注入点可以精确放在“已应用、未提交”的位置。
    # 默认隔离级设为 READ COMMITTED：迁移事务可能跨越源写入提交点，
    # 每条语句都必须能看到最新已提交数据（显式 BEGIN 否则会沿用默认
    # REPEATABLE READ 的事务首条语句快照，导致漏读新提交的行）。
    conn = psycopg.connect(dsn(), autocommit=autocommit)
    # 会话级默认隔离级：后续显式 BEGIN（含 psycopg 的 conn.transaction()）
    # 都按 READ COMMITTED 启动，每条语句看到最新已提交快照。
    # verify 的一致性快照仍可在其事务上显式指定 REPEATABLE READ。
    conn.execute("SET DEFAULT_TRANSACTION_ISOLATION TO 'READ COMMITTED'")
    return conn
