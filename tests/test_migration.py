"""End-to-end tests: plan/apply against a live PostgreSQL, including a
concurrent writer, lock-timeout handling, reruns and conflict rejection."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from conftest import REPO, requires_pg, run_tool

pytestmark = requires_pg

UTC = timezone.utc
JAN1 = datetime(2026, 1, 1, tzinfo=UTC)
FEB1 = datetime(2026, 2, 1, tzinfo=UTC)

EXPECTED_PART = {
    "dec": "partition_input.not_named_by_month",
    "jan": "partition_input.events_jan",
    "default": "partition_input.bucket_misc",
}


def _partition_oids(conn):
    rows = conn.execute(
        "SELECT c.oid, n.nspname || '.' || c.relname "
        "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE i.inhparent = 'partition_input.events'::regclass"
    ).fetchall()
    return {name: oid for oid, name in rows}


def _snapshot(conn):
    """Full dataset: {id: occurred_at}."""
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT id, occurred_at FROM partition_input.events").fetchall()
    }


def _placement(conn):
    """Full dataset with physical partition oids: {id: (occurred_at, tableoid)}."""
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT id, occurred_at, tableoid FROM partition_input.events"
        ).fetchall()
    }


def _expected_partition_name(ts):
    if ts < JAN1:
        return EXPECTED_PART["dec"]
    if ts < FEB1:
        return EXPECTED_PART["jan"]
    return EXPECTED_PART["default"]


def _assert_attribution(conn):
    """Every row must physically live in the partition its occurred_at implies."""
    oids = _partition_oids(conn)
    by_oid = {v: k for k, v in oids.items()}
    for _id, (ts, tableoid) in _placement(conn).items():
        assert by_oid[tableoid] == _expected_partition_name(ts), (
            f"row id={_id} ts={ts} in {by_oid[tableoid]}"
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_plan_reports_month_bounds_conflicts_rows_and_ddl(self, fresh_db, make_config):
        doc, code, _ = run_tool("plan", make_config(), fresh_db)
        assert code == 0
        assert doc["status"] == "ready"
        assert doc["month"] == "2026-01"
        # Range comes from the configured month, not from any table name.
        assert doc["target_range"] == {
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-02-01T00:00:00+00:00",
        }
        assert doc["default_partition"]["name"] == "partition_input.bucket_misc"
        # Bounds are read from the catalog even though the December
        # partition's name says nothing about them.
        dec = next(p for p in doc["existing_partitions"]
                   if p["name"] == "partition_input.not_named_by_month")
        assert dec["range"] == ["2025-12-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]
        assert doc["conflicting_partitions"] == []
        assert doc["estimated_rows_to_move"] == 3
        actions = "\n".join(doc["planned_actions"])
        assert "LOCK TABLE" in actions
        assert "CREATE TABLE" in actions and "PARTITION OF" not in actions
        assert "ATTACH PARTITION" in actions
        assert "FOR VALUES FROM ('2026-01-01 00:00:00+00:00') TO ('2026-02-01 00:00:00+00:00')" in actions


# ---------------------------------------------------------------------------
# apply: basic migration, boundaries, idempotency
# ---------------------------------------------------------------------------

class TestApply:
    def test_migration_moves_exactly_the_month(self, fresh_db, make_config):
        doc, code, _ = run_tool("apply", make_config(), fresh_db)
        assert code == 0, doc
        assert doc["status"] == "migrated"
        assert doc["moved_rows"] == 3
        assert doc["lock_wait_ms"] is not None
        assert doc["new_partition"]["range"] == [
            "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00",
        ]
        assert doc["verification"]["ok"]

        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("SET TIME ZONE 'UTC'")
            placement = _placement(conn)
            oids = _partition_oids(conn)
            # Boundary rows: month start and the microsecond month end moved;
            # next month's midnight and February stayed in DEFAULT.
            assert placement[2][0] == JAN1
            assert placement[2][1] == oids[EXPECTED_PART["jan"]]
            assert placement[4][0] == datetime(2026, 1, 31, 23, 59, 59, 999999, UTC)
            assert placement[4][1] == oids[EXPECTED_PART["jan"]]
            assert placement[5][0] == FEB1
            assert placement[5][1] == oids[EXPECTED_PART["default"]]
            assert placement[1][1] == oids[EXPECTED_PART["dec"]]
            assert set(placement) == {1, 2, 3, 4, 5, 6}  # nothing lost, nothing duplicated
            _assert_attribution(conn)
            # The primary key survived on the new partition.
            pk = conn.execute(
                "SELECT count(*) FROM pg_index WHERE indrelid = %s AND indisprimary",
                (oids[EXPECTED_PART["jan"]],),
            ).fetchone()[0]
            assert pk == 1
            # The temporary exclusion CHECK on DEFAULT is gone afterwards.
            leftover = conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid = %s AND conname LIKE 'pmv_%%'",
                (oids[EXPECTED_PART["default"]],),
            ).fetchone()[0]
            assert leftover == 0

    def test_rerun_is_idempotent(self, fresh_db, make_config):
        cfg = make_config()
        first, code1, _ = run_tool("apply", cfg, fresh_db)
        assert code1 == 0 and first["status"] == "migrated"
        second, code2, _ = run_tool("apply", cfg, fresh_db)
        assert code2 == 0
        assert second["status"] == "already_done"
        assert second["verification"]["ok"]
        # A plan after the fact also reports the finished state.
        plan, code3, _ = run_tool("plan", cfg, fresh_db)
        assert code3 == 0 and plan["status"] == "already_done"
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            assert len(_snapshot(conn)) == 6
            _assert_attribution(conn)

    def test_december_is_recognized_by_bounds_not_by_name(self, fresh_db, make_config):
        # The December partition is deliberately named "not_named_by_month";
        # the tool must detect the covered month from catalog bounds alone.
        cfg = make_config(month="2025-12", new_partition="partition_input.events_dec")
        plan, code, _ = run_tool("plan", cfg, fresh_db)
        assert code == 0 and plan["status"] == "already_done"
        assert plan["matching_partition"]["name"] == "partition_input.not_named_by_month"
        doc, code, _ = run_tool("apply", cfg, fresh_db)
        assert code == 0 and doc["status"] == "already_done"

    def test_empty_month_migrates_zero_rows(self, fresh_db, make_config):
        # No March rows anywhere: the tool still attaches the (empty)
        # partition so future writes route correctly.
        cfg = make_config(month="2026-03", new_partition="partition_input.events_mar")
        plan, code, _ = run_tool("plan", cfg, fresh_db)
        assert code == 0 and plan["status"] == "ready"
        assert plan["estimated_rows_to_move"] == 0
        doc, code, _ = run_tool("apply", cfg, fresh_db)
        assert code == 0 and doc["status"] == "migrated" and doc["moved_rows"] == 0
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            bound = conn.execute(
                "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='partition_input' AND c.relname='events_mar'"
            ).fetchone()[0]
            assert bound == (
                "FOR VALUES FROM ('2026-03-01 00:00:00+00') "
                "TO ('2026-04-01 00:00:00+00')"
            )


# ---------------------------------------------------------------------------
# apply with the supplied writer running concurrently
# ---------------------------------------------------------------------------

def _writer_expected_rows(first_id, count):
    return {
        first_id + n: datetime(2026, 1 if n % 2 == 0 else 2, 15, tzinfo=UTC)
        + timedelta(microseconds=n)
        for n in range(count)
    }


class TestConcurrentWriter:
    def test_no_loss_no_duplicates_no_routing_gap(self, fresh_db, make_config, tmp_path):
        first_id, count = 1000, 40
        writer_out = tmp_path / "writer.jsonl"

        with psycopg.connect(fresh_db, autocommit=True) as conn:
            before = _snapshot(conn)
        assert len(before) == 6

        writer = subprocess.Popen(
            [sys.executable, str(REPO / "fixtures" / "writer.py"),
             "--dsn", fresh_db, "--count", str(count), "--first-id", str(first_id),
             "--interval", "0.05"],
            cwd=REPO, stdout=writer_out.open("w"), stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.4)  # let the writer get going; migration lands mid-stream
        doc, code, stderr = run_tool("apply", make_config(), fresh_db)
        writer.wait(timeout=60)

        assert code == 0, (doc, stderr)
        assert doc["status"] == "migrated"

        results = [json.loads(line) for line in writer_out.read_text().splitlines() if line.strip()]
        committed = {r["id"] for r in results if r["status"] == "committed"}
        failed = {r["id"]: r for r in results if r["status"] == "failed"}
        # Bounded blocking: the migration lock is held for milliseconds, far
        # below the writer's 3s statement_timeout, so no write may fail.
        assert failed == {}, f"writer saw failures: {failed}"
        assert committed == {first_id + n for n in range(count)}

        expected = dict(before)
        expected.update({i: ts for i, ts in _writer_expected_rows(first_id, count).items()
                         if i in committed})

        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("SET TIME ZONE 'UTC'")
            after = _snapshot(conn)
            # Exactly the seed rows plus every row the writer committed:
            # none lost, none duplicated, none extra.
            assert after == expected
            _assert_attribution(conn)
            # The writer's January rows landed in the new partition no matter
            # whether they committed before or after the migration commit.
            oids = _partition_oids(conn)
            jan_writer_rows = [i for i in committed if i % 2 == 0]
            assert jan_writer_rows, "expected some concurrent January rows"
            placement = _placement(conn)
            for i in jan_writer_rows:
                assert placement[i][1] == oids[EXPECTED_PART["jan"]]
            # Report is internally consistent with the final state.
            total = conn.execute("SELECT count(*) FROM partition_input.events").fetchone()[0]
            assert total == len(expected)
            jan_in_default = conn.execute(
                "SELECT count(*) FROM partition_input.bucket_misc "
                "WHERE occurred_at >= %s AND occurred_at < %s", (JAN1, FEB1),
            ).fetchone()[0]
            assert jan_in_default == 0
        # moved_rows = 3 seed rows + whatever January rows the writer had
        # committed before the migration lock was taken.
        assert doc["moved_rows"] >= 3
        assert doc["moved_rows"] <= 3 + count // 2


# ---------------------------------------------------------------------------
# lock contention: bounded blocking, clean rollback, safe retry
# ---------------------------------------------------------------------------

class TestLockTimeout:
    def test_lock_timeout_aborts_cleanly_and_retry_succeeds(self, fresh_db, make_config):
        cfg = make_config(lock_timeout_ms=300, max_attempts=2, retry_backoff_ms=100)
        holder = psycopg.connect(fresh_db)  # holds the tree lock
        holder.execute("LOCK TABLE partition_input.events IN ACCESS EXCLUSIVE MODE")
        try:
            doc, code, _ = run_tool("apply", cfg, fresh_db)
        finally:
            holder.rollback()
            holder.close()

        assert code == 3
        assert doc["status"] == "lock_failed"
        assert len(doc["attempts"]) == 2
        assert all(a["result"] == "lock_timeout" and a["sqlstate"] == "55P03"
                   for a in doc["attempts"])

        with psycopg.connect(fresh_db, autocommit=True) as conn:
            # Nothing changed: no new relation, all January rows still in DEFAULT.
            assert conn.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='partition_input' AND c.relname='events_jan'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM partition_input.bucket_misc "
                "WHERE occurred_at >= %s AND occurred_at < %s", (JAN1, FEB1),
            ).fetchone()[0] == 3

        # Once the blocker is gone the very same command succeeds.
        doc2, code2, _ = run_tool("apply", cfg, fresh_db)
        assert code2 == 0 and doc2["status"] == "migrated"

    def test_plan_reading_is_bounded_by_lock_timeout(self, fresh_db, make_config):
        cfg = make_config(lock_timeout_ms=300)
        holder = psycopg.connect(fresh_db)
        holder.execute("LOCK TABLE partition_input.events IN ACCESS EXCLUSIVE MODE")
        try:
            doc, code, _ = run_tool("plan", cfg, fresh_db)
        finally:
            holder.rollback()
            holder.close()
        assert code == 1
        assert doc["status"] == "error"
        assert "lock_timeout" in doc["reason"]


# ---------------------------------------------------------------------------
# plan/apply staleness and foreign objects
# ---------------------------------------------------------------------------

class TestConflictsAndForeignObjects:
    def test_conflicting_partition_created_after_plan_is_rejected(self, fresh_db, make_config):
        cfg = make_config()
        plan, code, _ = run_tool("plan", cfg, fresh_db)
        assert code == 0 and plan["status"] == "ready"

        # Someone else creates an overlapping partition before apply runs.
        # (The chosen sub-range contains no seed rows, so PostgreSQL allows it.)
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE partition_input.sneaky PARTITION OF partition_input.events "
                "FOR VALUES FROM ('2026-01-10') TO ('2026-01-20')"
            )
        try:
            plan2, _, _ = run_tool("plan", cfg, fresh_db)
            assert plan2["status"] == "conflict"
            assert "partition_input.sneaky" in [p["name"] for p in plan2["conflicting_partitions"]]

            doc, code, _ = run_tool("apply", cfg, fresh_db)
            assert code == 2
            assert doc["status"] == "refused"
            assert "partition_input.sneaky" in doc["conflicting_partitions"]
            with psycopg.connect(fresh_db, autocommit=True) as conn:
                # The stale plan was not executed.
                assert conn.execute(
                    "SELECT count(*) FROM partition_input.bucket_misc "
                    "WHERE occurred_at >= %s AND occurred_at < %s", (JAN1, FEB1),
                ).fetchone()[0] == 3
        finally:
            with psycopg.connect(fresh_db, autocommit=True) as conn:
                conn.execute("DROP TABLE partition_input.sneaky")

        # After the conflict is resolved the same config migrates cleanly.
        doc, code, _ = run_tool("apply", cfg, fresh_db)
        assert code == 0 and doc["status"] == "migrated"

    def test_foreign_table_with_target_name_is_never_dropped(self, fresh_db, make_config):
        cfg = make_config()
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("CREATE TABLE partition_input.events_jan (id bigint)")
            conn.execute("INSERT INTO partition_input.events_jan VALUES (42)")
        doc, code, _ = run_tool("apply", cfg, fresh_db)
        assert code == 2
        assert doc["status"] == "refused"
        assert doc["plan_status"] == "name_taken"
        assert doc["new_partition_name_taken"]["looks_like_tool_leftover"] is False

        with psycopg.connect(fresh_db, autocommit=True) as conn:
            # The foreign table is untouched: still a plain table, row intact.
            row = conn.execute(
                "SELECT c.relkind, c.relispartition FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='partition_input' AND c.relname='events_jan'"
            ).fetchone()
            assert row == ("r", False)
            assert conn.execute("SELECT count(*) FROM partition_input.events_jan").fetchone()[0] == 1
            conn.execute("DROP TABLE partition_input.events_jan")

        doc, code, _ = run_tool("apply", cfg, fresh_db)
        assert code == 0 and doc["status"] == "migrated"

    def test_missing_default_partition_is_reported(self, fresh_db, make_config):
        # Premise broken: no DEFAULT partition to migrate rows out of.
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("DROP TABLE partition_input.bucket_misc")
        plan, code, _ = run_tool("plan", make_config(), fresh_db)
        assert code == 0 and plan["status"] == "no_default"
        doc, code, _ = run_tool("apply", make_config(), fresh_db)
        assert code == 2 and doc["status"] == "refused"
