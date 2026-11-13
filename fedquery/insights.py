"""Execution trace: per-stage row counts, pushdown audit and the join explanation."""
from __future__ import annotations

from typing import Any

from .query import QuerySpec
from .sources import HttpStep, PgStep


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if not denominator else round(numerator / denominator, 4)


def build_insight(
    spec: QuerySpec,
    *,
    pg_steps: list[PgStep],
    http_steps: list[HttpStep],
    orders_fetched: int,
    distinct_keys: int,
    products_fetched: int,
    joined_rows: int,
    output_rows: int,
    sent_ids: set[int],
    returned_keys: set[int],
    duplicate_keys: list[int],
    product_rows_by_key: dict[int, int],
) -> dict[str, Any]:
    total_sent = sum(len(step.requested_ids) for step in http_steps)
    batch_size = max((len(step.requested_ids) for step in http_steps), default=None)
    where_removed = joined_rows - output_rows if spec.category_in_where else None

    if spec.category_in_on:
        category_reason = (
            "category is part of ON; filtering it at the provider cannot change "
            "LEFT-JOIN survival because rejected rows simply do not match"
        )
        category_pushdown = True
    elif spec.category_in_where:
        category_reason = (
            "category is a WHERE predicate; it must run after the join, otherwise "
            "LEFT-JOIN rows that should survive as unmatched would be dropped"
        )
        category_pushdown = False
    else:
        category_reason = "query declares no category predicate"
        category_pushdown = False

    return {
        "name": spec.name,
        "plan": {
            "orders_region_filter": {
                "source": "postgres",
                "pushdown": True,
                "predicate": f"orders.region = {spec.orders_region!r}",
            },
            "products_category": {
                "predicate": (
                    f"products.category = {spec.product_category!r}"
                    if spec.has_category
                    else None
                ),
                "placement": spec.filter_placement if spec.has_category else None,
                "remote_pushdown": category_pushdown,
                "reason": category_reason,
                "local_recheck": spec.has_category,
            },
            "projection": {
                "orders_columns": spec.orders_columns,
                "product_fields": spec.product_columns,
            },
            "keys": {
                "join": spec.join,
                "key_pair": list(spec.join_key_pair),
                "batch_size": batch_size,
                "requests": len(http_steps),
                "distinct_keys": distinct_keys,
                "keys_sent_total": total_sent,
                "each_key_requested_once": total_sent == distinct_keys,
                "null_keys_requested": False,
            },
        },
        "stage_rows": {
            "orders_selected_by_pg": orders_fetched,
            "distinct_join_keys": distinct_keys,
            "products_rows_fetched": products_fetched,
            "joined_rows": joined_rows,
            "where_category_removed": where_removed,
            "output_rows": output_rows,
        },
        "pushdown_evidence": {
            "postgres_steps": [step.as_dict() for step in pg_steps],
            "http_steps": [step.as_dict() for step in http_steps],
            "no_unbounded_catalogue_scan": all(s.requested_ids for s in http_steps),
            "requested_keys_not_needed": sorted(sent_ids - returned_keys),
            "requested_key_waste_ratio": _ratio(len(sent_ids - returned_keys), len(sent_ids)),
            "duplicate_id_keys": duplicate_keys,
            "product_rows_per_key": {
                str(k): v for k, v in sorted(product_rows_by_key.items())
            },
        },
        "semantics": {
            "duplicate_rows_preserved": True,
            "null_join_keys_match": False,
            "left_join_pads_nulls": spec.join == "LEFT",
            "where_null_compares_true": False,
            "join_key": "dict[int, list[ProductRow]] buckets; no keys overwritten",
        },
    }
