"""Typed values and order-preserving join semantics for the two allowed relations.

Types come from source.json and are declared statically here as an allow-list;
there is no SQL parser and no generic type inference.
"""
from __future__ import annotations

import decimal
from dataclasses import dataclass
from typing import Any, Iterable, Optional

DECIMAL_TYPE = "decimal"


class SchemaError(Exception):
    """Raised when a query/type/column is outside the supported schema."""


# (source, relation, column) -> declared type
DECLARED_TYPES = {
    ("orders", "orders", "id"): "int64",
    ("orders", "orders", "product_id"): "int64?",
    ("orders", "orders", "region"): "string",
    ("orders", "orders", "amount"): "decimal?",
    ("products", "products", "id"): "int64?",
    ("products", "products", "name"): "string",
    ("products", "products", "category"): "string?",
}

# Physical names are mapped through this allow-list, never interpolated from input.
COLUMN_SQL = {
    ("orders", "orders", "id"): "id",
    ("orders", "orders", "product_id"): "product_id",
    ("orders", "orders", "region"): "region",
    ("orders", "orders", "amount"): "amount",
}
COLUMN_HTTP = {
    ("products", "products", "id"): "id",
    ("products", "products", "name"): "name",
    ("products", "products", "category"): "category",
}

RELATION_SOURCE = {"orders": "orders", "products": "products"}

JOIN_KEY_PAIR = ("orders.product_id", "products.id")

MAX_BATCH_SIZE = 256


def is_decimal(colref: str) -> bool:
    return _type_of(colref).rstrip("?") == DECIMAL_TYPE


def _type_of(colref: str) -> str:
    relation, column = colref.split(".", 1)
    return DECLARED_TYPES[(RELATION_SOURCE[relation], relation, column)]


def check_column(colref: Any) -> str:
    if not isinstance(colref, str):
        raise SchemaError(f"column reference must be a string, got {colref!r}")
    if colref.count(".") != 1:
        raise SchemaError(f"unsupported column reference {colref!r}")
    relation, column = colref.split(".", 1)
    if relation not in RELATION_SOURCE:
        raise SchemaError(f"unknown relation in column reference {colref!r}")
    key = (RELATION_SOURCE[relation], relation, column)
    if key not in DECLARED_TYPES:
        raise SchemaError(f"unknown column {colref!r}")
    return colref


def relation_of(colref: str) -> str:
    return colref.split(".", 1)[0]


def normalize_key(value: Any) -> Optional[int]:
    """Canonicalize a value of declared type int64?/int64 for join equality."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaError("boolean is not a valid join key")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise SchemaError(f"invalid int64 join key value {value!r}")


def normalize_scalar(colref: str, value: Any) -> Any:
    """Normalize a SELECTed value according to the declared type."""
    typ = _type_of(colref).rstrip("?")
    if value is None:
        return None
    if typ == "int64":
        return normalize_key(value)
    if typ == "decimal":
        if isinstance(value, decimal.Decimal):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, str, float)):
            raise SchemaError(f"invalid decimal value {value!r}")
        # str() prevents any intermediate binary float for float inputs.
        return decimal.Decimal(str(value))
    if typ == "string":
        if not isinstance(value, str):
            raise SchemaError(f"invalid string value {value!r}")
        return value
    raise SchemaError(f"unhandled declared type {typ!r}")


def serialize_value(colref: str, value: Any) -> Any:
    """JSON-safe form, with decimals rendered exactly as strings."""
    if value is None:
        return None
    if is_decimal(colref):
        return format(value, "f")
    return value


def canonical_category(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"category predicate must be a string, got {value!r}")
    return value


@dataclass(frozen=True)
class OrderRow:
    id: int
    product_id: Optional[int]
    region: str
    amount: Optional[decimal.Decimal]


@dataclass(frozen=True)
class ProductRow:
    id: Optional[int]
    name: str
    category: Optional[str]


@dataclass(frozen=True)
class JoinedRow:
    orders: Optional[OrderRow]
    products: Optional[ProductRow]  # None only for an unmatched LEFT JOIN row


def product_row_from_payload(payload: dict) -> ProductRow:
    return ProductRow(
        id=normalize_key(payload["id"]) if "id" in payload else None,
        name=normalize_scalar("products.name", payload.get("name")),
        category=(
            normalize_scalar("products.category", payload["category"])
            if "category" in payload
            else None
        ),
    )


def product_row_to_payload(row: ProductRow) -> dict:
    out: dict[str, Any] = {"id": row.id, "name": row.name, "category": row.category}
    return out


def order_row_from_payload(payload: dict) -> OrderRow:
    return OrderRow(
        id=normalize_scalar("orders.id", payload["id"]),
        product_id=normalize_scalar("orders.product_id", payload.get("product_id")),
        region=normalize_scalar("orders.region", payload["region"]),
        amount=normalize_scalar("orders.amount", payload.get("amount")),
    )


def join_rows(
    orders: Iterable[OrderRow],
    products_by_key: dict[int, list[ProductRow]],
    join_type: str,
    *,
    on_category: Optional[str],
) -> Iterable[JoinedRow]:
    """Join orders to a key-bucketed product side, preserving duplicate right rows.

    products_by_key values are lists: one entry per physical product row, so a
    duplicate id yields one joined row per match. NULL join keys never match and
    are never sent, so a NULL-key order only survives under LEFT JOIN (right NULL).
    """
    if join_type not in ("INNER", "LEFT"):
        raise SchemaError(f"unsupported join type {join_type!r}")
    for order in orders:
        matches: list[ProductRow] = []
        key = order.product_id
        if key is not None:
            for product in products_by_key.get(key, ()):
                if on_category is not None and product.category != on_category:
                    continue  # belt-and-braces; remote ON filter normally applied already
                matches.append(product)
        if matches:
            for product in matches:
                yield JoinedRow(order, product)
        elif join_type == "LEFT":
            yield JoinedRow(order, None)


def apply_where_category(rows: Iterable[JoinedRow], category: str) -> Iterable[JoinedRow]:
    """WHERE category = v: three-valued logic -- NULL comparison is never true."""
    for row in rows:
        if row.products is not None and row.products.category == category:
            yield row


def project(row: JoinedRow, select: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for colref in select:
        source_row = row.orders if relation_of(colref) == "orders" else row.products
        value = None if source_row is None else getattr(
            source_row, colref.split(".", 1)[1]
        )
        out[colref] = serialize_value(colref, value)
    return out
