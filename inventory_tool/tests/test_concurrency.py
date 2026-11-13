"""进程级并发测试: 用 multiprocessing.Barrier 让多个进程在同一瞬间发起竞争.

覆盖:
- 两张多商品订单争抢最后库存 (恰好一个成功, 不超卖, 无部分预占)
- 两个请求同时发货 (同一订单只有一个成功)
- 发货与取消竞争 (只有一个终态生效, 库存/预占平衡)
- 相反 SKU 加锁顺序的并发预占 (不死锁, 结果一致)
- 缺货并发下不出现部分预占
"""
from __future__ import annotations

import multiprocessing as mp

import pytest

from inventory_tool import repository as repo
from inventory_tool.service import (
    OK_RESERVED,
    OK_SHIPPED,
    connect,
    process_request,
)

from conftest import seed_stock

TIMEOUT = 30


def _worker(dsn: str, request: dict, barrier, queue) -> None:
    """子进程入口: 在屏障处等待, 然后同时发起请求."""
    conn = connect(dsn)
    try:
        barrier.wait(timeout=TIMEOUT)
        outcome = process_request(conn, request)
        queue.put(outcome.to_dict())
    except Exception as exc:  # pragma: no cover - 失败时显式上报
        queue.put({"fatal": repr(exc)})
    finally:
        conn.close()


def run_concurrent(dsn: str, requests: list[dict]) -> list[dict]:
    # fork 上下文: 子进程继承已导入的模块, 避免 spawn 与 pytest assert 重写冲突.
    n = len(requests)
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(n)
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(dsn, req, barrier, queue))
        for req in requests
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=TIMEOUT)
        assert p.exitcode is not None, f"子进程 {p.pid} 超时未结束"
        assert p.exitcode == 0, f"子进程异常退出 exitcode={p.exitcode}"
    results = []
    for _ in range(n):
        results.append(queue.get(timeout=TIMEOUT))
    assert all("fatal" not in r for r in results), results
    return results


def reserve(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "reserve", "items": items}


def ship(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "ship", "items": items}


def cancel(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "cancel", "items": items}


def refresh(conn):
    """结束父连接上可能存在的旧事务快照, 以便读到并发进程已提交的数据."""
    conn.rollback()
    return conn


def test_two_multi_item_orders_fight_for_last_stock(db, dsn):
    # 库存只够其中一张整单: A 共 5, B 共 4
    seed_stock(db, {"A": 5, "B": 4})
    items = [{"sku": "A", "qty": 3}, {"sku": "B", "qty": 2}]
    results = run_concurrent(dsn, [
        reserve("r1", "o1", items),
        reserve("r2", "o2", items),
    ])
    refresh(db)
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    assert len(successes) == 1
    assert len(failures) == 1
    # 失败方明确是库存不足, 且没有任何订单部分预占
    assert failures[0]["result_code"] == "INSUFFICIENT_STOCK"
    stock = {r["sku"]: r for r in repo.list_stock(db)}
    assert stock["A"]["reserved"] == 3 and stock["B"]["reserved"] == 2
    assert stock["A"]["available"] == 2 and stock["B"]["available"] == 2
    orders = {oid: repo.get_order(db, oid) for oid in ("o1", "o2")}
    existing = [o for o in orders.values() if o is not None]
    assert len(existing) == 1 and existing[0]["status"] == "RESERVED"
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_concurrent_ship_same_order_only_one_wins(db, dsn):
    seed_stock(db, {"A": 6})
    process_request(db, reserve("r0", "o1", [{"sku": "A", "qty": 4}]))
    results = run_concurrent(dsn, [
        ship("s1", "o1", [{"sku": "A", "qty": 4}]),
        ship("s2", "o1", [{"sku": "A", "qty": 4}]),
    ])
    refresh(db)
    successes = [r for r in results if r["success"]]
    assert len(successes) == 1
    assert successes[0]["result_code"] == OK_SHIPPED
    loser = next(r for r in results if not r["success"])
    assert loser["result_code"] == "INVALID_ORDER_STATUS"
    stock = repo.list_stock(db)[0]
    assert stock == {"sku": "A", "on_hand": 2, "reserved": 0, "available": 2}
    assert repo.get_order(db, "o1")["status"] == "SHIPPED"


