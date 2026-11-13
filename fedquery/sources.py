"""Source runtimes: parameterized PostgreSQL access and the HTTP catalogue client."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

from .model import (
    COLUMN_HTTP,
    COLUMN_SQL,
    MAX_BATCH_SIZE,
    OrderRow,
    ProductRow,
    SchemaError,
    normalize_key,
    product_row_from_payload,
)


class SourceError(Exception):
    """Raised for failures talking to either source."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class HttpConfig:
    origin: str
    path: str
    capabilities: dict[str, bool]

    @property
    def base_url(self) -> str:
        return self.origin.rstrip("/") + self.path


@dataclass(frozen=True)
class SourceConfig:
    postgres_schema: str
    postgres_table: str
    http: HttpConfig
    declared_types: dict[str, str]


def load_source_config(path, *, expected_schema: Optional[str] = None,
                       expected_table: Optional[str] = None) -> SourceConfig:
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError as exc:
        raise SourceError(f"source config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"invalid JSON in source config {path}: {exc}") from exc

    allowed_top = {
        "postgres_schema", "postgres_table", "http_origin", "http_path",
        "capabilities", "types",
    }
    unknown = sorted(set(raw) - allowed_top)
    if unknown:
        raise SourceError(f"source config: unsupported field(s): {', '.join(unknown)}")
    for key in ("postgres_schema", "postgres_table", "http_origin", "http_path",
                "capabilities", "types"):
        if key not in raw:
            raise SourceError(f"source config: missing field {key!r}")

    schema = raw["postgres_schema"]
    table = raw["postgres_table"]
    if expected_schema is not None and schema != expected_schema:
        raise SourceError(
            f"source config declares schema {schema!r}, executor only supports "
            f"{expected_schema!r}"
        )
    if expected_table is not None and table != expected_table:
        raise SourceError(
            f"source config declares table {table!r}, executor only supports "
            f"{expected_table!r}"
        )
    if not isinstance(schema, str) or not schema or not isinstance(table, str) or not table:
        raise SourceError("source config: schema/table must be non-empty strings")

    caps = raw["capabilities"]
    if not isinstance(caps, dict):
        raise SourceError("source config: capabilities must be an object")
    allowed_caps = {"ids_in", "category_eq", "projection"}
    unknown_caps = sorted(set(caps) - allowed_caps)
    if unknown_caps:
        raise SourceError(f"source config: unsupported capability: {', '.join(unknown_caps)}")
    for cap in allowed_caps:
        if not isinstance(caps.get(cap), bool):
            raise SourceError(f"source config: capability {cap!r} must be a boolean")

    types = raw["types"]
    if not isinstance(types, dict):
        raise SourceError("source config: types must be an object")
    from .model import DECLARED_TYPES

    expected_types = {
        f"{relation}.{column}": typ
        for (_, relation, column), typ in DECLARED_TYPES.items()
    }
    unknown_types = sorted(set(types) - set(expected_types))
    if unknown_types:
        raise SourceError(
            f"source config: unsupported declared type for: {', '.join(unknown_types)}"
        )
    for colref, typ in types.items():
        if expected_types[colref] != typ:
            raise SourceError(
                f"source config: declared type {colref}={typ!r} does not match the "
                f"executor's supported type {expected_types[colref]!r}"
            )

    return SourceConfig(
        postgres_schema=schema,
        postgres_table=table,
        http=HttpConfig(raw["http_origin"], raw["http_path"], dict(caps)),
        declared_types=dict(types),
    )


# ------------------------------------------------------------------------ postgres


@dataclass
class PgStep:
    stage: str
    sql: str
    params: list[Any]
    rows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "sql": self.sql,
            "params": list(self.params),
            "rows": self.rows,
        }


