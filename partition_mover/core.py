"""Plan and apply logic for the one-time single-month partition migration.

Transaction-boundary overview (see SOLUTION.md for the full discussion):

* ``build_plan`` runs on an autocommit connection and takes no locks.  It is
  a diagnostic snapshot, never trusted by ``apply``.
* ``run_apply`` re-reads the catalog before every attempt, then runs the
  whole migration — lock, in-lock revalidation, table creation, row move,
  DEFAULT-side constraint, ATTACH — in ONE transaction.  PostgreSQL DDL is
  transactional, so any failure (lock timeout, statement timeout, a plan
  that stopped being valid, a crash) rolls the attempt back completely:
  no half-moved rows, no leftover objects, no routing gap.
"""
from __future__ import annotations

import sys
import time

import psycopg

from .bounds import MAXVALUE, MINVALUE, sql_ts_literal
from .catalog import (
    KEY_TYPE_KEYWORD,
    ParentInfo,
    PartitionInfo,
    count_rows,
    count_rows_in_range,
    lookup_relation,
    qi,
    read_parent,
    read_partitions,
)
from .config import Config
from .errors import (
    AlreadyDoneInTxn,
    InTxnVerificationFailed,
    MoverError,
    PlanChanged,
)

#: Provenance marker written as a COMMENT on the new partition.  It lets a
#: later run tell "object this tool created" apart from a foreign table that
#: merely shares the configured name.  The tool never drops either kind.
MARKER_PREFIX = "partition-mover:v1"


def _log(msg: str) -> None:
    print(f"[partition_mover] {msg}", file=sys.stderr, flush=True)


def _fmt_dt(dt) -> str:
    return dt.isoformat()


def _fmt_endpoint(value) -> str:
    if value is MINVALUE:
        return "MINVALUE"
    if value is MAXVALUE:
        return "MAXVALUE"
    return _fmt_dt(value)


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def marker_comment(cfg: Config, start, end) -> str:
    return (
        f"{MARKER_PREFIX} parent={cfg.table_qname} month={cfg.month} "
        f"range=[{_fmt_dt(start)},{_fmt_dt(end)})"
    )


