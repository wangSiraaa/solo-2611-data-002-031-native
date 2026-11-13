"""Validation for the limited JSON query language.

Only the declared fields are accepted; anything else is rejected explicitly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .model import JOIN_KEY_PAIR, SchemaError, check_column, canonical_category

QUERY_KEYS = {
    "name",
    "orders_region",
    "join",
    "product_category",
    "filter_placement",
    "select",
}
REQUIRED_KEYS = {"orders_region", "join", "product_category", "filter_placement", "select"}
JOIN_TYPES = ("INNER", "LEFT")
PLACEMENTS = ("ON", "WHERE")


class QueryError(Exception):
    """Raised for malformed or unsupported query input."""


@dataclass(frozen=True)
class QuerySpec:
    name: str
    orders_region: str
    join: str
    product_category: Optional[str]
    filter_placement: str
    select: tuple[str, ...]
    join_key_pair: tuple[str, str] = JOIN_KEY_PAIR

    @property
    def has_category(self) -> bool:
        return self.product_category is not None

    @property
    def category_in_on(self) -> bool:
        return self.has_category and self.filter_placement == "ON"

    @property
    def category_in_where(self) -> bool:
        return self.has_category and self.filter_placement == "WHERE"

    @property
    def orders_columns(self) -> list[str]:
        """Order columns needed by PostgreSQL: select + keys + predicates."""
        cols = {c.split(".", 1)[1] for c in self.select if c.startswith("orders.")}
        cols.update({"id", "product_id"} | {"region"})
        if "orders.amount" in self.select:
            cols.add("amount")
        ordered = ["id", "product_id", "region"]
        for c in ("amount",):
            if c in cols and c not in ordered:
                ordered.append(c)
        return ordered

    @property
    def product_columns(self) -> list[str]:
        """Product columns needed from HTTP: select + keys (+ category when needed)."""
        cols = {c.split(".", 1)[1] for c in self.select if c.startswith("products.")}
        cols.add("id")
        if self.has_category:
            cols.add("category")
        return [c for c in ("id", "name", "category") if c in cols]


def _reject_unknown(obj: dict[str, Any], where: str) -> None:
    unknown = sorted(set(obj) - QUERY_KEYS)
    if unknown:
        raise QueryError(f"{where}: unsupported field(s): {', '.join(unknown)}")


def _parse_one(raw: Any, index: int) -> QuerySpec:
    if not isinstance(raw, dict):
        raise QueryError(f"query[{index}]: expected a JSON object")
    _reject_unknown(raw, f"query[{index}]")
    missing = sorted(REQUIRED_KEYS - set(raw))
    if missing:
        raise QueryError(f"query[{index}]: missing required field(s): {', '.join(missing)}")

    name = raw.get("name", f"query[{index}]")
    if not isinstance(name, str) or not name:
        raise QueryError(f"query[{index}]: name must be a non-empty string")

    region = raw["orders_region"]
    if not isinstance(region, str) or not region:
        raise QueryError(f"{name}: orders_region must be a non-empty string")

    join = raw["join"]
    if join not in JOIN_TYPES:
        raise QueryError(f"{name}: join must be one of {JOIN_TYPES}, got {join!r}")

    placement = raw["filter_placement"]
    if placement not in PLACEMENTS:
        raise QueryError(
            f"{name}: filter_placement must be one of {PLACEMENTS}, got {placement!r}"
        )

    category = raw["product_category"]
    if category is not None:
        category = canonical_category(category)

    select = raw["select"]
    if not isinstance(select, list) or not select:
        raise QueryError(f"{name}: select must be a non-empty list of column references")
    if len(set(map(str, select))) != len(select):
        raise QueryError(f"{name}: select contains duplicate column references")
    checked: list[str] = []
    for colref in select:
        try:
            checked.append(check_column(colref))
        except SchemaError as exc:
            raise QueryError(f"{name}: {exc}") from exc

    return QuerySpec(
        name=name,
        orders_region=region,
        join=join,
        product_category=category,
        filter_placement=placement,
        select=tuple(checked),
    )


def parse_query_file(path: Union[str, Path]) -> list[QuerySpec]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QueryError(f"query file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QueryError(f"invalid JSON in {path}: {exc}") from exc

    if isinstance(raw, dict):
        unknown = sorted(set(raw) - {"queries"})
        if unknown:
            raise QueryError(f"query file: unsupported top-level field(s): {', '.join(unknown)}")
        if "queries" not in raw:
            raise QueryError("query file: object form must contain a 'queries' list")
        raw = raw["queries"]
    if not isinstance(raw, list):
        raise QueryError("query file: expected a list of queries or an object with 'queries'")

    specs = [_parse_one(item, i) for i, item in enumerate(raw)]
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise QueryError("query names must be unique")
    return specs
