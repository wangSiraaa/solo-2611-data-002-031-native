"""迁移运行与只读查询 / 对账仓储."""
from __future__ import annotations

from pathlib import Path

from .validation import validate_stock_row

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def run_migrations(conn) -> list[str]:
    """按文件名顺序执行 migrations/ 下的 SQL, 返回已执行文件名."""
    applied: list[str] = []
    conn.autocommit = False
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        conn.commit()
        applied.append(path.name)
    return applied


def load_initial_stock(conn, rows: list[tuple[str, int]], *, upsert: bool = False) -> int:
    """初始化库存.

    默认仅在空库存表上导入; upsert=True 时按 sku 覆盖 on_hand 并清零预占
    (仅用于重新初始化, 要求不存在任何订单流水).
    """
    clean: list[tuple[str, int]] = []
    seen: set[str] = set()
    for sku, qty in rows:
        sku, qty = validate_stock_row(sku, qty)
        if sku in seen:
            raise ValueError(f"CSV 中 SKU 重复: {sku}")
        seen.add(sku)
        clean.append((sku, qty))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock")
        existing = cur.fetchone()[0]
        if existing and not upsert:
            raise RuntimeError(
                "库存表已存在数据; 如需重新初始化请显式使用 --reset "
                "(会清空订单/流水/请求台账)"
            )
        if upsert:
            cur.execute("SELECT count(*) FROM stock_ledger WHERE reason <> 'INIT'")
            if cur.fetchone()[0]:
                raise RuntimeError("已存在业务流水, 拒绝重置; 请使用独立空库重新初始化")
            # 重置: 连同 INIT 基线一起重建
            cur.execute("TRUNCATE TABLE stock_ledger RESTART IDENTITY")
            cur.executemany(
                "INSERT INTO stock (sku, on_hand, reserved) VALUES (%s, %s, 0) "
                "ON CONFLICT (sku) DO UPDATE SET on_hand = EXCLUDED.on_hand, "
                "reserved = 0, updated_at = now()",
                clean,
            )
        else:
            cur.executemany(
                "INSERT INTO stock (sku, on_hand, reserved) VALUES (%s, %s, 0)",
                clean,
            )
        # 初始库存作为 INIT 流水落账, 使流水之和恒等于当前库存.
        cur.executemany(
            """
            INSERT INTO stock_ledger
                (request_id, order_id, sku, qty, delta_on_hand, delta_reserved, reason)
            VALUES ('__init__', '__init__', %s, %s, %s, 0, 'INIT')
            """,
            [(sku, qty, qty) for sku, qty in clean],
        )
    conn.commit()
    return len(clean)


def reset_all(conn) -> None:
    """清空全部业务表 (仅测试 / 重新初始化使用)."""
    with conn.cursor() as cur:
        for table in (
            "stock_ledger", "order_items", "order_records",
            "request_records", "stock",
        ):
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
    conn.commit()


def get_order(conn, order_id: str) -> dict | None:
    """查询订单状态、明细与关联请求."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT order_id, status, created_at, updated_at "
            "FROM order_records WHERE order_id = %s",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT sku, qty FROM order_items WHERE order_id = %s ORDER BY sku",
            (order_id,),
        )
        items = [{"sku": sku, "qty": qty} for sku, qty in cur.fetchall()]
        cur.execute(
            "SELECT request_id, action, status, result_code, message, created_at "
            "FROM request_records WHERE order_id = %s "
            "ORDER BY created_at, request_id",
            (order_id,),
        )
        requests = [
            {
                "request_id": r[0], "action": r[1], "status": r[2],
                "result_code": r[3], "message": r[4], "created_at": r[5].isoformat(),
            }
            for r in cur.fetchall()
        ]
    return {
        "order_id": row[0],
        "status": row[1],
        "created_at": row[2].isoformat(),
        "updated_at": row[3].isoformat(),
        "items": items,
        "requests": requests,
    }


def list_stock(conn) -> list[dict]:
    """按 SKU 排序返回库存、预占、可用量."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sku, on_hand, reserved, on_hand - reserved AS available "
            "FROM stock ORDER BY sku"
        )
        return [
            {"sku": r[0], "on_hand": r[1], "reserved": r[2], "available": r[3]}
            for r in cur.fetchall()
        ]


def reconcile(conn) -> list[dict]:
    """对账: 表值 vs 流水重算 vs RESERVED 订单之和, 按 SKU 排序."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM inventory_reconciliation()")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_requests(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, order_id, action, status, result_code, message, "
            "content_hash, created_at FROM request_records "
            "ORDER BY created_at, request_id"
        )
        return [
            {
                "request_id": r[0], "order_id": r[1], "action": r[2],
                "status": r[3], "result_code": r[4], "message": r[5],
                "content_hash": r[6], "created_at": r[7].isoformat(),
            }
            for r in cur.fetchall()
        ]


def list_ledger(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ledger_id, request_id, order_id, sku, qty, "
            "delta_on_hand, delta_reserved, reason, created_at "
            "FROM stock_ledger ORDER BY ledger_id"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return rows
