"""Shared pytest fixtures: a live PostgreSQL (PG_DSN) reset to the seed state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
DSN = os.environ.get("PG_DSN", "")


def _pg_available() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3, autocommit=True) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


PG_AVAILABLE = _pg_available()
requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason="PG_DSN not set or not connectable")


@pytest.fixture()
def dsn() -> str:
    return DSN


@pytest.fixture()
def fresh_db(dsn):
    """Reset the schema to exactly the seeded state."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS partition_input CASCADE")
    subprocess.run(
        ["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "fixtures" / "seed.sql")],
        check=True, capture_output=True, text=True,
    )
    return dsn


@pytest.fixture()
def make_config(tmp_path):
    """Write a target.json based on the fixture, with optional overrides."""
    base = json.loads((REPO / "fixtures" / "target.json").read_text())

    def _make(**overrides):
        cfg = dict(base)
        cfg.update(overrides)
        path = tmp_path / "target.json"
        path.write_text(json.dumps(cfg))
        return str(path)

    return _make


def run_tool(command: str, config: str, dsn: str) -> tuple[dict, int, str]:
    """Run ``python -m partition_mover <command>`` as a subprocess.

    Returns (parsed JSON stdout, exit code, stderr text).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "partition_mover", command, "--config", config, "--dsn", dsn],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        doc = {"_unparsed_stdout": proc.stdout}
    return doc, proc.returncode, proc.stderr
