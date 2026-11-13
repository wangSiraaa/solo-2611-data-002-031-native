"""schedc command line interface.

Commands: migrate, generate, compile, publish, query, verify.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import store
from .compiler import compile_rules
from .models import (CompileError, content_hash, iso_z, load_document,
                     parse_instant)
from .store import exclusion_to_json, plan_to_json


# --------------------------------------------------------------------------
# synthetic rule set used by `generate` (and by the README walk-through)
# --------------------------------------------------------------------------

def sample_document() -> dict:
    """Synthetic rules exercising every required scenario (window: 2026)."""
    return {
        "rules": [
            {"rule_id": "ny-spring-skip", "timezone": "America/New_York",
             "local_time": "02:30:00",
             "start_date": "2026-03-01", "end_date": "2026-03-15",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "ny-fall-first", "timezone": "America/New_York",
             "local_time": "01:30:00",
             "start_date": "2026-10-25", "end_date": "2026-11-08",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "ny-fall-second", "timezone": "America/New_York",
             "local_time": "01:30:00",
             "start_date": "2026-10-25", "end_date": "2026-11-08",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "second"},
            {"rule_id": "ny-fall-both", "timezone": "America/New_York",
             "local_time": "01:30:00",
             "start_date": "2026-10-25", "end_date": "2026-11-08",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "both"},
            {"rule_id": "sh-daily", "timezone": "Asia/Shanghai",
             "local_time": "09:00:00",
             "start_date": "2026-01-01", "end_date": "2026-12-31",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "ny-month-end", "timezone": "America/New_York",
             "local_time": "21:00:00",
             "start_date": "2026-01-01", "end_date": "2026-12-31",
             "frequency": {"type": "monthly", "day": "L"},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "sh-monthly-31", "timezone": "Asia/Shanghai",
             "local_time": "10:00:00",
             "start_date": "2026-01-01", "end_date": "2026-12-31",
             "frequency": {"type": "monthly", "day": 31},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "ny-weekly-mon", "timezone": "America/New_York",
             "local_time": "09:00:00",
             "start_date": "2026-03-01", "end_date": "2026-03-31",
             "frequency": {"type": "weekly", "weekdays": [1]},
             "on_nonexistent": "skip", "on_repeated": "first"},
            {"rule_id": "utc-midnight", "timezone": "Etc/UTC",
             "local_time": "00:00:00",
             "start_date": "2026-01-01", "end_date": "2026-12-31",
             "frequency": {"type": "daily"},
             "on_nonexistent": "skip", "on_repeated": "first"},
        ],
        "exceptions": [
            {"rule_id": "ny-weekly-mon", "date": "2026-03-09",
             "action": "skip"},
            {"rule_id": "ny-weekly-mon", "date": "2026-03-11",
             "action": "replace", "replace_time": "15:00:00"},
            {"rule_id": "ny-weekly-mon", "date": "2026-03-16",
             "action": "replace", "replace_time": "07:30:00"},
        ],
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _load_rules_file(path: str):
    try:
        obj = json.loads(Path(path).read_text())
    except OSError as e:
        raise CompileError(f"cannot read rules file: {e}") from None
    except json.JSONDecodeError as e:
        raise CompileError(f"rules file is not valid JSON: {e}") from None
    return load_document(obj)


def _compile_from_args(args):
    rules, exceptions = _load_rules_file(args.rules)
    start = parse_instant(args.start)
    end = parse_instant(args.end)
    result = compile_rules(rules, exceptions, start, end)
    return rules, exceptions, result


def _result_to_json(rules, exceptions, result) -> dict:
    return {
        "window": {"start": iso_z(result.window_start),
                   "end": iso_z(result.window_end)},
        "content_hash": content_hash(rules, exceptions,
                                     result.window_start, result.window_end),
        "plan_count": len(result.plans),
        "exclusion_count": len(result.exclusions),
        "plans": [plan_to_json(p) for p in result.plans],
        "exclusions": [exclusion_to_json(x) for x in result.exclusions],
    }


def _emit(doc: dict, out: str | None) -> None:
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if out:
        Path(out).write_text(text)
        print(json.dumps({"written": out, "bytes": len(text)}))
    else:
        sys.stdout.write(text)


def _resolve_dsn(args) -> str:
    dsn = (args.dsn or os.environ.get("SCHEDC_DSN")
           or os.environ.get("DATABASE_URL"))
    if not dsn:
        raise CompileError(
            "no database DSN: pass --dsn or set SCHEDC_DSN / DATABASE_URL")
    return dsn


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------

def _cmd_migrate(args) -> int:
    conn = store.connect(_resolve_dsn(args))
    try:
        done = store.migrate(conn)
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"applied_migrations": done}))
    return 0


def _cmd_generate(args) -> int:
    _emit(sample_document(), args.out)
    return 0


def _cmd_compile(args) -> int:
    rules, exceptions, result = _compile_from_args(args)
    _emit(_result_to_json(rules, exceptions, result), args.out)
    return 0


def _cmd_publish(args) -> int:
    rules, exceptions, result = _compile_from_args(args)
    fault = os.environ.get("SCHEDC_FAULT_AFTER_PLANS")
    fault_after = int(fault) if fault else None
    conn = store.connect(_resolve_dsn(args))
    try:
        outcome = store.publish(
            conn, rules=rules, exceptions=exceptions, result=result,
            expected_version=args.expected_version, request_id=args.request_id,
            fault_after_plans=fault_after)
        conn.commit()
    finally:
        conn.close()
    outcome["window"] = {"start": iso_z(result.window_start),
                         "end": iso_z(result.window_end)}
    print(json.dumps(outcome, indent=2))
    return 0


def _cmd_query(args) -> int:
    conn = store.connect(_resolve_dsn(args))
    try:
        doc = store.get_version(conn, args.version)
    finally:
        conn.close()
    _emit(doc, args.out)
    return 0


def _cmd_verify(args) -> int:
    conn = store.connect(_resolve_dsn(args))
    try:
        report = store.verify(conn, args.version)
    finally:
        conn.close()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 5


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schedc",
        description="Compile local periodic schedules to UTC plans and "
                    "publish them as atomic, versioned snapshots.")
    p.add_argument("--dsn", help="PostgreSQL DSN (or SCHEDC_DSN/DATABASE_URL)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending database migrations")

    g = sub.add_parser("generate", help="write a synthetic rules document")
    g.add_argument("--out", help="output file (default: stdout)")

    c = sub.add_parser("compile", help="compile rules into UTC plans")
    c.add_argument("--rules", required=True, help="rules JSON file")
    c.add_argument("--start", required=True, help="window start, UTC instant")
    c.add_argument("--end", required=True, help="window end, UTC instant (excluded)")
    c.add_argument("--out", help="output file (default: stdout)")

    pub = sub.add_parser("publish", help="compile and atomically store a version")
    pub.add_argument("--rules", required=True)
    pub.add_argument("--start", required=True)
    pub.add_argument("--end", required=True)
    pub.add_argument("--expected-version", type=int, required=True,
                     help="version this publish is based on (0 = none yet)")
    pub.add_argument("--request-id", required=True,
                     help="idempotency key for safe retries")

    q = sub.add_parser("query", help="read a stored version (default: current)")
    q.add_argument("--version", type=int, help="explicit historical version")
    q.add_argument("--out", help="output file (default: stdout)")

    vf = sub.add_parser("verify", help="re-check a stored version")
    vf.add_argument("--version", type=int, help="explicit historical version")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"migrate": _cmd_migrate, "generate": _cmd_generate,
                "compile": _cmd_compile, "publish": _cmd_publish,
                "query": _cmd_query, "verify": _cmd_verify}
    try:
        return handlers[args.command](args)
    except CompileError as e:
        print(json.dumps({"error": "compile_failed", "detail": str(e)}),
              file=sys.stderr)
        return 2
    except store.VersionConflict as e:
        print(json.dumps({"error": "version_conflict", "detail": str(e)}),
              file=sys.stderr)
        return 3
    except store.RequestConflict as e:
        print(json.dumps({"error": "request_conflict", "detail": str(e)}),
              file=sys.stderr)
        return 4
    except store.NotFound as e:
        print(json.dumps({"error": "not_found", "detail": str(e)}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
