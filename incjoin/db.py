"""PostgreSQL 连接、建库与迁移管理。

数据库独立于任何外部服务：默认连接本机 127.0.0.1:55432/taskdb，
可用 --dsn 或环境变量 INCJOIN_DSN 覆盖。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

DEFAULT_DSN = "postgresql://127.0.0.1:55432/taskdb"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def resolve_dsn(dsn: str | None) -> str:
    return dsn or os.environ.get("INCJOIN_DSN") or DEFAULT_DSN


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(resolve_dsn(dsn), autocommit=autocommit)


def ensure_database(dsn: str | None = None) -> None:
    """目标库不存在时，先连维护库 postgres 把它创建出来。"""
    target = psycopg.conninfo.conninfo_to_dict(resolve_dsn(dsn))
    dbname = target.pop("dbname")
    admin_dsn = psycopg.conninfo.make_conninfo(**target, dbname="postgres")
    try:
        psycopg.connect(resolve_dsn(dsn)).close()
        return  # 目标库已可连接
    except psycopg.OperationalError:
        pass
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname))
            )


def migrate(dsn: str | None = None) -> None:
    """幂等应用 schema.sql；所有命令在打开业务事务前先调用。"""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


def bootstrap(dsn: str | None = None) -> None:
    ensure_database(dsn)
    migrate(dsn)
