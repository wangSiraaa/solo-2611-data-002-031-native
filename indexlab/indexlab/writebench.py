"""写入代价测量：同一初始模板副本 + 相同操作流，每个组合独立克隆测量。"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .candidates import Candidate
from .config import Config

WRITE_TABLES = ("orders", "order_items")


def gen_write_ops(cfg: Config, prices: dict[int, float]) -> list[dict]:
    """固定操作流：插入订单(含明细) + 更新订单状态。固定 seed，存档复用。"""
    rng = random.Random(cfg.seed + 7)
    s = cfg.scale
    max_oid = s["orders"]
    n_cust = s["customers"]
    prod_ids = sorted(prices)
    ops: list[dict] = []
    oid = max_oid
    for _ in range(cfg.writebench["n_insert_orders"]):
        oid += 1
        lines = []
        total = 0.0
        seen: set[int] = set()
        for _ in range(rng.randrange(1, 4)):
            pid = rng.choice(prod_ids)
            if pid in seen:  # 同一订单内商品不重复（PK 约束）
                continue
            seen.add(pid)
            qty = rng.randrange(1, 5)
            price = prices[pid]
            total += qty * price
            lines.append({"product_id": pid, "qty": qty, "unit_price": price})
        ops.append({
            "op": "insert_order",
            "order_id": oid,
            "customer_id": rng.randrange(1, n_cust + 1),
            "order_date": "2026-11-13",
            "status": "open",
            "channel": rng.choice(["web", "app", "store"]),
            "total": round(total, 2),
            "items": lines,
        })
    # （lines 生成见上：同一订单内商品已去重，满足 (order_id, product_id) 主键）
    nxt = {"open": "paid", "paid": "shipped", "shipped": "returned", "returned": "returned"}
    for _ in range(cfg.writebench["n_update_orders"]):
        target = rng.randrange(1, max_oid + 1)
        st = rng.choice(list(nxt))
        ops.append({"op": "update_status", "order_id": target, "status": nxt[st]})
    rng.shuffle(ops)
    return ops


def _clone_write_tables(conn, cfg: Config, schema: str, tables: list[str]):
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    for t in tables:
        # LIKE INCLUDING ALL 复制默认值/约束/已有索引（含主键唯一索引与业务索引），
        # 保证克隆表与初始数据副本结构一致；INSERT SELECT 填充同一初始数据。
        conn.execute(f"CREATE TABLE {schema}.{t} (LIKE {cfg.schema_tpl}.{t} INCLUDING ALL)")
        conn.execute(f"INSERT INTO {schema}.{t} SELECT * FROM {cfg.schema_tpl}.{t}")
        conn.execute(f"ANALYZE {schema}.{t}")


def _run_ops(conn, ops: list[dict], per_commit: int) -> float:
    conn.autocommit = False
    try:
        t0 = time.perf_counter()
        n = 0
        for op in ops:
            if op["op"] == "insert_order":
                conn.execute(
                    "INSERT INTO orders (order_id, customer_id, order_date, status, channel, total)"
                    " VALUES (%s,%s,%s,%s,%s,%s)",
                    (op["order_id"], op["customer_id"], op["order_date"],
                     op["status"], op["channel"], op["total"]),
                )
                for it in op["items"]:
                    conn.execute(
                        "INSERT INTO order_items (order_id, product_id, qty, unit_price)"
                        " VALUES (%s,%s,%s,%s)",
                        (op["order_id"], it["product_id"], it["qty"], it["unit_price"]),
                    )
            else:
                conn.execute(
                    "UPDATE orders SET status = %s WHERE order_id = %s",
                    (op["status"], op["order_id"]),
                )
            n += 1
            if n % per_commit == 0:
                conn.commit()
        conn.commit()
        return time.perf_counter() - t0
    except Exception:
        conn.rollback()  # 出错必须回滚，否则连接停留在 INERROR 状态污染后续测量
        raise
    finally:
        conn.autocommit = True


def measure_write_cost(cfg: Config, conn, combo_id: str,
                       candidates: list[Candidate], ops: list[dict],
                       out_dir: Path) -> dict:
    """对给定候选组合测量写入耗时。每次重复都从模板 schema 全新克隆。

    返回 {"status": "ok"|"skipped"|"failed", "times_s": [...], ...}
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    touched = sorted({c.table for c in candidates} & set(WRITE_TABLES))
    if candidates and not touched:
        res = {"status": "skipped",
               "reason": "组合不含写表(orders/order_items)上的索引，写入路径与基线一致"}
        (out_dir / "writebench.json").write_text(json.dumps(res, indent=1))
        return res

    # 操作流写 orders 与 order_items 两张表：无论候选索引落在哪张，
    # 都必须把两张写表整体克隆，候选索引只建在对应克隆表上。
    clone_tables = list(WRITE_TABLES)
    times: list[float] = []
    try:
        for rep in range(cfg.writebench["repeats"]):
            schema = f"{cfg.schema_write_prefix}{combo_id}_r{rep}"
            _clone_write_tables(conn, cfg, schema, clone_tables)
            conn.execute(f"SET search_path TO {schema}")
            for i, c in enumerate(candidates):
                if c.table in clone_tables:
                    conn.execute(c.ddl(f"{cfg.index_prefix}wb_{i}", schema))
            dt = _run_ops(conn, ops, cfg.writebench["ops_per_commit"])
            times.append(round(dt, 4))
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.execute(f"SET search_path TO {cfg.schema_main}")
        res = {"status": "ok", "times_s": times, "tables_cloned": clone_tables,
               "candidate_indexed_tables": touched, "ops": len(ops)}
    except Exception as e:  # 保留失败原因，该组合不得视为成功
        res = {"status": "failed", "error": f"{type(e).__name__}: {e}",
               "times_s": times}
        try:
            conn.execute(f"SET search_path TO {cfg.schema_main}")
        except Exception:
            pass
    (out_dir / "writebench.json").write_text(json.dumps(res, indent=1))
    return res
