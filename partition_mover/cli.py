"""Command-line interface: ``python3 -m partition_mover plan|apply``."""
from __future__ import annotations

import argparse
import json
import sys

import psycopg

from .config import load_config
from .core import run_apply, run_plan
from .errors import MoverError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partition_mover",
        description=(
            "One-time migration of one UTC month of rows from the DEFAULT "
            "partition of a range-partitioned table into a new monthly "
            "partition, safe under bounded concurrent writes."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command, helptext in (
        ("plan", "inspect the catalog and print the migration plan as JSON"),
        ("apply", "re-validate the plan and run the migration; print a JSON report"),
    ):
        sp = sub.add_parser(command, help=helptext)
        sp.add_argument("--config", required=True, help="path to target.json")
        sp.add_argument("--dsn", required=True, help="PostgreSQL connection string")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except MoverError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "plan":
            doc, code = run_plan(args.dsn, cfg)
        else:
            doc, code = run_apply(args.dsn, cfg)
    except MoverError as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"{args.command} failed with database error "
              f"[{exc.sqlstate}]: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1

    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return code