class PostgresOrders:
    """Parameterized access to the single allowed orders relation."""

    def __init__(self, connection: Any, schema: str, table: str) -> None:
        self._conn = connection
        self._schema = schema
        self._table = table
        self._prefix = f'"{schema}"."{table}"'
        self.steps: list[PgStep] = []

    def _qualified(self, relation: str, column: str) -> str:
        if relation != "orders":
            raise SchemaError(f"relation {relation!r} is not served by PostgreSQL here")
        return f'{self._prefix}."{COLUMN_SQL[("orders", relation, column)]}"'

    def fetch_orders(self, *, region: str, columns: list[str]) -> list[OrderRow]:
        projection = ", ".join(
            f'{self._qualified("orders", c)} AS "orders.{c}"' for c in columns
        )
        sql = (
            f"SELECT {projection} FROM {self._prefix} "
            f'WHERE {self._qualified("orders", "region")} = %s '
            f'ORDER BY {self._qualified("orders", "id")}'
        )
        params = [region]
        rows = self._execute(sql, params, stage="orders_filter_pushdown")
        return [self._to_order(row) for row in rows]

    def _to_order(self, pg_row: dict[str, Any]) -> OrderRow:
        return OrderRow(
            id=normalize_key(pg_row["orders.id"]),
            product_id=normalize_key(pg_row.get("orders.product_id")),
            region=pg_row["orders.region"],
            amount=pg_row.get("orders.amount"),
        )

    def _execute(self, sql: str, params: list[Any], *, stage: str) -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        with self._conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, params)
            except psycopg.Error as exc:
                raise SourceError(f"PostgreSQL query failed: {exc}") from exc
            result = cur.fetchall()
        self.steps.append(PgStep(stage=stage, sql=sql, params=list(params), rows=len(result)))
        return result


# ---------------------------------------------------------------------------- http


@dataclass
class HttpStep:
    stage: str
    url: str
    query: dict[str, str]
    requested_ids: list[int] = field(default_factory=list)
    returned_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "url": self.url,
            "query": dict(self.query),
            "requested_ids": list(self.requested_ids),
            "returned_rows": self.returned_rows,
        }


class ProductsClient:
    """HTTP catalogue client that only requests the columns/keys it needs."""

    def __init__(self, config: HttpConfig, *, timeout: float = 10.0) -> None:
        self._config = config
        self._timeout = timeout
        self.steps: list[HttpStep] = []

    def _fields_for(self, columns: list[str]) -> str:
        mapped = [COLUMN_HTTP[("products", "products", c)] for c in columns]
        # The fixture applies projection last; providers accept any order, but
        # emitting a stable id-first order keeps requests reproducible.
        ordered = [c for c in ("id", "name", "category") if c in mapped]
        return ",".join(ordered)

    @property
    def capabilities(self) -> dict[str, bool]:
        return dict(self._config.capabilities)

    def fetch(self, *, ids: list[int], columns: list[str],
              category: Optional[str]) -> list[ProductRow]:
        if not ids:
            raise SourceError("refusing an unbounded products request (no ids)")
        if len(ids) > MAX_BATCH_SIZE:
            raise SourceError(
                f"batch of {len(ids)} exceeds the configured maximum {MAX_BATCH_SIZE}"
            )
        if not self._config.capabilities.get("ids_in", False):
            raise SourceError("provider does not advertise the ids_in capability")
        params = {"ids": ",".join(str(i) for i in sorted(ids))}
        if category is not None:
            if not self._config.capabilities.get("category_eq", False):
                raise SourceError(
                    "provider does not advertise category_eq; cannot push it down"
                )
            params["category"] = category
        if self._config.capabilities.get("projection", False):
            params["fields"] = self._fields_for(columns)
        query = urlencode(params)
        url = f"{self._config.base_url}?{query}"
        payload = self._get_json(url)
        if not isinstance(payload, list):
            raise SourceError(f"products provider returned non-list JSON: {payload!r}")
        products = [product_row_from_payload(p) for p in payload]
        self.steps.append(
            HttpStep(
                stage="products_batch",
                url=url,
                query=params,
                requested_ids=sorted(ids),
                returned_rows=len(products),
            )
        )
        return products

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise SourceError(f"products provider HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError(f"products provider request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SourceError(f"products provider returned invalid JSON: {exc}") from exc
