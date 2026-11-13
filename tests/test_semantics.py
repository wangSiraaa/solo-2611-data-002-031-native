"""Tests for the restricted two-source executor.

Unit tests exercise the join/filter semantics with in-memory data and need no
server. Integration tests spin up the fixture provider in-process and use the
local PostgreSQL instance when FEDQUERY_DSN is set; they otherwise skip.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from fedquery.model import (
    OrderRow,
    ProductRow,
    apply_where_category,
    join_rows,
    serialize_value,
)
from fedquery.query import QueryError, QuerySpec, parse_query_file
from fedquery.sources import load_source_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def make_spec(**overrides) -> QuerySpec:
    base = dict(
        name="t",
        orders_region="north",
        join="LEFT",
        product_category="food",
        filter_placement="ON",
        select=("orders.id", "products.name"),
    )
    base.update(overrides)
    return QuerySpec(**base)


def orders():
    return [
        OrderRow(1, 10, "north", Decimal("18.00")),
        OrderRow(2, 20, "north", Decimal("30.00")),
        OrderRow(4, None, "north", Decimal("12.00")),
        OrderRow(6, 10, "north", Decimal("2.00")),
    ]


def buckets():
    return {
        10: [
            ProductRow(10, "茶", "food"),
            ProductRow(10, "茶-旧包装", "food"),
        ],
        20: [ProductRow(20, "杯", "home")],
    }


class JoinSemanticsTests(unittest.TestCase):
    def test_duplicate_right_rows_are_preserved(self):
        rows = list(join_rows(orders(), buckets(), "LEFT", on_category="food"))
        ids = [(r.orders.id, None if r.products is None else r.products.name) for r in rows]
        self.assertEqual(
            ids,
            [
                (1, "茶"),
                (1, "茶-旧包装"),
                (2, None),  # category mismatch => unmatched under ON
                (4, None),  # NULL key => unmatched
                (6, "茶"),
                (6, "茶-旧包装"),
            ],
        )

    def test_inner_join_drops_unmatched_and_null_keys(self):
        rows = list(join_rows(orders(), buckets(), "INNER", on_category=None))
        self.assertEqual([(r.orders.id, r.products.id) for r in rows],
                         [(1, 10), (1, 10), (2, 20), (6, 10), (6, 10)])
        self.assertTrue(all(r.products is not None for r in rows))

    def test_null_key_never_matches_null_id(self):
        orders_with_null = [OrderRow(4, None, "north", None)]
        right = {None: [ProductRow(None, "未登记", "food")]}  # defensive, should not occur
        rows = list(join_rows(orders_with_null, right, "LEFT", on_category=None))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].products)

    def test_where_filter_removes_padded_rows(self):
        joined = list(join_rows(orders(), buckets(), "LEFT", on_category=None))
        filtered = list(apply_where_category(joined, "food"))
        # food rows: order 1 x2, order 6 x2; order 2 has home, order 4 is NULL.
        self.assertEqual([(r.orders.id) for r in filtered], [1, 1, 6, 6])

    def test_where_null_category_is_not_true(self):
        joined = list(join_rows(
            [OrderRow(3, 30, "north", None)],
            {30: [ProductRow(30, "糖", None)]},
            "LEFT",
            on_category=None,
        ))
        self.assertEqual(list(apply_where_category(joined, "food")), [])


class PlacementDifferenceTests(unittest.TestCase):
    """The same input must yield different row counts for ON vs WHERE."""

    def _run(self, placement: str) -> list:
        spec = make_spec(filter_placement=placement)
        joined = list(join_rows(
            orders(), buckets(), spec.join,
            on_category=spec.product_category if spec.category_in_on else None,
        ))
        if spec.category_in_where:
            joined = list(apply_where_category(joined, spec.product_category))
        return [(r.orders.id, None if r.products is None else r.products.name)
                for r in joined]

    def test_on_keeps_unmatched_where_removes_them(self):
        on_rows = self._run("ON")
        where_rows = self._run("WHERE")
        self.assertEqual(len(on_rows), 6)
        self.assertEqual(len(where_rows), 4)
        # ON keeps padded rows for orders 2 and 4; WHERE drops them.
        self.assertEqual([r for r in on_rows if r[1] is None], [(2, None), (4, None)])
        self.assertTrue(all(r[1] is not None for r in where_rows))


class DecimalTests(unittest.TestCase):
    def test_decimal_is_exact_and_never_float(self):
        value = serialize_value("orders.amount", Decimal("18.10"))
        self.assertEqual(value, "18.10")
        self.assertIsInstance(value, str)
        # 0.1 + 0.2 style checks via the normalization path are covered in the
        # integration test where PostgreSQL returns numeric directly.

    def test_decimal_normalization_rejects_bool(self):
        from fedquery.model import normalize_scalar
        with self.assertRaises(Exception):
            normalize_scalar("orders.amount", True)


class QueryValidationTests(unittest.TestCase):
    def test_fixture_queries_parse(self):
        specs = parse_query_file(FIXTURES / "queries.json")
        self.assertEqual([s.name for s in specs], ["filter_in_on", "filter_in_where", "inner"])

    def test_unknown_field_is_rejected(self):
        spec = {
            "name": "bad",
            "orders_region": "north",
            "join": "LEFT",
            "product_category": None,
            "filter_placement": "ON",
            "select": ["orders.id"],
            "evil": 1,
        }
        with self.assertRaises(QueryError):
            from fedquery.query import _parse_one
            _parse_one(spec, 0)

    def test_unknown_column_is_rejected(self):
        with self.assertRaises(QueryError):
            from fedquery.query import _parse_one
            _parse_one({
                "name": "bad", "orders_region": "north", "join": "LEFT",
                "product_category": None, "filter_placement": "ON",
                "select": ["orders.password"],
            }, 0)

    def test_bad_join_and_placement_rejected(self):
        from fedquery.query import _parse_one
        for bad in (dict(join="RIGHT"), dict(filter_placement="HAVING")):
            payload = {
                "name": "bad", "orders_region": "north", "join": "LEFT",
                "product_category": None, "filter_placement": "ON",
                "select": ["orders.id"],
            }
            payload.update(bad)
            with self.assertRaises(QueryError):
                _parse_one(payload, 0)

    def test_unknown_relation_rejected(self):
        with self.assertRaises(QueryError):
            from fedquery.query import _parse_one
            _parse_one({
                "name": "bad", "orders_region": "north", "join": "LEFT",
                "product_category": None, "filter_placement": "ON",
                "select": ["customers.id"],
            }, 0)


class SourceConfigTests(unittest.TestCase):
    def test_fixture_config_matches_declared_types(self):
        cfg = load_source_config(
            FIXTURES / "source.json",
            expected_schema="fed_input", expected_table="orders",
        )
        self.assertEqual(cfg.postgres_schema, "fed_input")
        self.assertTrue(cfg.http.capabilities["ids_in"])

    def test_wrong_schema_rejected(self):
        from fedquery.sources import SourceError
        with self.assertRaises(SourceError):
            load_source_config(FIXTURES / "source.json", expected_schema="other")
