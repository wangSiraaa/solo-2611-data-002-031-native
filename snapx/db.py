"""Database connection helpers.

Connection parameters come from SNAPX_PG* environment variables, with
defaults matching `snapx dbstart` (local standalone cluster on 127.0.0.1:54329).
"""

import os

import psycopg

DEFAULT_PORT = "54329"
DEFAULT_DB = "snapx"
DEFAULT_USER = "postgres"


def conninfo() -> str:
    host = os.environ.get("SNAPX_PGHOST", "127.0.0.1")
    port = os.environ.get("SNAPX_PGPORT", DEFAULT_PORT)
    dbname = os.environ.get("SNAPX_PGDB", DEFAULT_DB)
    user = os.environ.get("SNAPX_PGUSER", DEFAULT_USER)
    return f"host={host} port={port} dbname={dbname} user={user}"


def connect(autocommit: bool = True) -> psycopg.Connection:
    # autocommit=True + explicit BEGIN gives full control over transaction
    # boundaries and isolation level (required for snapshot export/import).
    return psycopg.connect(conninfo(), autocommit=autocommit)


def current_epoch() -> int:
    with connect() as conn:
        row = conn.execute("SELECT epoch FROM dataset_epoch WHERE id = 1").fetchone()
        return int(row[0])
