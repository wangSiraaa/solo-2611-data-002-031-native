"""Per-request database access and SQL counting.

Every HTTP request opens its own connection and gets a fresh
RequestContext, so nothing (connections, loaders, caches) leaks
across requests: the next request always sees committed changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

RUNTIME_DIR = Path(__file__).resolve().parent.parent / ".runtime"

# The workspace filesystem (virtiofs) cannot host Unix sockets, so the
# server is started with -k /tmp/pgsock; override with PG_DSN if needed.
DEFAULT_DSN = "host=/tmp/pgsock dbname=taskdb"


def dsn() -> str:
    return os.environ.get("PG_DSN", DEFAULT_DSN)


async def connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        dsn(), row_factory=dict_row, autocommit=True
    )


@dataclass
class RequestContext:
    """Lives exactly as long as one GraphQL request."""

    conn: psycopg.AsyncConnection
    sql_log: list[dict[str, Any]] = field(default_factory=list)
    orders_loader: Any = None  # set in __post_init__

    def __post_init__(self) -> None:
        from .loaders import BatchLoader, batch_load_orders

        self.orders_loader = BatchLoader(lambda keys: batch_load_orders(self, keys))

    async def fetch(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        # Record every statement so a request can report exactly how many
        # SQL queries it caused (exposed via extensions.sqlCount).
        self.sql_log.append(
            {
                "sql": " ".join(sql.split()),
                "params": [list(p) if isinstance(p, list) else p for p in params],
            }
        )
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()