def migration_statements(
    cfg: Config, parent: ParentInfo, default_part: PartitionInfo, start, end
) -> list:
    """The exact statements of the migration transaction, in order.

    The first statement is the tree lock; the rest run only after the
    in-lock revalidation succeeds.
    """
    qparent = qi(cfg.table_schema, cfg.table_name)
    qnew = qi(cfg.new_schema, cfg.new_name)
    qdef = qi(default_part.schema, default_part.name)
    key = qi(parent.key_column)
    cols = ", ".join(qi(c) for c in parent.columns)
    with_tz = parent.key_type == "timestamp with time zone"
    kw = KEY_TYPE_KEYWORD[parent.key_type]
    lo = sql_ts_literal(start, with_tz)
    hi = sql_ts_literal(end, with_tz)
    rng = f"{key} >= {kw} '{lo}' AND {key} < {kw} '{hi}'"
    yyyymm = cfg.month.replace("-", "")
    range_con = qi(f"pmv_range_{yyyymm}")
    excl_con = qi(f"pmv_excl_{yyyymm}")
    marker = marker_comment(cfg, start, end).replace("'", "''")
    return [
        # Lock the whole partition tree (parent + every partition) so no
        # writer can route a row with a stale partition map while the
        # DEFAULT partition's constraint and the new partition's attachment
        # are swapped in.  Acquisition is bounded by lock_timeout.
        ("lock", f"LOCK TABLE {qparent} IN ACCESS EXCLUSIVE MODE"),
        # New partition starts life as a plain standalone table; it is NOT
        # created with PARTITION OF because that would validate against the
        # DEFAULT partition's current rows and fail.
        ("create", f"CREATE TABLE {qnew} (LIKE {qparent} INCLUDING ALL)"),
        ("comment", f"COMMENT ON TABLE {qnew} IS '{marker}'"),
        # Lets ATTACH skip re-validating the new table's rows.
        ("check_new", f"ALTER TABLE {qnew} ADD CONSTRAINT {range_con} CHECK ({rng})"),
        # Move the month's rows.  One statement, one snapshot, same
        # transaction as the ATTACH: rows cannot be lost, duplicated, or
        # visible in neither/both places at any committed point in time.
        (
            "move",
            f"WITH moved AS (DELETE FROM {qdef} WHERE {rng} RETURNING *) "
            f"INSERT INTO {qnew} ({cols}) SELECT {cols} FROM moved",
        ),
        # Prove the DEFAULT partition holds no rows of the month, so ATTACH
        # does not need its own validation scan of DEFAULT.  Validation of
        # this CHECK happens here, under the lock, after the move.
        ("check_default", f"ALTER TABLE {qdef} ADD CONSTRAINT {excl_con} CHECK (NOT ({rng}))"),
        # Metadata-only attach: both sides carry proving CHECK constraints.
        # After this statement the DEFAULT partition's implicit constraint
        # excludes the month, so the temporary CHECK is redundant.
        ("attach", f"ALTER TABLE {qparent} ATTACH PARTITION {qnew} "
                   f"FOR VALUES FROM ('{lo}') TO ('{hi}')"),
        ("drop_check_default", f"ALTER TABLE {qdef} DROP CONSTRAINT {excl_con}"),
    ]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self, cfg, parent, partitions, start, end):
        self.cfg = cfg
        self.parent = parent
        self.partitions = partitions
        self.start = start
        self.end = end
        self.default = next((p for p in partitions if p.bound.is_default), None)
        self.exact = next(
            (p for p in partitions
             if not p.bound.is_default and p.bound.lower == start and p.bound.upper == end),
            None,
        )
        self.conflicts = [
            p for p in partitions
            if not p.bound.is_default and p.bound.overlaps(start, end)
            and not (p.bound.lower == start and p.bound.upper == end)
        ]
        self.name_taken = None
        self.estimated_rows = None
        self.default_total_rows = None
        self.status = None
        self.statements = []

    def to_dict(self) -> dict:
        def part_dict(p):
            d = {"name": p.qname, "oid": p.oid, "bound": p.bound.raw}
            if not p.bound.is_default:
                d["range"] = [_fmt_endpoint(p.bound.lower), _fmt_endpoint(p.bound.upper)]
            return d

        doc = {
            "status": self.status,
            "table": self.cfg.table_qname,
            "month": self.cfg.month,
            "timezone": "UTC",
            "target_range": {"start": _fmt_dt(self.start), "end": _fmt_dt(self.end)},
            "parent": {
                "oid": self.parent.oid,
                "partition_strategy": "range",
                "key_column": self.parent.key_column,
                "key_type": self.parent.key_type,
            },
            "default_partition": (
                {"name": self.default.qname, "oid": self.default.oid} if self.default else None
            ),
            "existing_partitions": [part_dict(p) for p in self.partitions],
            "conflicting_partitions": [part_dict(p) for p in self.conflicts],
            "matching_partition": part_dict(self.exact) if self.exact else None,
            "new_partition": self.cfg.new_qname,
            "new_partition_name_taken": self.name_taken,
            "estimated_rows_to_move": self.estimated_rows,
            "default_partition_total_rows": self.default_total_rows,
            "lock_timeout_ms": self.cfg.lock_timeout_ms,
            "statement_timeout_ms": self.cfg.statement_timeout_ms,
            "planned_actions": [sql for _label, sql in self.statements],
        }
        return doc


def build_plan(conn, cfg: Config) -> Plan:
    """Read the catalog and derive what a migration would have to do.

    Read-only and lock-free; the result is a snapshot that ``apply`` always
    re-derives and re-validates under the migration lock.
    """
    parent = read_parent(conn, cfg.table_schema, cfg.table_name)
    partitions = read_partitions(conn, parent.oid)
    start, end = cfg.month_range
    plan = Plan(cfg, parent, partitions, start, end)

    existing = lookup_relation(conn, cfg.new_schema, cfg.new_name)
    if existing is not None and not (
        plan.exact is not None and existing["oid"] == plan.exact.oid
    ):
        comment = existing["comment"] or ""
        plan.name_taken = {
            "name": cfg.new_qname,
            "relkind": existing["relkind"],
            "is_partition": existing["relispartition"],
            "tool_marker": comment or None,
            "looks_like_tool_leftover": comment.startswith(MARKER_PREFIX),
        }

    if plan.default is not None:
        plan.estimated_rows = count_rows_in_range(
            conn, plan.default.schema, plan.default.name,
            parent.key_column, start, end,
        )
        plan.default_total_rows = count_rows(conn, plan.default.schema, plan.default.name)

    if plan.exact is not None:
        plan.status = "already_done"
    elif plan.conflicts:
        plan.status = "conflict"
    elif plan.default is None:
        plan.status = "no_default"
    elif plan.name_taken is not None:
        plan.status = "name_taken"
    else:
        plan.status = "ready"
        plan.statements = migration_statements(cfg, parent, plan.default, start, end)
    return plan


