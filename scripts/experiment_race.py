#!/usr/bin/env python3
"""Experiment: why the migration locks the whole partition tree.

Reproduces the evidence behind the transaction-boundary design.  A simulated
migration transaction runs while a concurrent writer inserts a January row:

* Scenario A (what the tool does): the migration holds ACCESS EXCLUSIVE on
  the whole partition tree.  The writer blocks *before* tuple routing, then
  re-routes with the fresh partition map after COMMIT and lands in the new
  partition.  No failure, no misplacement.
* Scenario B (the weaker alternative): the migration locks only the DEFAULT
  partition.  The writer routes with the stale map, blocks on the DEFAULT
  partition's lock, and after the migration commits its INSERT fails loudly
  with 23514 (partition-constraint violation re-checked by PostgreSQL).
  Still no silent misplacement, but the application loses the write unless
  it retries.

Usage:  PG_DSN="host=... dbname=..." python3 scripts/experiment_race.py
Resets the schema to the seed state between scenarios.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[1]
DSN = os.environ["PG_DSN"]

MIGRATION_SQL = """
CREATE TABLE partition_input.events_jan (LIKE partition_input.events INCLUDING ALL);
ALTER TABLE partition_input.events_jan ADD CONSTRAINT pmv_range_chk
  CHECK (occurred_at >= TIMESTAMPTZ '2026-01-01 00:00:00+00:00'
     AND occurred_at <  TIMESTAMPTZ '2026-02-01 00:00:00+00:00');
WITH moved AS (
  DELETE FROM partition_input.bucket_misc
  WHERE occurred_at >= TIMESTAMPTZ '2026-01-01 00:00:00+00:00'
    AND occurred_at <  TIMESTAMPTZ '2026-02-01 00:00:00+00:00'
  RETURNING *)
INSERT INTO partition_input.events_jan (id, occurred_at, payload)
  SELECT id, occurred_at, payload FROM moved;
ALTER TABLE partition_input.events ATTACH PARTITION partition_input.events_jan
  FOR VALUES FROM ('2026-01-01 00:00:00+00:00') TO ('2026-02-01 00:00:00+00:00');
"""


def reset_schema():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS partition_input CASCADE")
        conn.execute((REPO / "fixtures" / "seed.sql").read_text())


def writer(result: dict, start_evt: threading.Event):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '10s'")
        start_evt.wait()
        t0 = time.monotonic()
        try:
            conn.execute(
                "INSERT INTO partition_input.events VALUES (%s, %s, %s)",
                (9000, "2026-01-15 00:00:00+00", "race-probe"),
            )
            result["status"] = "committed"
        except psycopg.Error as exc:
            result.update(status="failed", sqlstate=exc.sqlstate,
                          message=str(exc).splitlines()[0])
        result["blocked_for_s"] = round(time.monotonic() - t0, 3)


def run_scenario(name: str, lock_sql: str):
    reset_schema()
    result, start_evt = {}, threading.Event()
    thread = threading.Thread(target=writer, args=(result, start_evt))
    thread.start()
    with psycopg.connect(DSN) as mig, mig.transaction():
        mig.execute("SET LOCAL lock_timeout = '2s'")
        mig.execute(lock_sql)
        start_evt.set()          # writer starts and blocks on our lock
        time.sleep(1.0)
        for stmt in (s for s in MIGRATION_SQL.split(";") if s.strip()):
            mig.execute(stmt)
        # COMMIT on block exit releases the lock; the writer proceeds.
    thread.join()
    with psycopg.connect(DSN, autocommit=True) as conn:
        placement = conn.execute(
            "SELECT tableoid::regclass::text FROM partition_input.events WHERE id = 9000"
        ).fetchall()
        stray = conn.execute(
            "SELECT count(*) FROM partition_input.bucket_misc "
            "WHERE occurred_at >= TIMESTAMPTZ '2026-01-01 00:00:00+00:00' "
            "  AND occurred_at <  TIMESTAMPTZ '2026-02-01 00:00:00+00:00'"
        ).fetchone()[0]
    print(json.dumps({
        "scenario": name,
        "writer": result,
        "probe_row_partition": placement[0][0] if placement else None,
        "january_rows_left_in_default": stray,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    run_scenario("A: ACCESS EXCLUSIVE on the whole partition tree (tool's choice)",
                 "LOCK TABLE partition_input.events IN ACCESS EXCLUSIVE MODE")
    run_scenario("B: ACCESS EXCLUSIVE on the DEFAULT partition only (weaker)",
                 "LOCK TABLE partition_input.bucket_misc IN ACCESS EXCLUSIVE MODE")
