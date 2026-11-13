"""功能测试: 状态机、原子预占、幂等、拒绝路径与全局不变量."""
from __future__ import annotations

import pytest

from inventory_tool import repository as repo
from inventory_tool.service import (
    ERR_INSUFFICIENT,
    ERR_INVALID_STATUS,
    ERR_ITEMS_MISMATCH,
    ERR_ORDER_ALREADY_RESERVED,
    ERR_ORDER_NOT_FOUND,
    OK_CANCELLED,
    OK_RESERVED,
    OK_SHIPPED,
    REJECTED_CONFLICT,
    process_request,
)
from inventory_tool.validation import ValidationError, content_hash

from conftest import seed_stock


def reserve_req(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "reserve", "items": items}


def ship_req(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "ship", "items": items}


def cancel_req(rid, oid, items):
    return {"request_id": rid, "order_id": oid, "action": "cancel", "items": items}


def stock_map(conn):
    return {r["sku"]: r for r in repo.list_stock(conn)}


def assert_invariants(conn):
    """库存非负、预占非负、可用=库存-预占、预占=RESERVED 订单之和、流水一致."""
    report = repo.reconcile(conn)
    assert report, "对账至少应返回库存行"
    for row in report:
        assert row["table_on_hand"] >= 0
        assert row["table_reserved"] >= 0
        assert row["table_reserved"] <= row["table_on_hand"]
        assert row["on_hand_match"], f"库存与流水不符: {row}"
        assert row["reserved_match"], f"预占与流水不符: {row}"
        assert row["orders_match"], f"预占与 RESERVED 订单之和不符: {row}"


def test_reserve_full_order_success(db):
    seed_stock(db, {"A": 5, "B": 3})
    out = process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 3},
                                                       {"sku": "B", "qty": 2}]))
    assert out.success and out.result_code == OK_RESERVED
    s = stock_map(db)
    assert (s["A"]["on_hand"], s["A"]["reserved"], s["A"]["available"]) == (5, 3, 2)
    assert (s["B"]["on_hand"], s["B"]["reserved"], s["B"]["available"]) == (3, 2, 1)
    assert repo.get_order(db, "o1")["status"] == "RESERVED"
    assert_invariants(db)


def test_reserve_insufficient_is_atomic_no_partial(db):
    """缺货订单: 整单失败, 不占用任何商品, 订单不创建."""
    seed_stock(db, {"A": 5, "B": 1})
    out = process_request(db, reserve_req("r1", "o1", [
        {"sku": "A", "qty": 2}, {"sku": "B", "qty": 2}]))
    assert not out.success and out.result_code == ERR_INSUFFICIENT
    s = stock_map(db)
    assert s["A"]["reserved"] == 0 and s["B"]["reserved"] == 0
    assert repo.get_order(db, "o1") is None
    # 失败结果已持久化
    reqs = {r["request_id"]: r for r in repo.list_requests(db)}
    assert reqs["r1"]["status"] == "FAILED"
    # 失败不产生任何业务流水 (仅保留初始化 INIT 基线)
    assert all(e["reason"] == "INIT" for e in repo.list_ledger(db))
    assert_invariants(db)


def test_ship_reduces_both_and_marks_shipped(db):
    seed_stock(db, {"A": 5})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 3}]))
    out = process_request(db, ship_req("r2", "o1", [{"sku": "A", "qty": 3}]))
    assert out.success and out.result_code == OK_SHIPPED
    s = stock_map(db)["A"]
    assert s == {"sku": "A", "on_hand": 2, "reserved": 0, "available": 2}
    assert repo.get_order(db, "o1")["status"] == "SHIPPED"
    assert_invariants(db)


def test_cancel_releases_reservation(db):
    seed_stock(db, {"A": 5, "B": 4})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 4},
                                                 {"sku": "B", "qty": 1}]))
    out = process_request(db, cancel_req("r2", "o1", [{"sku": "A", "qty": 4},
                                                      {"sku": "B", "qty": 1}]))
    assert out.success and out.result_code == OK_CANCELLED
    s = stock_map(db)
    assert s["A"]["reserved"] == 0 and s["B"]["reserved"] == 0
    assert s["A"]["on_hand"] == 5 and s["B"]["on_hand"] == 4
    assert repo.get_order(db, "o1")["status"] == "CANCELLED"
    assert_invariants(db)


def test_terminal_states_cannot_transition(db):
    seed_stock(db, {"A": 5})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 1}]))
    process_request(db, ship_req("r2", "o1", [{"sku": "A", "qty": 1}]))
    # SHIPPED -> cancel / ship 均拒绝
    out = process_request(db, cancel_req("r3", "o1", [{"sku": "A", "qty": 1}]))
    assert not out.success and out.result_code == ERR_INVALID_STATUS
    out = process_request(db, ship_req("r4", "o1", [{"sku": "A", "qty": 1}]))
    assert not out.success and out.result_code == ERR_INVALID_STATUS
    # 库存保持终态
    assert stock_map(db)["A"]["on_hand"] == 4
    assert_invariants(db)


def test_cancelled_cannot_ship(db):
    seed_stock(db, {"A": 5})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 1}]))
    process_request(db, cancel_req("r2", "o1", [{"sku": "A", "qty": 1}]))
    out = process_request(db, ship_req("r3", "o1", [{"sku": "A", "qty": 1}]))
    assert not out.success and out.result_code == ERR_INVALID_STATUS
    assert stock_map(db)["A"]["on_hand"] == 5
    assert_invariants(db)


