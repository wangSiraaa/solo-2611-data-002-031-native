"""Integration tests: real PostgreSQL + an in-process provider with an audit log.

Skipped unless FEDQUERY_DSN points at a database seeded with fixtures/orders.sql.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fedquery.engine import DEFAULT_BATCH_SIZE, execute
from fedquery.query import QuerySpec, parse_query_file
from fedquery.sources import HttpConfig, ProductsClient, PostgresOrders

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
DSN = os.environ.get("FEDQUERY_DSN")
PRODUCTS = json.loads((FIXTURES / "products.json").read_text(encoding="utf-8"))


class _Provider:
    """Minimal faithful mirror of fixtures/products_api.py with an audit log."""

    def __init__(self, rows):
        self.rows = rows
        self.requests: list[dict] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query, keep_blank_values=True)
                fields = q.get("fields", ["id,name,category"])[0].split(",")
                selected = provider.rows
                if "ids" in q:
                    ids = {int(x) for x in q["ids"][0].split(",") if x}
                    selected = [r for r in selected if r["id"] in ids]
                if "category" in q:
                    selected = [r for r in selected if r["category"] == q["category"][0]]
                result = [{k: r[k] for k in fields} for r in selected]
                provider.requests.append({"query": q, "returned_rows": len(result)})
                body = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _client(provider: _Provider) -> ProductsClient:
    return ProductsClient(
        HttpConfig(
            origin=f"http://127.0.0.1:{provider.port}",
            path="/products",
            capabilities={"ids_in": True, "category_eq": True, "projection": True},
        )
    )


def _spec(name: str) -> QuerySpec:
    specs = {s.name: s for s in parse_query_file(FIXTURES / "queries.json")}
    return specs[name]


@unittest.skipUnless(DSN, "set FEDQUERY_DSN to run integration tests")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.conn = psycopg.connect(DSN)

    def tearDown(self):
        self.conn.close()

    def _run(self, name: str, *, batch_size: int = DEFAULT_BATCH_SIZE):
        with _Provider(PRODUCTS) as provider:
            orders_source = PostgresOrders(self.conn, "fed_input", "orders")
            client = _client(provider)
            result = execute(
                _spec(name), orders_source=orders_source,
                products_client=client, batch_size=batch_size,
            )
            return result, provider.requests

    def test_filter_in_on_keeps_unmatched_orders(self):
        result, requests = self._run("filter_in_on")
        self.assertEqual(len(result.rows), 8)
        self.assertEqual(len(requests), 1)
        self.assertIn("category", requests[0]["query"])
        # Duplicate id=10 produces two rows for both order 1 and order 6.
        self.assertEqual([r["orders.id"] for r in result.rows].count(1), 2)
        self.assertEqual([r["orders.id"] for r in result.rows].count(6), 2)
        # Decimal amounts are exact strings, not floats.
        self.assertIsInstance(result.rows[0]["orders.amount"], str)
        self.assertEqual(result.rows[0]["orders.amount"], "18.00")

    def test_filter_in_where_removes_unmatched(self):
        result, requests = self._run("filter_in_where")
        self.assertEqual(len(result.rows), 4)
        # The WHERE placement must not push category remotely.
        self.assertNotIn("category", requests[0]["query"])

    def test_placement_changes_row_count(self):
        on_result, _ = self._run("filter_in_on")
        where_result, _ = self._run("filter_in_where")
        self.assertGreater(len(on_result.rows), len(where_result.rows))

    def test_empty_result_set_makes_no_catalogue_request(self):
        with _Provider(PRODUCTS) as provider:
            orders_source = PostgresOrders(self.conn, "fed_input", "orders")
            client = _client(provider)
            spec = QuerySpec(
                name="empty", orders_region="nowhere", join="LEFT",
                product_category=None, filter_placement="ON",
                select=("orders.id",),
            )
            result = execute(spec, orders_source=orders_source, products_client=client)
            self.assertEqual(result.rows, [])
            self.assertEqual(provider.requests, [])
            self.assertEqual(result.insight["plan"]["keys"]["requests"], 0)

    def test_batching_sends_each_key_once_in_small_batches(self):
        # Add high-cardinality north orders with distinct keys to force batches.
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fed_input.orders VALUES "
                + ",".join(
                    f"({1000+i},{2000+i},'batchtest',1.00)" for i in range(7)
                )
                + " ON CONFLICT (id) DO NOTHING"
            )
        self.conn.commit()
        try:
            with _Provider(PRODUCTS) as provider:
                orders_source = PostgresOrders(self.conn, "fed_input", "orders")
                client = _client(provider)
                spec = QuerySpec(
                    name="batch", orders_region="batchtest", join="INNER",
                    product_category=None, filter_placement="ON",
                    select=("orders.id",),
                )
                result = execute(spec, orders_source=orders_source,
                                 products_client=client, batch_size=2)
                # 7 distinct keys, batch size 2 => 4 requests.
                self.assertEqual(len(provider.requests), 4)
                sent = [i for req in provider.requests
                        for i in req["query"]["ids"][0].split(",") if i]
                self.assertEqual(len(sent), 7)
                self.assertEqual(len(set(sent)), 7)  # no duplicate key requests
                self.assertEqual(result.insight["plan"]["keys"]["batch_size"], 2)
        finally:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM fed_input.orders WHERE region='batchtest'")
            self.conn.commit()

    def test_null_and_duplicate_cases_exact(self):
        result, _ = self._run("inner")
        self.assertEqual(result.rows, [])  # south order 5 -> key 99 unknown

    def test_pg_statement_is_parameterized(self):
        result, _ = self._run("filter_in_on")
        pg = result.pg_steps[0]
        self.assertIn("%s", pg.sql)
        self.assertEqual(pg.params, ["north"])
        self.assertNotIn("north", pg.sql)

    def test_where_null_category_excluded(self):
        # order 3 joins product 30 (category NULL); with WHERE food it must go.
        result, _ = self._run("filter_in_where")
        self.assertNotIn(3, [r["orders.id"] for r in result.rows])

    def _oracle(self, spec: QuerySpec, products: list[dict]) -> list[tuple]:
        """Single-node PostgreSQL equivalent with products loaded as a temp table."""
        from psycopg.rows import tuple_row

        with self.conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS products "
                "(id bigint, name text, category text) ON COMMIT DROP"
            )
            cur.execute("TRUNCATE products")
            cur.executemany(
                "INSERT INTO products VALUES (%s, %s, %s)",
                [(p["id"], p["name"], p["category"]) for p in products],
            )
            join_kw = "LEFT JOIN" if spec.join == "LEFT" else "JOIN"
            on = ["products.id = orders.product_id"]
            where = ["orders.region = %s"]
            on_params, where_params = [], [spec.orders_region]
            if spec.category_in_on:
                on.append("products.category = %s")
                on_params.append(spec.product_category)
            if spec.category_in_where:
                where.append("products.category = %s")
                where_params.append(spec.product_category)
            projection = ", ".join(
                ("orders" if c.startswith("orders.") else "products")
                + "." + c.split(".", 1)[1]
                for c in spec.select
            )
            sql = (
                f"SELECT {projection} FROM fed_input.orders AS orders {join_kw} "
                f"products ON {' AND '.join(on)} WHERE {' AND '.join(where)} "
                f"ORDER BY orders.id"
            )
            cur.execute(sql, [*on_params, *where_params])
            return cur.fetchall()

    def test_matches_single_node_oracle(self):
        for name in ("filter_in_on", "filter_in_where", "inner"):
            spec = _spec(name)
            result, _ = self._run(name)
            expected = self._oracle(spec, PRODUCTS)
            actual = [tuple(r[c] for c in spec.select) for r in result.rows]
            # Executor emits decimals as exact strings; PostgreSQL returns Decimal.
            actual_norm = [
                tuple(
                    str(v) if hasattr(v, "as_tuple") else v for v in row
                )
                for row in actual
            ]
            expected_norm = [
                tuple(
                    str(v) if hasattr(v, "as_tuple") else v for v in row
                )
                for row in expected
            ]
            self.assertEqual(actual_norm, expected_norm, f"mismatch for {name}")


if __name__ == "__main__":
    unittest.main()
