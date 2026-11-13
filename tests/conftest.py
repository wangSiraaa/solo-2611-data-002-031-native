"""Shared test harness: fresh database schema + demo data per test."""

from __future__ import annotations

import os
import sys

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supplier_migrate import db  # noqa: E402
from supplier_migrate.seed import DEFAULT_SUPPLIERS, seed, reset  # noqa: E402

TEST_DSN = os.environ.get(
    "SUPPLIER_TEST_DSN",
    os.environ.get("SUPPLIER_DB_DSN", "dbname=supplier_test host=/tmp/supplier-pg-sock"),
)


@pytest.fixture(scope="session", autouse=True)
def _database():
    admin = f"{TEST_DSN} dbname=postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        present = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", ("supplier_test",)
        ).fetchone()
        if not present:
            conn.execute('CREATE DATABASE "supplier_test"')
    db.init_db(TEST_DSN)


@pytest.fixture()
def conn():
    with db.connect(TEST_DSN) as c:
        yield c


@pytest.fixture()
def fresh(conn):
    reset(conn)
    seed(conn)
    conn.commit()
    return conn


@pytest.fixture()
def folder(tmp_path):
    return tmp_path


def write_csv(tmp_path, name: str, rows, header="old_code,new_code") -> str:
    path = tmp_path / name
    text = header + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8")
    return str(path)
