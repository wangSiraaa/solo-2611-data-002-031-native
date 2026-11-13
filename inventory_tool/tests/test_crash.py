"""强制终止测试: 在关键边界 SIGKILL 执行进程, 重试后核对完整性.

两个崩溃点 (由 INV_CRASH_HOOK 环境变量控制):
  pre_commit : 商品行已更新、事务提交之前 -> 连接断开, PG 自动回滚,
               重试必须像首次执行一样成功, 不留任何半成品.
  post_commit: 事务已提交、结果返回之前 -> 数据已落库,
               重试必须走幂等路径返回第一次的终态, 不重复扣减.
"""
from __future__ import annotations

import multiprocessing as mp
import os

from inventory_tool import repository as repo
from inventory_tool.service import OK_RESERVED, OK_SHIPPED, connect, process_request

from conftest import seed_stock

TIMEOUT = 30


def _crashed_worker(dsn: str, request: dict, hook: str, queue) -> None:
    os.environ["INV_CRASH_HOOK"] = hook
    conn = connect(dsn)
    try:
        outcome = process_request(conn, request)
        queue.put(outcome.to_dict())  # 正常情况下不会走到这里
    finally:
        conn.close()


def _run_crashed(dsn: str, request: dict, phase: str) -> int:
    # fork: 继承 pytest 进程内已导入的模块, 与 spawn 相比不受 assert 重写影响.
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_crashed_worker,
        args=(dsn, request, f"{phase}:{request['request_id']}", queue),
    )
    proc.start()
    proc.join(timeout=TIMEOUT)
    assert proc.exitcode == -9, f"预期 SIGKILL(-9), 实际 exitcode={proc.exitcode}"
    assert queue.empty(), "崩溃进程不应返回结果"
    return proc.pid


def test_kill_before_commit_reserve_then_retry(db, dsn):
    seed_stock(db, {"A": 5, "B": 5})
    req = {"request_id": "r1", "order_id": "o1", "action": "reserve",
           "items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 3}]}

    _run_crashed(dsn, req, "pre_commit")
    db.rollback()  # 结束父连接旧快照, 读取崩溃进程回滚后的真实状态

    # 崩溃后: 事务已回滚, 无订单/台账/业务流水, 库存未动
    assert repo.get_order(db, "o1") is None
    assert repo.list_requests(db) == []
    assert all(e["reason"] == "INIT" for e in repo.list_ledger(db))
    stock = {r["sku"]: r for r in repo.list_stock(db)}
    assert stock["A"]["reserved"] == 0 and stock["B"]["reserved"] == 0

    # 重试: 应当作为首次请求成功
    outcome = process_request(db, req)
    assert outcome.success and outcome.result_code == OK_RESERVED
    assert outcome.replayed is False
    stock = {r["sku"]: r for r in repo.list_stock(db)}
    assert stock["A"]["reserved"] == 2 and stock["B"]["reserved"] == 3
    biz = [e for e in repo.list_ledger(db) if e["reason"] != "INIT"]
    assert len(biz) == 2
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_kill_after_commit_reserve_then_retry_is_idempotent(db, dsn):
    seed_stock(db, {"A": 5})
    req = {"request_id": "r1", "order_id": "o1", "action": "reserve",
           "items": [{"sku": "A", "qty": 4}]}

    _run_crashed(dsn, req, "post_commit")
    db.rollback()

    # 提交已生效
    assert repo.get_order(db, "o1")["status"] == "RESERVED"
    assert repo.list_stock(db)[0]["reserved"] == 4

    # 重试: 幂等返回首次结果, 不重复预占
    outcome = process_request(db, req)
    assert outcome.success and outcome.replayed is True
    assert repo.list_stock(db)[0]["reserved"] == 4
    biz = [e for e in repo.list_ledger(db) if e["reason"] != "INIT"]
    assert len(biz) == 1
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_kill_before_commit_ship_then_retry(db, dsn):
    seed_stock(db, {"A": 7})
    process_request(db, {
        "request_id": "r0", "order_id": "o1", "action": "reserve",
        "items": [{"sku": "A", "qty": 5}],
    })
    ship_req = {"request_id": "r1", "order_id": "o1", "action": "ship",
                "items": [{"sku": "A", "qty": 5}]}

    _run_crashed(dsn, ship_req, "pre_commit")
    db.rollback()

    # 回滚后订单仍 RESERVED, 库存与预占不变, 无 ship 流水/台账
    assert repo.get_order(db, "o1")["status"] == "RESERVED"
    stock = repo.list_stock(db)[0]
    assert stock == {"sku": "A", "on_hand": 7, "reserved": 5, "available": 2}
    assert [r["request_id"] for r in repo.list_requests(db)] == ["r0"]
    assert all(e["reason"] == "RESERVE"
               for e in repo.list_ledger(db) if e["reason"] != "INIT")

    outcome = process_request(db, ship_req)
    assert outcome.success and outcome.result_code == OK_SHIPPED
    stock = repo.list_stock(db)[0]
    assert stock == {"sku": "A", "on_hand": 2, "reserved": 0, "available": 2}
    assert repo.get_order(db, "o1")["status"] == "SHIPPED"
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]


def test_kill_after_commit_failed_reserve_then_retry_returns_failure(db, dsn):
    # 失败结果也已持久化; 提交后被杀, 重试必须返回同一个失败终态.
    seed_stock(db, {"A": 1})
    req = {"request_id": "r1", "order_id": "o1", "action": "reserve",
           "items": [{"sku": "A", "qty": 9}]}
    _run_crashed(dsn, req, "post_commit")
    db.rollback()

    assert repo.get_order(db, "o1") is None
    persisted = repo.list_requests(db)
    assert len(persisted) == 1 and persisted[0]["status"] == "FAILED"

    outcome = process_request(db, req)
    assert not outcome.success and outcome.replayed is True
    assert outcome.result_code == "INSUFFICIENT_STOCK"
    assert all(e["reason"] == "INIT" for e in repo.list_ledger(db))
    assert repo.list_stock(db)[0]["reserved"] == 0


def test_kill_before_commit_cancel_then_retry(db, dsn):
    seed_stock(db, {"A": 6})
    process_request(db, {
        "request_id": "r0", "order_id": "o1", "action": "reserve",
        "items": [{"sku": "A", "qty": 6}],
    })
    cancel_req = {"request_id": "r1", "order_id": "o1", "action": "cancel",
                  "items": [{"sku": "A", "qty": 6}]}
    _run_crashed(dsn, cancel_req, "pre_commit")
    db.rollback()

    assert repo.get_order(db, "o1")["status"] == "RESERVED"
    assert repo.list_stock(db)[0]["reserved"] == 6

    outcome = process_request(db, cancel_req)
    assert outcome.success and outcome.result_code == "CANCELLED"
    stock = repo.list_stock(db)[0]
    assert stock == {"sku": "A", "on_hand": 6, "reserved": 0, "available": 6}
    for row in repo.reconcile(db):
        assert row["on_hand_match"] and row["reserved_match"] and row["orders_match"]