def test_ship_unknown_order_and_items_mismatch(db):
    seed_stock(db, {"A": 5})
    out = process_request(db, ship_req("r1", "ghost", [{"sku": "A", "qty": 1}]))
    assert not out.success and out.result_code == ERR_ORDER_NOT_FOUND

    process_request(db, reserve_req("r2", "o1", [{"sku": "A", "qty": 2}]))
    out = process_request(db, ship_req("r3", "o1", [{"sku": "A", "qty": 1}]))
    assert not out.success and out.result_code == ERR_ITEMS_MISMATCH
    assert_invariants(db)


def test_duplicate_reserve_same_order_rejected(db):
    seed_stock(db, {"A": 10})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 2}]))
    out = process_request(db, reserve_req("r2", "o1", [{"sku": "A", "qty": 2}]))
    assert not out.success and out.result_code == ERR_ORDER_ALREADY_RESERVED
    assert stock_map(db)["A"]["reserved"] == 2  # 没有叠加
    assert_invariants(db)


def test_idempotent_retry_returns_first_result_including_failure(db):
    seed_stock(db, {"A": 1})
    req = reserve_req("r1", "o1", [{"sku": "A", "qty": 5}])
    first = process_request(db, req)
    assert not first.success and first.result_code == ERR_INSUFFICIENT
    second = process_request(db, dict(req))
    assert second.replayed is True
    assert second.success == first.success
    assert second.result_code == first.result_code
    # 只有一条请求台账, 业务流水为空 (INIT 基线除外)
    assert len(repo.list_requests(db)) == 1
    assert all(e["reason"] == "INIT" for e in repo.list_ledger(db))

    # 成功请求同样幂等
    ok_req = reserve_req("r2", "o2", [{"sku": "A", "qty": 1}])
    process_request(db, ok_req)
    replay = process_request(db, dict(ok_req))
    assert replay.replayed and replay.success and replay.result_code == OK_RESERVED
    assert stock_map(db)["A"]["reserved"] == 1
    assert_invariants(db)


def test_same_request_id_different_content_rejected(db):
    seed_stock(db, {"A": 10})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 1}]))
    evil = reserve_req("r1", "o1", [{"sku": "A", "qty": 9}])
    out = process_request(db, evil)
    assert not out.success and out.result_code == REJECTED_CONFLICT
    # 原始结果不受影响, 库存未被篡改
    assert stock_map(db)["A"]["reserved"] == 1
    reqs = repo.list_requests(db)
    original = next(r for r in reqs if r["request_id"] == "r1")
    assert original["result_code"] == OK_RESERVED
    assert_invariants(db)


def test_validation_rules(db):
    seed_stock(db, {"A": 5})
    bad_cases = [
        {"request_id": "", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": 1}]},
        {"request_id": "r", "order_id": "o", "action": "freeze",
         "items": [{"sku": "A", "qty": 1}]},
        {"request_id": "r", "order_id": "o", "action": "reserve", "items": []},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": 0}]},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": -1}]},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": 1.5}]},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": True}]},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "A", "qty": 1}, {"sku": "A", "qty": 2}]},
        {"request_id": "r", "order_id": "o", "action": "reserve",
         "items": [{"sku": "", "qty": 1}]},
    ]
    for case in bad_cases:
        with pytest.raises(ValidationError):
            process_request(db, case)
    # 全部非法请求都不允许落库
    assert repo.list_requests(db) == []
    assert all(e["reason"] == "INIT" for e in repo.list_ledger(db))
    assert_invariants(db)


def test_ledger_can_recompute_stock(db):
    seed_stock(db, {"A": 10, "B": 8})
    process_request(db, reserve_req("r1", "o1", [{"sku": "A", "qty": 4}]))
    process_request(db, ship_req("r2", "o1", [{"sku": "A", "qty": 4}]))
    process_request(db, reserve_req("r3", "o2", [{"sku": "B", "qty": 3}]))
    process_request(db, cancel_req("r4", "o2", [{"sku": "B", "qty": 3}]))
    process_request(db, reserve_req("r5", "o3", [{"sku": "A", "qty": 2},
                                                 {"sku": "B", "qty": 5}]))
    ledger = [e for e in repo.list_ledger(db) if e["reason"] != "INIT"]
    calc = {}
    for entry in ledger:
        agg = calc.setdefault(entry["sku"], {"on_hand": 0, "reserved": 0})
        agg["on_hand"] += entry["delta_on_hand"]
        agg["reserved"] += entry["delta_reserved"]
    assert calc == {"A": {"on_hand": -4, "reserved": 2},
                    "B": {"on_hand": 0, "reserved": 5}}
    for row in repo.reconcile(db):
        assert row["ledger_on_hand"] == row["table_on_hand"]
        assert calc[row["sku"]]["reserved"] == row["ledger_reserved"]
    assert_invariants(db)


def test_content_hash_stable_and_order_insensitive_for_keys(db):
    req = reserve_req("r1", "o1", [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 2}])
    req_again = {
        "request_id": "r1", "order_id": "o1", "action": "reserve",
        "items": [{"qty": 1, "sku": "A"}, {"qty": 2, "sku": "B"}],
    }
    assert content_hash(req) == content_hash(req_again)
