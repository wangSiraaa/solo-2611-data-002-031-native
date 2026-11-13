"""GraphQL schema (fixed SDL from fixtures/schema.graphql) and resolvers."""
from __future__ import annotations

from pathlib import Path

import psycopg
from graphql import GraphQLError, build_schema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "schema.graphql"

CUSTOMERS_SQL = "SELECT id, name FROM gql_input.customers WHERE id = ANY(%s)"

BIGINT_MIN, BIGINT_MAX = -(2**63), 2**63 - 1


async def resolve_customers(_root, info, ids):
    """Existing customers, in first-occurrence order of the input ids.

    Duplicates are returned once; ids that do not exist (or can never
    exist as bigint) are ignored. One SQL query for the whole list.
    """
    ctx = info.context
    wanted: list[int] = []
    seen: set[int] = set()
    for raw in ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue  # not a bigint id -> cannot exist -> ignore
        if not BIGINT_MIN <= cid <= BIGINT_MAX:
            continue
        if cid not in seen:
            seen.add(cid)
            wanted.append(cid)
    if not wanted:
        return []
    try:
        rows = await ctx.fetch(CUSTOMERS_SQL, (wanted,))
    except psycopg.Error as exc:
        raise GraphQLError(f"customers query failed: {exc}") from exc
    by_id = {row["id"]: row for row in rows}
    return [
        {"id": by_id[cid]["id"], "name": by_id[cid]["name"]}
        for cid in wanted
        if cid in by_id
    ]


def resolve_customer_id(customer, _info):
    return str(customer["id"])


def resolve_orders(customer, info, first):
    """Return a Future for key (customer_id, first); the request-scoped
    BatchLoader turns all such keys of one tick into one SQL per distinct
    `first`. The field alias never influences the key."""
    if not isinstance(first, int) or isinstance(first, bool) or not 1 <= first <= 5:
        raise GraphQLError(
            f"orders(first:) expects an integer between 1 and 5, got {first!r}"
        )
    return info.context.orders_loader.load((customer["id"], first))


def build():
    schema = build_schema(SCHEMA_PATH.read_text(encoding="utf-8"))
    query = schema.type_map["Query"]
    customer = schema.type_map["Customer"]
    query.fields["customers"].resolve = resolve_customers
    customer.fields["id"].resolve = resolve_customer_id
    customer.fields["orders"].resolve = resolve_orders
    return schema


schema = build()