def run_plan(dsn: str, cfg: Config):
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    try:
        conn.execute("SET TIME ZONE 'UTC'")
        # Bounded waiting also for the read-only catalog/data reads: if
        # another session holds a conflicting lock, report instead of
        # blocking forever.
        conn.execute(f"SET lock_timeout = '{cfg.lock_timeout_ms}ms'")
        try:
            plan = build_plan(conn, cfg)
        except psycopg.errors.LockNotAvailable as exc:
            return {
                "status": "error",
                "reason": "could not read the catalog within "
                          f"lock_timeout={cfg.lock_timeout_ms}ms; another session "
                          "holds a conflicting lock on the table",
                "detail": str(exc).splitlines()[0],
            }, 1
    finally:
        conn.close()
    return plan.to_dict(), 0


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _recheck_under_lock(conn, cfg: Config, plan: Plan) -> None:
    """Re-validate the plan now that the partition tree is exclusively locked.

    Between plan/preflight and lock acquisition the catalog may have changed
    (someone attaching a conflicting partition, recreating the parent, taking
    the target name).  Any deviation aborts the attempt; because this runs
    inside the migration transaction, aborting rolls everything back.
    """
    try:
        parent2 = read_parent(conn, cfg.table_schema, cfg.table_name)
    except MoverError as exc:
        raise PlanChanged(f"parent table changed since planning: {exc}") from exc
    if parent2.oid != plan.parent.oid:
        raise PlanChanged("parent table was dropped and recreated since planning")

    parts = read_partitions(conn, parent2.oid)
    start, end = plan.start, plan.end
    for p in parts:
        if p.bound.is_default:
            continue
        if p.bound.lower == start and p.bound.upper == end:
            raise AlreadyDoneInTxn(f"partition {p.qname} already covers the month")
        if p.bound.overlaps(start, end):
            raise PlanChanged(f"conflicting partition appeared: {p.qname} ({p.bound.raw})")

    default2 = next((p for p in parts if p.bound.is_default), None)
    if default2 is None:
        raise PlanChanged("DEFAULT partition disappeared since planning")
    if plan.default is None or default2.oid != plan.default.oid:
        raise PlanChanged("DEFAULT partition was replaced since planning")

    if lookup_relation(conn, cfg.new_schema, cfg.new_name) is not None:
        raise PlanChanged(f"relation {cfg.new_qname} now exists; refusing to touch it")


def _execute_once(dsn: str, cfg: Config, plan: Plan) -> dict:
    """One migration attempt: a single transaction doing everything."""
    conn = psycopg.connect(dsn, connect_timeout=5)  # autocommit=False: real txn
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{cfg.lock_timeout_ms}ms'")
            conn.execute(f"SET LOCAL statement_timeout = '{cfg.statement_timeout_ms}ms'")
            conn.execute("SET LOCAL TIME ZONE 'UTC'")

            statements = migration_statements(cfg, plan.parent, plan.default,
                                              plan.start, plan.end)
            t0 = time.monotonic()
            conn.execute(statements[0][1])  # LOCK TABLE ... ACCESS EXCLUSIVE
            lock_wait_ms = (time.monotonic() - t0) * 1000.0

            _recheck_under_lock(conn, cfg, plan)

            # Exact number of rows to move, measured under the lock: no
            # writer can change it for the rest of this transaction.
            to_move = count_rows_in_range(
                conn, plan.default.schema, plan.default.name,
                plan.parent.key_column, plan.start, plan.end,
            )

            move_rowcount = None
            for label, sql in statements[1:]:
                cur = conn.execute(sql)
                if label == "move":
                    move_rowcount = cur.rowcount

            # In-transaction verification of the move invariants.
            left_in_default = count_rows_in_range(
                conn, plan.default.schema, plan.default.name,
                plan.parent.key_column, plan.start, plan.end,
            )
            if left_in_default != 0:
                raise InTxnVerificationFailed(
                    f"{left_in_default} month rows still in DEFAULT after the move"
                )
            in_new = count_rows(conn, cfg.new_schema, cfg.new_name)
            if in_new != to_move:
                raise InTxnVerificationFailed(
                    f"expected {to_move} rows in the new partition, found {in_new}"
                )
            if move_rowcount is not None and move_rowcount != to_move:
                raise InTxnVerificationFailed(
                    f"move statement affected {move_rowcount} rows, expected {to_move}"
                )
        return {"lock_wait_ms": lock_wait_ms, "moved_rows": to_move}
    finally:
        conn.close()


