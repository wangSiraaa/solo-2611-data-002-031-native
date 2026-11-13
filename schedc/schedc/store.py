"""PostgreSQL versioned storage for compiled schedules.

Publication is atomic: a version row plus its rules, exceptions, plans and
exclusions are written in one transaction, and the current-version pointer
is only moved at the very end.  Optimistic concurrency is provided by
locking the singleton ``current_version`` row; idempotent retries are
provided by the unique ``request_id``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from .compiler import CompileResult, compile_rules, make_plan_id, resolve_local
from .models import (UTC, CompileError, DateException, Exclusion, Plan, Rule,
                     canonical_exception, canonical_rule, content_hash, iso_z,
                     parse_exception, parse_rule)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


class VersionConflict(Exception):
    """expected_version does not match the stored current version."""


class RequestConflict(Exception):
    """request_id was already used with different content."""


class NotFound(Exception):
    """requested version does not exist."""


def connect(dsn: str) -> psycopg.Connection:
    # autocommit mode: standalone statements never leave a dangling
    # implicit transaction behind, and every `with conn.transaction()`
    # block below is a real top-level (atomic) transaction.
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def migrate(conn: psycopg.Connection, sql_dir: Path | None = None) -> list[int]:
    """Apply pending migrations from sql_dir in filename order."""
    sql_dir = Path(sql_dir) if sql_dir else SQL_DIR
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, "
            "applied_at timestamptz NOT NULL DEFAULT now())")
    applied = {r[0] for r in
               conn.execute("SELECT version FROM schema_migrations").fetchall()}
    done = []
    for path in sorted(sql_dir.glob("*.sql")):
        v = int(path.name.split("_")[0])
        if v in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (v,))
        done.append(v)
    return done


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------

def publish(conn: psycopg.Connection, *, rules: list[Rule],
            exceptions: list[DateException], result: CompileResult,
            expected_version: int, request_id: str,
            fault_after_plans: int | None = None) -> dict:
    """Atomically store one compiled version and make it current.

    Idempotency: a retry with the same request_id and identical content
    returns the original version; the same request_id with different
    content is rejected.  Concurrency: publishers serialise on the
    current_version row, so of two publishers sharing one expected_version
    at most one can commit.
    """
    chash = content_hash(rules, exceptions, result.window_start, result.window_end)
    with conn.transaction():
        row = conn.execute(
            "SELECT version, content_hash, rule_count, plan_count, exclusion_count "
            "FROM versions WHERE request_id = %s", (request_id,)).fetchone()
        if row is not None:
            if row[1] != chash:
                raise RequestConflict(
                    f"request_id {request_id!r} was already used for version "
                    f"{row[0]} with different content")
            return {"version": row[0], "replayed": True, "content_hash": chash,
                    "rule_count": row[2], "plan_count": row[3],
                    "exclusion_count": row[4]}

        cur = conn.execute(
            "SELECT version FROM current_version WHERE id = true FOR UPDATE").fetchone()
        current = cur[0] if (cur and cur[0] is not None) else 0
        if expected_version != current:
            raise VersionConflict(
                f"version changed: expected_version={expected_version}, "
                f"current version is {current}")

        new_version = conn.execute(
            "INSERT INTO versions(request_id, content_hash, window_start, "
            "window_end, rule_count, plan_count, exclusion_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING version",
            (request_id, chash, result.window_start, result.window_end,
             len(rules), len(result.plans), len(result.exclusions))).fetchone()[0]

        for r in sorted(rules, key=lambda r: r.rule_id):
            conn.execute(
                "INSERT INTO rules(version, rule_id, content) VALUES (%s,%s,%s)",
                (new_version, r.rule_id, Jsonb(canonical_rule(r))))
        for e in sorted(exceptions, key=lambda e: (e.rule_id, e.local_date)):
            conn.execute(
                "INSERT INTO rule_exceptions(version, rule_id, local_date, "
                "action, replace_time) VALUES (%s,%s,%s,%s,%s)",
                (new_version, e.rule_id, e.local_date, e.action, e.replace_time))

        inserted = 0
        for p in result.plans:
            conn.execute(
                "INSERT INTO plans(version, plan_id, rule_id, planned_date, "
                "local_time, utc_time, offset_seconds, branch, exception_basis) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_version, p.plan_id, p.rule_id, p.planned_date, p.local_dt,
                 p.utc_dt, p.offset_seconds, p.branch, p.exception_basis))
            inserted += 1
            if fault_after_plans is not None and inserted >= fault_after_plans:
                # Test hook: simulate a hard process kill mid-publication.
                print(f"FAULT INJECTED after {inserted} plan inserts",
                      file=sys.stderr, flush=True)
                os._exit(1)

        for x in result.exclusions:
            conn.execute(
                "INSERT INTO exclusions(version, rule_id, local_date, reason, "
                "detail) VALUES (%s,%s,%s,%s,%s)",
                (new_version, x.rule_id, x.local_date, x.reason, x.detail))

        if cur is None:
            conn.execute(
                "INSERT INTO current_version(id, version) VALUES (true, %s)",
                (new_version,))
        else:
            conn.execute(
                "UPDATE current_version SET version = %s WHERE id = true",
                (new_version,))

    return {"version": new_version, "replayed": False, "content_hash": chash,
            "rule_count": len(rules), "plan_count": len(result.plans),
            "exclusion_count": len(result.exclusions)}


# --------------------------------------------------------------------------
# query
# --------------------------------------------------------------------------

def _load_version(conn: psycopg.Connection, version: int | None):
    if version is None:
        row = conn.execute(
            "SELECT v.version, v.request_id, v.content_hash, v.window_start, "
            "v.window_end, v.rule_count, v.plan_count, v.exclusion_count, "
            "v.created_at FROM current_version c "
            "JOIN versions v ON v.version = c.version WHERE c.id = true").fetchone()
        if row is None:
            raise NotFound("no version published yet")
    else:
        row = conn.execute(
            "SELECT version, request_id, content_hash, window_start, window_end, "
            "rule_count, plan_count, exclusion_count, created_at "
            "FROM versions WHERE version = %s", (version,)).fetchone()
        if row is None:
            raise NotFound(f"version {version} not found")

    v = row[0]
    rules = [parse_rule(r[0]) for r in conn.execute(
        "SELECT content FROM rules WHERE version = %s ORDER BY rule_id", (v,))]
    exceptions = [DateException(rule_id=r[0], local_date=r[1], action=r[2],
                                replace_time=r[3])
                  for r in conn.execute(
                      "SELECT rule_id, local_date, action, replace_time "
                      "FROM rule_exceptions WHERE version = %s "
                      "ORDER BY rule_id, local_date", (v,))]
    plans = [Plan(plan_id=r[0], rule_id=r[1], planned_date=r[2], local_dt=r[3],
                  utc_dt=r[4].astimezone(UTC), offset_seconds=r[5], branch=r[6],
                  exception_basis=r[7])
             for r in conn.execute(
                 "SELECT plan_id, rule_id, planned_date, local_time, utc_time, "
                 "offset_seconds, branch, exception_basis FROM plans "
                 "WHERE version = %s "
                 "ORDER BY utc_time, rule_id, planned_date", (v,))]
    exclusions = [Exclusion(rule_id=r[0], local_date=r[1], reason=r[2], detail=r[3])
                  for r in conn.execute(
                      "SELECT rule_id, local_date, reason, detail FROM exclusions "
                      "WHERE version = %s ORDER BY rule_id, local_date, reason", (v,))]
    meta = {
        "version": v,
        "request_id": row[1],
        "content_hash": row[2],
        "window_start": row[3].astimezone(UTC),
        "window_end": row[4].astimezone(UTC),
        "rule_count": row[5],
        "plan_count": row[6],
        "exclusion_count": row[7],
        "created_at": row[8].astimezone(UTC),
    }
    return meta, rules, exceptions, plans, exclusions


def _offset_str(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    s = abs(seconds)
    return f"{sign}{s // 3600:02d}:{(s % 3600) // 60:02d}"


def plan_to_json(p: Plan) -> dict:
    return {
        "plan_id": p.plan_id,
        "rule_id": p.rule_id,
        "planned_date": p.planned_date.isoformat(),
        "local_time": p.local_dt.isoformat(),
        "utc_time": iso_z(p.utc_dt),
        "offset": _offset_str(p.offset_seconds),
        "offset_seconds": p.offset_seconds,
        "branch": p.branch,
        "exception_basis": p.exception_basis,
    }


def exclusion_to_json(x: Exclusion) -> dict:
    return {"rule_id": x.rule_id, "local_date": x.local_date.isoformat(),
            "reason": x.reason, "detail": x.detail}


def get_version(conn: psycopg.Connection, version: int | None = None) -> dict:
    with conn.transaction():
        meta, rules, exceptions, plans, exclusions = _load_version(conn, version)
    return {
        "version": meta["version"],
        "request_id": meta["request_id"],
        "content_hash": meta["content_hash"],
        "created_at": iso_z(meta["created_at"]),
        "window": {"start": iso_z(meta["window_start"]),
                   "end": iso_z(meta["window_end"])},
        "rule_count": meta["rule_count"],
        "plan_count": meta["plan_count"],
        "exclusion_count": meta["exclusion_count"],
        "rules": [canonical_rule(r) for r in rules],
        "exceptions": [canonical_exception(e) for e in exceptions],
        "plans": [plan_to_json(p) for p in plans],
        "exclusions": [exclusion_to_json(x) for x in exclusions],
    }


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify(conn: psycopg.Connection, version: int | None = None) -> dict:
    """Re-check a stored version: round-trips, uniqueness, integrity."""
    checks: list[dict] = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        with conn.transaction():
            meta, rules, exceptions, plans, exclusions = _load_version(
                conn, version)
    except NotFound as e:
        check("version_exists", False, str(e))
        return {"ok": False, "version": version, "checks": checks}
    v = meta["version"]
    check("version_exists", True, f"version {v}")

    check("counts_match",
          meta["rule_count"] == len(rules)
          and meta["plan_count"] == len(plans)
          and meta["exclusion_count"] == len(exclusions),
          f"stored counts rules={meta['rule_count']} plans={meta['plan_count']} "
          f"exclusions={meta['exclusion_count']}; actual rules={len(rules)} "
          f"plans={len(plans)} exclusions={len(exclusions)}")

    expected_hash = content_hash(rules, exceptions,
                                 meta["window_start"], meta["window_end"])
    check("content_hash", expected_hash == meta["content_hash"],
          f"expected {expected_hash}, stored {meta['content_hash']}")

    out_of_window = [p.plan_id for p in plans
                     if not (meta["window_start"] <= p.utc_dt < meta["window_end"])]
    check("plans_within_window", not out_of_window,
          f"{len(out_of_window)} plan(s) outside half-open window"
          + (f", e.g. {out_of_window[0]}" if out_of_window else ""))

    rules_by_id = {r.rule_id: r for r in rules}
    bad_ids = [p.plan_id for p in plans
               if make_plan_id(rules_by_id[p.rule_id], p.planned_date, p.utc_dt)
               != p.plan_id]
    check("plan_id_stable", not bad_ids,
          f"{len(bad_ids)} plan id(s) do not match recomputation"
          + (f", e.g. {bad_ids[0]}" if bad_ids else ""))

    problems = []
    for p in plans:
        rule = rules_by_id[p.rule_id]
        occs = resolve_local(ZoneInfo(rule.timezone), p.local_dt)
        if not any(o[0] == p.utc_dt and o[1] == p.offset_seconds for o in occs):
            problems.append(f"{p.plan_id}: utc/offset not a real occurrence "
                            f"of local time")
            continue
        if len(occs) == 1 and p.branch != "none":
            problems.append(f"{p.plan_id}: single occurrence but branch="
                            f"{p.branch}")
        if len(occs) == 2:
            if p.branch == "first" and p.utc_dt != occs[0][0]:
                problems.append(f"{p.plan_id}: branch=first but not earliest UTC")
            if p.branch == "second" and p.utc_dt != occs[1][0]:
                problems.append(f"{p.plan_id}: branch=second but not latest UTC")
            if p.branch not in ("first", "second"):
                problems.append(f"{p.plan_id}: ambiguous time but branch="
                                f"{p.branch}")
    check("local_utc_roundtrip", not problems,
          "; ".join(problems[:3]) if problems else
          f"all {len(plans)} plans round-trip local<->UTC")

    ids = [p.plan_id for p in plans]
    keys = [(p.rule_id, p.planned_date, p.utc_dt) for p in plans]
    check("plan_uniqueness",
          len(set(ids)) == len(ids) and len(set(keys)) == len(keys),
          f"{len(ids)} plans, {len(set(ids))} distinct ids, "
          f"{len(set(keys))} distinct (rule,date,utc)")

    try:
        recompiled = compile_rules(rules, exceptions,
                                   meta["window_start"], meta["window_end"])
        same_plans = (sorted(p.plan_id for p in recompiled.plans)
                      == sorted(p.plan_id for p in plans))
        same_exc = (sorted((x.rule_id, x.local_date, x.reason)
                           for x in recompiled.exclusions)
                    == sorted((x.rule_id, x.local_date, x.reason)
                              for x in exclusions))
        check("recompile_deterministic", same_plans and same_exc,
              "stored output equals fresh recompilation"
              if (same_plans and same_exc) else
              f"plans match={same_plans}, exclusions match={same_exc}")
    except CompileError as e:
        check("recompile_deterministic", False, f"recompile failed: {e}")

    row = conn.execute(
        "SELECT c.version, v.version IS NOT NULL FROM current_version c "
        "LEFT JOIN versions v ON v.version = c.version WHERE c.id = true").fetchone()
    check("current_pointer",
          row is not None and (row[0] is None or row[1]),
          f"current_version -> {row[0] if row else 'missing row'}")

    return {"ok": all(c["ok"] for c in checks), "version": v, "checks": checks}
