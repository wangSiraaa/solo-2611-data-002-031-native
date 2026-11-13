"""Pytest fixtures: fresh schema per test and seeded synthetic accounts."""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ledger.ledger import load_accounts, migrate
from tests.helpers import ACCOUNTS, TEST_DSN


def _ensure_database() -> None:
    params = conninfo.conninfo_to_dict(TEST_DSN)
    dbname = params.get("dbname", "ledger_itest")
    admin = dict(params)
    admin["dbname"] = "postgres"
    with psycopg.connect(**admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname))
            )


@pytest.fixture(autouse=True)
def fresh_schema():
    """Every test starts from an empty schema applied from migrations."""
    _ensure_database()
    migrate(TEST_DSN, reset=True)
    yield


@pytest.fixture(autouse=True)
def accounts(fresh_schema):
    """Seed the synthetic account set for every test."""
    with psycopg.connect(TEST_DSN) as conn:
        load_accounts(conn, ACCOUNTS)
    return ACCOUNTS
