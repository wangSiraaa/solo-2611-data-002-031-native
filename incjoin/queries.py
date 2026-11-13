"""只读查询：源表、连接明细、汇总、批次回执。"""

from __future__ import annotations

from typing import Any

from .db import connect


def _rows(cur, sql_text: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur.execute(sql_text, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_detail(dsn: str | None, key: str | None = None) -> list[dict]:
    with connect(dsn) as conn, conn.cursor() as cur:
        if key is None:
            return _rows(
                cur,
                "SELECT l_row_id, r_row_id, join_key, qty, unit_cents, "
                "amount_cents, last_changed_batch FROM join_detail "
                "ORDER BY join_key, l_row_id, r_row_id",
            )
        return _rows(
            cur,
            "SELECT l_row_id, r_row_id, join_key, qty, unit_cents, "
            "amount_cents, last_changed_batch FROM join_detail "
            "WHERE join_key = %s ORDER BY l_row_id, r_row_id",
            (key,),
        )


def query_summary(dsn: str | None, key: str | None = None) -> list[dict]:
    with connect(dsn) as conn, conn.cursor() as cur:
        if key is None:
            return _rows(
                cur,
                "SELECT join_key, pair_count, qty_total, amount_total_cents, "
                "last_changed_batch FROM key_summary "
                "ORDER BY join_key",
            )
        return _rows(
            cur,
            "SELECT join_key, pair_count, qty_total, amount_total_cents, "
            "last_changed_batch FROM key_summary WHERE join_key = %s",
            (key,),
        )


def query_source(dsn: str | None, side: str) -> list[dict]:
    table = "l_source" if side == "L" else "r_source"
    value_col = "qty" if side == "L" else "unit_cents"
    with connect(dsn) as conn, conn.cursor() as cur:
        return _rows(
            cur,
            f"SELECT row_id, join_key, {value_col} AS value, "
            "last_changed_batch FROM "
            f"{table} ORDER BY join_key, row_id",
        )


def query_receipts(dsn: str | None) -> list[dict]:
    with connect(dsn) as conn, conn.cursor() as cur:
        return _rows(
            cur,
            "SELECT batch_id, content_hash, affected_keys, op_count, status, "
            "applied_at FROM batch_receipt ORDER BY applied_at, batch_id",
        )