def _verify(conn, cfg: Config, start, end, expect_name: str | None = None) -> dict:
    """Post-commit verification against the live catalog (fresh reads).

    The migrated month is identified by its exact bounds, not by its name:
    a pre-existing partition may carry any name (e.g. "not_named_by_month").
    ``expect_name`` is set only right after this tool attached the partition
    itself, where the name must be the configured one.
    """
    parent = read_parent(conn, cfg.table_schema, cfg.table_name)
    parts = read_partitions(conn, parent.oid)
    matched = next(
        (p for p in parts
         if not p.bound.is_default and p.bound.lower == start and p.bound.upper == end),
        None,
    )
    default = next((p for p in parts if p.bound.is_default), None)
    result = {
        "matching_partition": matched.qname if matched else None,
        "new_partition_bound": matched.bound.raw if matched else None,
        "new_partition_range": (
            [_fmt_endpoint(matched.bound.lower), _fmt_endpoint(matched.bound.upper)]
            if matched else None
        ),
        "bound_matches_target": matched is not None,
        "name_matches_config": bool(matched and matched.qname == cfg.new_qname),
        "default_partition": default.qname if default else None,
        "default_rows_in_range": (
            count_rows_in_range(conn, default.schema, default.name,
                                parent.key_column, start, end)
            if default else None
        ),
        "new_partition_rows": (
            count_rows(conn, matched.schema, matched.name) if matched else None
        ),
        "parent_total_rows": count_rows(conn, parent.schema, parent.name),
    }
    result["ok"] = bool(
        matched is not None
        and (expect_name is None or matched.qname == expect_name)
        and result["default_rows_in_range"] == 0
    )
    return result


def _verify_guarded(conn, cfg: Config, start, end, expect_name: str | None = None) -> dict:
    """Post-commit verification; a conflicting lock makes it inconclusive
    rather than hanging forever."""
    try:
        return _verify(conn, cfg, start, end, expect_name)
    except psycopg.errors.LockNotAvailable as exc:
        return {
            "ok": None,
            "error": "post-commit verification could not read the table within "
                     f"lock_timeout: {str(exc).splitlines()[0]}",
        }


