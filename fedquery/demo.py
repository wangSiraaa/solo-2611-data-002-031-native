#!/usr/bin/env python3
"""Run the fixtures end to end and render a human-readable report.

Starts nothing itself: requires the products fixture (fixtures/products_api.py)
to already be listening, and FEDQUERY_DSN to point at a seeded database. Use
scripts/init_local.sh to prepare both.

    python -m fedquery.demo --audit products-requests.jsonl

Outputs, per query: the rows, the ON-vs-WHERE explanation, the actual PG
statements with params, the actual HTTP requests, and the provider audit log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .run import _print_result, _read_audit, _connect
from .engine import DEFAULT_BATCH_SIZE, ExecutionError, execute
from .query import QueryError, parse_query_file
from .sources import ProductsClient, PostgresOrders, SourceError, load_source_config

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(ROOT / "fixtures/source.json"))
    parser.add_argument("--queries", default=str(ROOT / "fixtures/queries.json"))
    parser.add_argument("--audit", default=str(ROOT / "products-requests.jsonl"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    dsn = os.environ.get("FEDQUERY_DSN")
    if not dsn:
        print("set FEDQUERY_DSN (see scripts/init_local.sh)", file=sys.stderr)
        return 2

    config = load_source_config(
        args.source, expected_schema="fed_input", expected_table="orders"
    )
    specs = parse_query_file(args.queries)
    audit = Path(args.audit)
    connection = _connect(dsn)
    payloads: list[dict] = []
    try:
        for spec in specs:
            before, _ = _read_audit(audit)
            orders_source = PostgresOrders(
                connection, config.postgres_schema, config.postgres_table
            )
            client = ProductsClient(config.http)
            result = execute(
                spec, orders_source=orders_source,
                products_client=client, batch_size=args.batch_size,
            )
            after, _ = _read_audit(audit)
            observed = after[len(before):]
            _print_result(result, observed)
            payload = result.as_dict()
            payload["explain"]["pushdown_evidence"]["observed_provider_log"] = observed
            payloads.append(payload)
    except (ExecutionError, SourceError, QueryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payloads, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON written to {args.json_out}")

    _print_explanation(payloads)
    return 0


def _print_explanation(payloads: list[dict]) -> None:
    by_name = {p["query"]: p for p in payloads}
    if "filter_in_on" in by_name and "filter_in_where" in by_name:
        on = by_name["filter_in_on"]["explain"]["stage_rows"]
        wh = by_name["filter_in_where"]["explain"]["stage_rows"]
        print("\n=== Why ON and WHERE retain different rows ===")
        print(
            f"  ON:    joined_rows={on['joined_rows']} -> output_rows={on['output_rows']} "
            f"(no local post-filter)"
        )
        print(
            f"  WHERE: joined_rows={wh['joined_rows']} -> "
            f"where_category_removed={wh['where_category_removed']} -> "
            f"output_rows={wh['output_rows']}"
        )
        print(
            "  ON keeps every left order once per matching food product and pads a "
            "NULL right row when no product matches the ON condition (including "
            "category mismatch and NULL join key)."
        )
        print(
            "  WHERE first forms the same LEFT join, then requires category=food to "
            "be true in the joined row; padded/NULL rows fail that test and are "
            "removed, so only orders with a real food match survive."
        )


if __name__ == "__main__":
    raise SystemExit(main())
