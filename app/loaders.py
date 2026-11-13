"""Request-scoped batch loading for Customer.orders.

Design notes
------------
Loading key: ``(customer_id, first)`` — the alias used in the query and
the selected sub-fields are deliberately NOT part of the key. Two aliases
asking for the same ``first`` are the same load; the same alias with a
different ``first`` is a different load.

Batching: keys collected in one execution tick are grouped by ``first``.
Each distinct ``first`` value costs exactly ONE SQL query, regardless of
how many customers are requested — the per-parent limit is enforced in
SQL with ``row_number() OVER (PARTITION BY customer_id ...)`` instead of
a global ``LIMIT`` (a global LIMIT would let one customer's rows starve
the others).

Backfill: the batch function returns one result per input key, in the
same order, so each pending future gets exactly its own customer's rows.
Customers without orders get a real empty list — never confused with a
missing parent or a failed query.

Caching: the loader instance is created per request (see db.py), so
within one request identical keys share one future, and the next request
starts from an empty cache and sees newly committed rows.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Hashable

import psycopg
from graphql import GraphQLError

# One statement per distinct `first` value. The window function applies
# the limit per customer; the outer ORDER BY keeps backfill deterministic.
ORDERS_BATCH_SQL = """
SELECT id, customer_id, created_at, amount
FROM (
    SELECT id, customer_id, created_at, amount,
           row_number() OVER (
               PARTITION BY customer_id
               ORDER BY created_at DESC, id DESC
           ) AS rn
    FROM gql_input.orders
    WHERE customer_id = ANY(%s)
) AS ranked
WHERE rn <= %s
ORDER BY customer_id, created_at DESC, id DESC
"""


class BatchLoader:
    """Minimal DataLoader: per-key cache + one dispatch per event-loop tick.

    ``load()`` is synchronous and returns a Future. GraphQL-core calls all
    sibling field resolvers (across the whole customer list) in one
    synchronous pass before awaiting anything, so every key of that pass
    lands in a single dispatch.
    """

    def __init__(self, batch_fn: Callable[[list], Awaitable[list]]):
        self._batch_fn = batch_fn
        self._cache: dict[Hashable, asyncio.Future] = {}
        self._pending: list[tuple[Hashable, asyncio.Future]] = []
        self._dispatch_scheduled = False

    def load(self, key: Hashable) -> asyncio.Future:
        if key in self._cache:  # equivalent load within this request: reuse
            return self._cache[key]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._cache[key] = fut
        self._pending.append((key, fut))
        if not self._dispatch_scheduled:
            self._dispatch_scheduled = True
            loop.call_soon(self._dispatch)
        return fut

    def _dispatch(self) -> None:
        self._dispatch_scheduled = False
        pending, self._pending = self._pending, []
        asyncio.get_running_loop().create_task(self._resolve(pending))

    async def _resolve(self, pending: list[tuple[Hashable, asyncio.Future]]) -> None:
        keys = [key for key, _ in pending]
        try:
            values = await self._batch_fn(keys)
            if len(values) != len(keys):
                raise RuntimeError(
                    f"batch function returned {len(values)} results for {len(keys)} keys"
                )
        except Exception as exc:
            # A failed load must surface as a GraphQL error on every waiting
            # field — it must never be silently converted into empty lists.
            for _, fut in pending:
                if not fut.done():
                    fut.set_exception(exc)
        else:
            for (_, fut), value in zip(pending, values):
                if not fut.done():
                    fut.set_result(value)


def _format_ts(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.isoformat().replace("+00:00", "Z")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "createdAt": _format_ts(row["created_at"]),
        # numeric(12,2) arrives as Decimal; str() keeps the exact scale.
        "amount": str(row["amount"]),
    }


async def batch_load_orders(ctx, keys: list[tuple[int, int]]) -> list[list[dict]]:
    """Resolve [(customer_id, first), ...] -> [[order, ...], ...] (same order)."""
    by_first: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for index, (customer_id, first) in enumerate(keys):
        by_first[first].append((customer_id, index))

    results: list[Any] = [None] * len(keys)
    for first, group in sorted(by_first.items()):
        customer_ids = sorted({cid for cid, _ in group})
        try:
            rows = await ctx.fetch(ORDERS_BATCH_SQL, (customer_ids, first))
        except psycopg.Error as exc:
            raise GraphQLError(
                f"orders batch query failed (first={first}): {exc}"
            ) from exc
        buckets: dict[int, list[dict]] = defaultdict(list)
        for row in rows:
            buckets[row["customer_id"]].append(_to_order(row))
        for customer_id, index in group:
            # rn <= first already guarantees the limit; the slice is a
            # defensive re-check before the value reaches the response.
            results[index] = buckets.get(customer_id, [])[:first]
    return results
