"""Command-line entry point for the restricted two-source executor.

Examples
--------
python -m fedquery.run --source fixtures/source.json --queries fixtures/queries.json \
    --dsn "host=/tmp/pg-task-$USER dbname=taskdb"

python -m fedquery.run ... --query filter_in_on --audit products-requests.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .engine import DEFAULT_BATCH_SIZE, ExecutionError, execute
from .model import MAX_BATCH_SIZE, SchemaError
from .query import QueryError, parse_query_file
from .sources import ProductsClient, PostgresOrders, SourceError, load_source_config


def _connect(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "psycopg is not installed; run: pip install 'psycopg[binary]>=3.1'"
        ) from exc
    try:
        return psycopg.connect(dsn)
    except psycopg.Error as exc:
        raise SystemExit(f"could not connect to PostgreSQL with the given DSN: {exc}")


def _read_audit(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return entries, len(lines)


def _format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "    (0 rows)"
    columns = list(rows[0])
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in columns}
    header = "    " + " | ".join(c.ljust(widths[c]) for c in columns)
    divider = "    " + "-+-".join("-" * widths[c] for c in columns)
    body = [
        "    " + " | ".join(str(r[c]).ljust(widths[c]) for c in columns) for r in rows
    ]
    return "\n".join([header, divider, *body])


def run(
    *,
    source_path: str,
    queries_path: str,
    dsn: str,
    query_names: Optional[list[str]] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    audit_path: Optional[str] = None,
    json_out: Optional[str] = None,
) -> int:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")

    config = load_source_config(
        source_path, expected_schema="fed_input", expected_table="orders"
    )
    specs = parse_query_file(queries_path)
    if query_names:
        known = {s.name for s in specs}
        missing = [n for n in query_names if n not in known]
        if missing:
            raise SystemExit(f"unknown query name(s): {', '.join(missing)}")
        specs = [s for s in specs if s.name in set(query_names)]

    results: list[dict[str, Any]] = []
    exit_code = 0
    connection = _connect(dsn)
    try:
        for spec in specs:
            audit_target = Path(audit_path) if audit_path else None
            before_entries = before_lines = 0
            if audit_target is not None:
                before_entries, before_lines = _read_audit(audit_target)
            # Each query gets a fresh step log so counts cannot leak across runs.
            orders_source = PostgresOrders(
                connection, config.postgres_schema, config.postgres_table
            )
            products_client = ProductsClient(config.http)
            try:
                result = execute(
                    spec,
                    orders_source=orders_source,
                    products_client=products_client,
                    batch_size=batch_size,
                )
            except (ExecutionError, SchemaError, SourceError) as exc:
                print(f"[{spec.name}] FAILED: {exc}", file=sys.stderr)
                results.append({"query": spec.name, "error": str(exc)})
                exit_code = 1
                continue

            observed: list[dict[str, Any]] = []
            if audit_target is not None:
                after_entries, after_lines = _read_audit(audit_target)
                new_entries = after_entries[len(before_entries):]
                observed = new_entries
                result.insight["audit_file"] = {
                    "path": str(audit_target),
                    "observed_requests_this_query": len(new_entries),
                    "appended_lines": after_lines - before_lines,
                }

            payload = result.as_dict()
            payload["explain"]["pushdown_evidence"]["observed_provider_log"] = observed
            results.append(payload)
            _print_result(result, observed)
    finally:
        connection.close()

    if json_out:
        Path(json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON written to {json_out}")
    return exit_code


def _print_result(result: Any, observed: list[dict[str, Any]]) -> None:
    print(f"\n=== {result.name} ===")
    print(_format_rows(result.rows))
    plan = result.insight["plan"]
    print("  pushdown:")
    print(
        f"    PostgreSQL region filter: {plan['orders_region_filter']['predicate']} "
        f"(pushed down)"
    )
    cat = plan["products_category"]
    if cat["predicate"]:
        print(
            f"    product category {cat['predicate']} -> {cat['placement']} "
            f"(remote pushdown={cat['remote_pushdown']}, local recheck="
            f"{cat['local_recheck']})"
        )
        print(f"      {cat['reason']}")
    keys = plan["keys"]
    batch_desc = (
        f"{keys['distinct_keys']} distinct key(s) in {keys['requests']} batch(es) "
        f"of <= {keys['batch_size']}"
        if keys["requests"]
        else f"{keys['distinct_keys']} distinct key(s); no catalogue request needed"
    )
    print(
        f"    join {keys['join']} on {keys['key_pair'][0]} = {keys['key_pair'][1]}; "
        f"{batch_desc}"
    )
    print(f"    orders projection: {', '.join(plan['projection']['orders_columns'])}")
    print(f"    product projection: {', '.join(plan['projection']['product_fields'])}")
    print("  stages:")
    for stage, value in result.insight["stage_rows"].items():
        print(f"    {stage}: {value}")
    print("  actual PostgreSQL statements:")
    for step in result.pg_steps:
        print(f"    [{step.stage}] {step.sql}")
        print(f"      params={json.dumps(step.params, ensure_ascii=False)} rows={step.rows}")
    print("  actual HTTP requests:")
    for step in result.http_steps:
        print(f"    [{step.stage}] {step.url}")
        print(f"      ids={step.requested_ids} returned={step.returned_rows}")
    if observed:
        print("  provider audit log entries for this query:")
        for entry in observed:
            print(f"    {json.dumps(entry, ensure_ascii=False)}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restricted two-source (orders + products) query executor."
    )
    parser.add_argument("--source", default="fixtures/source.json")
    parser.add_argument("--queries", default="fixtures/queries.json")
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN (default: env FEDQUERY_DSN or host/$PGHOST...)",
    )
    parser.add_argument("--query", action="append", dest="query_names",
                        help="run only the named query (repeatable)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--audit", default=None,
        help="products fixture audit JSONL path to read back and prove pushdown",
    )
    parser.add_argument("--json-out", default=None, help="write full JSON results here")
    args = parser.parse_args(argv)

    dsn = args.dsn or __import__("os").environ.get("FEDQUERY_DSN")
    if not dsn:
        raise SystemExit(
            "no PostgreSQL DSN: pass --dsn or set FEDQUERY_DSN "
            "(see README for local initialization)"
        )

    try:
        return run(
            source_path=args.source,
            queries_path=args.queries,
            dsn=dsn,
            query_names=args.query_names,
            batch_size=args.batch_size,
            audit_path=args.audit,
            json_out=args.json_out,
        )
    except QueryError as exc:
        print(f"query error: {exc}", file=sys.stderr)
        return 2
    except SourceError as exc:
        print(f"source config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
