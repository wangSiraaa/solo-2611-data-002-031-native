"""Orchestration: decide what is pushed where, fetch, join, filter, project."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .insights import build_insight
from .model import (
    MAX_BATCH_SIZE,
    ProductRow,
    apply_where_category,
    join_rows,
    project,
)
from .query import QuerySpec
from .sources import HttpStep, PgStep, ProductsClient, PostgresOrders


class ExecutionError(Exception):
    """Raised when an execution-time precondition fails."""


DEFAULT_BATCH_SIZE = 32


def key_batches(keys: list[int], batch_size: int) -> list[list[int]]:
    if batch_size < 1:
        raise ExecutionError("batch size must be >= 1")
    if batch_size > MAX_BATCH_SIZE:
        raise ExecutionError(f"batch size must be <= {MAX_BATCH_SIZE}")
    return [keys[i : i + batch_size] for i in range(0, len(keys), batch_size)]


@dataclass
class ExecutionResult:
    name: str
    rows: list[dict[str, Any]]
    insight: dict[str, Any]
    pg_steps: list[PgStep] = field(default_factory=list)
    http_steps: list[HttpStep] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.name, "rows": self.rows, "explain": self.insight}


def execute(
    spec: QuerySpec,
    *,
    orders_source: PostgresOrders,
    products_client: ProductsClient,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ExecutionResult:
    """Run one query with pushdown-first planning and exact join semantics.

    Pushdown decisions:
      * orders.region              -> always pushed to PostgreSQL (parameterized).
      * products projection        -> always requested via `fields`.
      * products.category (ON)     -> pushed to the provider's `category` filter.
      * products.category (WHERE)  -> NOT pushed; evaluated after the join.
      * join keys                  -> deduplicated and sent in small batches.
    """
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ExecutionError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}, got {batch_size}"
        )
    if spec.category_in_on and not products_client.capabilities.get("category_eq", False):
        raise ExecutionError("category_eq capability is required for an ON pushdown")

    # 1. PostgreSQL: region predicate + declared projection, parameterized.
    orders = orders_source.fetch_orders(
        region=spec.orders_region, columns=spec.orders_columns
    )

    # 2. Collect only real join keys. NULL keys can never match, so they are
    #    never transmitted; they re-enter as unmatched LEFT rows later.
    keys: list[int] = []
    seen: set[int] = set()
    for order in orders:
        if order.product_id is not None and order.product_id not in seen:
            seen.add(order.product_id)
            keys.append(order.product_id)
    keys.sort()

    # 3. Batch requests. Zero valid keys => zero catalogue requests.
    remote_category = spec.product_category if spec.category_in_on else None
    products_by_key: dict[int, list[ProductRow]] = {}
    for batch in key_batches(keys, batch_size):
        products = products_client.fetch(
            ids=batch,
            columns=spec.product_columns,
            category=remote_category,
        )
        for product in products:
            if product.id is None:
                # A NULL right key matches nothing; keep it out of the buckets.
                continue
            products_by_key.setdefault(product.id, []).append(product)

    # 4. Local join, preserving duplicate right rows; NULL keys never match.
    joined = list(
        join_rows(
            orders,
            products_by_key,
            spec.join,
            on_category=spec.product_category if spec.category_in_on else None,
        )
    )

    # 5. WHERE category evaluates on the join output with SQL 3-valued logic.
    if spec.category_in_where:
        final_rows = list(apply_where_category(joined, spec.product_category))
    else:
        final_rows = joined

    # 6. Projection is last, against the real selectivity.
    rows = [project(row, list(spec.select)) for row in final_rows]

    sent_ids = {i for step in products_client.steps for i in step.requested_ids}
    duplicate_keys = sorted(k for k, v in products_by_key.items() if len(v) > 1)
    insight = build_insight(
        spec,
        pg_steps=orders_source.steps,
        http_steps=products_client.steps,
        orders_fetched=len(orders),
        distinct_keys=len(keys),
        # Actual rows returned by the provider, including none usable (NULL id).
        products_fetched=sum(step.returned_rows for step in products_client.steps),
        joined_rows=len(joined),
        output_rows=len(final_rows),
        sent_ids=sent_ids,
        returned_keys=set(products_by_key),
        duplicate_keys=duplicate_keys,
        product_rows_by_key={k: len(v) for k, v in products_by_key.items()},
    )

    return ExecutionResult(
        name=spec.name,
        rows=rows,
        insight=insight,
        pg_steps=list(orders_source.steps),
        http_steps=list(products_client.steps),
    )
