"""PostgreSQL 连接与迁移管理。"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql"

# 本机默认连接：Unix socket 在 /tmp/pg-task，端口 55432，库 taskdb，trust 认证。
DEFAULT_DSN = "host=/tmp/pg-task port=55432 dbname=taskdb user=node"


def resolve_dsn(dsn: str | None = None) -> str:
    """DSN 优先级：显式参数 > 环境变量 DEPGRAPH_DSN > 本机默认值。"""
    return dsn or os.environ.get("DEPGRAPH_DSN") or DEFAULT_DSN


def connect(dsn: str | None = None) -> psycopg.Connection:
    conn = psycopg.connect(resolve_dsn(dsn), autocommit=False)
    return conn


def ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied_versions(conn: psycopg.Connection) -> set[str]:
    ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def run_migrations(conn: psycopg.Connection) -> list[str]:
    """按文件名顺序执行尚未应用的迁移，在同一事务内登记。"""
    done = applied_versions(conn)
    newly_applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.name
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
        )
        newly_applied.append(version)
    conn.commit()
    return newly_applied