def run_apply(dsn: str, cfg: Config):
    """Run the migration.  Returns (report_dict, exit_code).

    Exit codes: 0 migrated / already done; 2 refused (conflicting partition,
    foreign object with the target name, no DEFAULT partition, plan changed);
    3 lock contention persisted through all bounded retries; 1 other failure.
    """
    started = time.monotonic()
    attempts = []
    start, end = cfg.month_range
    admin = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    try:
        admin.execute("SET TIME ZONE 'UTC'")
        # Bounded waiting for every read on this connection as well.
        admin.execute(f"SET lock_timeout = '{cfg.lock_timeout_ms}ms'")
        for attempt in range(1, cfg.max_attempts + 1):
            # Fresh preflight on every attempt: plan and data may have
            # changed since the previous one.
            try:
                plan = build_plan(admin, cfg)
            except (psycopg.errors.LockNotAvailable,
                    psycopg.errors.DeadlockDetected) as exc:
                kind = ("lock_timeout" if isinstance(exc, psycopg.errors.LockNotAvailable)
                        else "deadlock")
                _log(f"attempt {attempt}: {kind} during preflight — rolled back")
                attempts.append({
                    "attempt": attempt,
                    "result": kind,
                    "phase": "preflight",
                    "sqlstate": exc.sqlstate,
                    "detail": str(exc).splitlines()[0],
                })
                if attempt < cfg.max_attempts:
                    time.sleep(cfg.retry_backoff_ms * attempt / 1000.0)
                continue
            if plan.status == "already_done":
                _log(f"attempt {attempt}: month {cfg.month} already migrated; verifying")
                verification = _verify_guarded(admin, cfg, start, end)
                report = {
                    "status": "already_done",
                    "reason": f"partition {plan.exact.qname} already covers "
                              f"[{_fmt_dt(start)}, {_fmt_dt(end)})",
                    "attempts": attempts,
                    "moved_rows": None,
                    "verification": verification,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
                return report, 0 if verification["ok"] else 1
            if plan.status != "ready":
                reasons = {
                    "conflict": "an overlapping partition exists; refusing to run a stale plan",
                    "no_default": "parent has no DEFAULT partition to migrate rows from",
                    "name_taken": f"relation {cfg.new_qname} already exists and is not an "
                                  "attached partition for this month; this tool never drops "
                                  "or reuses foreign objects — resolve it manually",
                }
                reason = reasons.get(plan.status, plan.status)
                if plan.status == "name_taken" and plan.name_taken.get("looks_like_tool_leftover"):
                    reason += (" (the object carries this tool's marker comment, so it is a "
                               "leftover of an interrupted run; inspect it and remove it "
                               "manually if it is safe to do so)")
                _log(f"attempt {attempt}: refusing — {reason}")
                report = {
                    "status": "refused",
                    "reason": reason,
                    "plan_status": plan.status,
                    "conflicting_partitions": [
                        p.qname for p in plan.conflicts
                    ],
                    "new_partition_name_taken": plan.name_taken,
                    "attempts": attempts,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
                return report, 2

            _log(f"attempt {attempt}: acquiring lock (lock_timeout={cfg.lock_timeout_ms}ms)")
            try:
                result = _execute_once(dsn, cfg, plan)
            except AlreadyDoneInTxn as exc:
                _log(f"attempt {attempt}: already done under lock: {exc}")
                verification = _verify_guarded(admin, cfg, start, end)
                return {
                    "status": "already_done",
                    "reason": str(exc),
                    "attempts": attempts,
                    "moved_rows": None,
                    "verification": verification,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }, 0 if verification["ok"] else 1
            except PlanChanged as exc:
                _log(f"attempt {attempt}: plan invalidated under lock: {exc}")
                return {
                    "status": "refused",
                    "reason": f"plan no longer valid at execution time: {exc}",
                    "plan_status": "changed",
                    "attempts": attempts,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }, 2
            except (psycopg.errors.LockNotAvailable,
                    psycopg.errors.DeadlockDetected) as exc:
                kind = ("lock_timeout" if isinstance(exc, psycopg.errors.LockNotAvailable)
                        else "deadlock")
                _log(f"attempt {attempt}: {kind}: {str(exc).splitlines()[0]} — rolled back")
                attempts.append({
                    "attempt": attempt,
                    "result": kind,
                    "sqlstate": exc.sqlstate,
                    "detail": str(exc).splitlines()[0],
                })
                if attempt < cfg.max_attempts:
                    time.sleep(cfg.retry_backoff_ms * attempt / 1000.0)
                continue

            attempts.append({
                "attempt": attempt,
                "result": "success",
                "lock_wait_ms": round(result["lock_wait_ms"], 1),
            })
            verification = _verify_guarded(admin, cfg, start, end,
                                           expect_name=cfg.new_qname)
            _log(f"migrated {result['moved_rows']} rows into {cfg.new_qname}")
            report = {
                "status": "migrated",
                "reason": None,
                "attempts": attempts,
                "moved_rows": result["moved_rows"],
                "lock_wait_ms": round(result["lock_wait_ms"], 1),
                "new_partition": {
                    "name": cfg.new_qname,
                    "bound": verification.get("new_partition_bound"),
                    "range": verification.get("new_partition_range"),
                },
                "verification": verification,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
            # The migration transaction committed; an inconclusive
            # verification (lock contention on the re-read) does not change
            # that fact, so only a failed verification changes the exit code.
            return report, 0 if verification["ok"] is not False else 1

        report = {
            "status": "lock_failed",
            "reason": f"could not acquire the migration lock within "
                      f"{cfg.lock_timeout_ms}ms on {cfg.max_attempts} attempt(s); "
                      "every attempt was rolled back cleanly — rerun later",
            "attempts": attempts,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        }
        return report, 3
    finally:
        admin.close()