def test_ship_versus_cancel_race(db, dsn):
    seed_stock(db, {"A": 6, "B": 3})
    items = [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 3}]
    process_request(db, reserve("r0", "o1", items))
    results = run_concurrent(dsn, [
        ship("s1", "o1", items),
        cancel("c1", "o1", items),
    ])
    refresh(db)
    assert sum(1 for r in results if r["success"]) == 1
    status = repo.get_order(db, "o1")["status"]
    stock = {r["sku"]: r for r in repo.list_stock(db)}
    if status == "SHIPPED":
        assert stock["A"] == {"sku": "A", "on_hand": 4, "reserved": 0, "available": 4}
        assert stock["B"] == {"sku": "B", "on_hand": 0, "reserved": 0, "available": 0}
    else:
        assert status == "CANCELLED"
        assert stock["A"] == {"sku": "A", "on_hand": 6, "reserved": 0, "available": 6}
        assert stock["B"] == {"sku": "B", "on_hand": 3, "reserved": 0, "available": 3}
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_reverse_lock_order_no_deadlock(db, dsn):
    # 两个进程以相反 SKU 顺序预占, 行锁统一在服务层按 sku 排序, 不得死锁.
    seed_stock(db, {"A": 10, "B": 10})
    results = run_concurrent(dsn, [
        reserve("r1", "o1", [{"sku": "A", "qty": 4}, {"sku": "B", "qty": 4}]),
        reserve("r2", "o2", [{"sku": "B", "qty": 4}, {"sku": "A", "qty": 4}]),
    ])
    refresh(db)
    assert all(r["success"] and r["result_code"] == OK_RESERVED for r in results)
    stock = {r["sku"]: r for r in repo.list_stock(db)}
    assert stock["A"]["reserved"] == 8 and stock["B"]["reserved"] == 8
    assert stock["A"]["available"] == 2 and stock["B"]["available"] == 2
    for row in repo.reconcile(db):
        assert row["orders_match"] and row["reserved_match"]


def test_concurrent_shortage_never_partial(db, dsn):
    # 三张订单同时抢, 每张要 2, 库存只有 3 -> 严格只够一张, 其余必须整单失败.
    seed_stock(db, {"A": 3})
    results = run_concurrent(dsn, [
        reserve("r1", "o1", [{"sku": "A", "qty": 2}]),
        reserve("r2", "o2", [{"sku": "A", "qty": 2}]),
        reserve("r3", "o3", [{"sku": "A", "qty": 2}]),
    ])
    refresh(db)
    assert sum(1 for r in results if r["success"]) == 1
    stock = repo.list_stock(db)[0]
    assert stock["reserved"] == 2 and stock["available"] == 1
    failed_orders = [oid for oid in ("o1", "o2", "o3")
                     if repo.get_order(db, oid) is None]
    assert len(failed_orders) == 2
    # 失败请求同样持久化, 且无流水
    failed_reqs = [r for r in results if not r["success"]]
    assert all(r["result_code"] == "INSUFFICIENT_STOCK" for r in failed_reqs)
    biz_ledger = [e for e in repo.list_ledger(db) if e["reason"] != "INIT"]
    assert {e["sku"] for e in biz_ledger} == {"A"}
    assert sum(e["delta_reserved"] for e in biz_ledger) == 2


@pytest.mark.parametrize("terminal_action", ["ship", "cancel"])
def test_repeated_terminal_requests_serialized(db, dsn, terminal_action):
    seed_stock(db, {"A": 8})
    process_request(db, reserve("r0", "o1", [{"sku": "A", "qty": 3}]))
    builder = ship if terminal_action == "ship" else cancel
    results = run_concurrent(dsn, [
        builder(f"{terminal_action}-1", "o1", [{"sku": "A", "qty": 3}]),
        builder(f"{terminal_action}-2", "o1", [{"sku": "A", "qty": 3}]),
        builder(f"{terminal_action}-3", "o1", [{"sku": "A", "qty": 3}]),
    ])
    refresh(db)
    assert sum(1 for r in results if r["success"]) == 1
    assert repo.get_order(db, "o1")["status"] in ("SHIPPED", "CANCELLED")
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_serializable_retry_under_hot_contention(db, dsn):
    # 6 个进程同时抢同一 SKU (库存 5, 每张要 2 -> 仅 2 张成功).
    # SERIALIZABLE 冲突由有限重试消化, 每个请求都拿到明确终态, 无超卖.
    seed_stock(db, {"X": 5})
    results = run_concurrent(dsn, [
        reserve(f"k{i}", f"ko{i}", [{"sku": "X", "qty": 2}]) for i in range(6)
    ])
    refresh(db)
    assert sum(1 for r in results if r["success"]) == 2
    assert all("fatal" not in r for r in results)
    assert all(r["attempts"] >= 1 for r in results)
    stock = repo.list_stock(db)[0]
    assert stock["reserved"] == 4 and stock["available"] == 1
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]
