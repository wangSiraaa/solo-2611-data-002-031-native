"""Shared pytest fixtures: a freshly migrated test database per test."""
import os

import pytest

from schedc import store

TEST_DSN = os.environ.get(
    "SCHEDC_TEST_DSN",
    "postgresql://schedc_admin@localhost:54329/schedc_test")


def _db_ok() -> bool:
    try:
        conn = store.connect(TEST_DSN)
        conn.close()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_ok()


@pytest.fixture()
def conn():
    c = store.connect(TEST_DSN)
    with c.transaction():
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    store.migrate(c)
    yield c
    c.close()
