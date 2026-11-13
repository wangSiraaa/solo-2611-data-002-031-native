"""核心库存服务: 每个请求一个数据库事务, 保证原子性与幂等性.

并发控制
--------
- 事务隔离级别使用 SERIALIZABLE, 任何可串行化冲突都会被检测并整体重试.
- 所有需要锁定的 stock 行一律按 sku 升序加 ``SELECT ... FOR UPDATE`` 行锁,
  无论调用方传入的商品顺序如何, 从而避免相反加锁顺序导致的长期死锁.
- 订单行先锁 order_records (主键单点), 再按 sku 顺序锁库存行, 锁层次固定.
- 可重试错误 (SerializationFailure / DeadlockDetected / 唯一竞争) 采用
  有限次数的指数退避重试; 重试期间请求尚未持久化, 终态只会产生一次.

幂等
----
- request_id 为主键. 首次成功/失败都会把结果写入 request_records.
- 相同 request_id + 相同内容: 直接返回第一次已持久化的结果 (replayed=True),
  包括失败结果, 不会再次执行任何库存变化.
- 相同 request_id + 不同内容: 持久化并返回 REJECTED / REQUEST_CONTENT_CONFLICT.
"""
from __future__ import annotations

import os
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg2

from .validation import (
    CONFLICT_MARKER_SUFFIX,
    content_hash,
    validate_request,
)

# 可重试数据库冲突的最大尝试次数 (首次 + 重试), 有限且明确.
MAX_ATTEMPTS = 10
_BASE_BACKOFF_MS = 5
_MAX_BACKOFF_MS = 200

# 结果码常量
OK_RESERVED = "RESERVED"
OK_SHIPPED = "SHIPPED"
OK_CANCELLED = "CANCELLED"
REJECTED_CONFLICT = "REQUEST_CONTENT_CONFLICT"
ERR_ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
ERR_ORDER_ALREADY_RESERVED = "ORDER_ALREADY_RESERVED"
ERR_INVALID_STATUS = "INVALID_ORDER_STATUS"
ERR_ITEMS_MISMATCH = "ITEMS_MISMATCH"
ERR_INSUFFICIENT = "INSUFFICIENT_STOCK"
ERR_SKU_UNKNOWN = "UNKNOWN_SKU"
ERR_RETRY_EXHAUSTED = "RETRIES_EXHAUSTED"


@dataclass
class RequestOutcome:
    """一次 process_request 的明确终态结果."""

    request_id: str
    order_id: str
    action: str
    success: bool
    result_code: str
    message: str
    replayed: bool = False
    attempts: int = 1
    stock_snapshot: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "order_id": self.order_id,
            "action": self.action,
            "success": self.success,
            "result_code": self.result_code,
            "message": self.message,
            "replayed": self.replayed,
            "attempts": self.attempts,
            "stock": self.stock_snapshot,
        }


class RetryableDatabaseConflict(Exception):
    """可安全重试的数据库冲突 (序列化失败 / 死锁 / 唯一竞争)."""


def _backoff(attempt: int) -> float:
    """指数退避 + 确定性抖动上限, 单位秒."""
    cap = min(_MAX_BACKOFF_MS, _BASE_BACKOFF_MS * (2 ** attempt))
    return random.uniform(cap * 0.5, cap) / 1000.0


def _maybe_crash(conn, phase: str, request_id: str) -> None:
    """崩溃注入钩子, 仅用于测试强制终止执行进程.

    环境变量 INV_CRASH_HOOK 格式: "<phase>:<request_id>"
      phase = pre_commit  (更新商品后、提交前)
              post_commit (提交后、返回前)
    命中后立即 SIGKILL 本进程, 模拟操作系统级强制终止;
    未提交事务由 PostgreSQL 在连接断开时自动回滚.
    """
    spec = os.environ.get("INV_CRASH_HOOK", "")
    if not spec:
        return
    hook_phase, _, hook_rid = spec.partition(":")
    if hook_phase == phase and hook_rid == request_id:
        # pre_commit: 调用方尚未 commit, 连接断开后 PG 自动回滚.
        # post_commit: 调用方已 commit, 数据已持久化.
        os.kill(os.getpid(), signal.SIGKILL)
        # SIGKILL 不可捕获; 以下语句仅为静态检查保留.
        raise SystemExit(137)


def _lock_stock_rows(cur, skus: list[str]) -> dict[str, tuple[int, int]]:
    """按 sku 升序对库存行加排他行锁, 返回 {sku: (on_hand, reserved)}."""
    rows: dict[str, tuple[int, int]] = {}
    for sku in sorted(skus):
        cur.execute(
            "SELECT on_hand, reserved FROM stock WHERE sku = %s FOR UPDATE",
            (sku,),
        )
        row = cur.fetchone()
        if row is None:
            raise _BusinessError(ERR_SKU_UNKNOWN, f"未知 SKU, 库存表中不存在: {sku}")
        rows[sku] = (row[0], row[1])
    return rows


class _BusinessError(Exception):
    """业务失败: 在当前事务内持久化 FAILED 结果后正常提交."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _finalize_request(cur, req: dict[str, Any], status: str,
                      code: str, message: str) -> None:
    """业务执行结束后回填请求台账的终态 (该行在业务变更前已登记)."""
    cur.execute(
        "UPDATE request_records SET status = %s, result_code = %s, message = %s "
        "WHERE request_id = %s",
        (status, code, message, req["request_id"]),
    )


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _do_reserve(cur, req: dict[str, Any], digest: str) -> tuple[str, str]:
    order_id = req["order_id"]
    items = req["items"]
    want = {it["sku"]: it["qty"] for it in items}

    # 订单已存在 -> 拒绝再次预占, 绝不悄悄叠加数量.
    cur.execute("SELECT status FROM order_records WHERE order_id = %s FOR UPDATE",
                (order_id,))
    existing = cur.fetchone()
    if existing is not None:
        raise _BusinessError(
            ERR_ORDER_ALREADY_RESERVED,
            f"订单 {order_id} 已存在 (状态={existing[0]}), 不允许重复预占",
        )

    # 统一按 sku 排序加锁, 消除多 SKU 相反加锁顺序死锁.
    stock = _lock_stock_rows(cur, list(want.keys()))

    # 全量校验: 任一 SKU 可用量不足即整体失败, 不占用任何商品.
    insufficient = [
        (sku, want[sku], on - res)
        for sku, (on, res) in stock.items()
        if on - res < want[sku]
    ]
    if insufficient:
        detail = ", ".join(
            f"{sku}: 需要 {need}, 可用 {avail}" for sku, need, avail in insufficient
        )
        raise _BusinessError(ERR_INSUFFICIENT, f"库存不足, 整单预占失败 ({detail})")

    # 全部可用 -> 建订单、加预占、写流水.
    cur.execute(
        "INSERT INTO order_records (order_id, status) VALUES (%s, 'RESERVED')",
        (order_id,),
    )
    cur.executemany(
        "INSERT INTO order_items (order_id, sku, qty) VALUES (%s, %s, %s)",
        [(order_id, it["sku"], it["qty"]) for it in items],
    )
    for it in items:
        sku, qty = it["sku"], it["qty"]
        cur.execute(
            "UPDATE stock SET reserved = reserved + %s WHERE sku = %s",
            (qty, sku),
        )
        cur.execute(
            """
            INSERT INTO stock_ledger
                (request_id, order_id, sku, qty, delta_on_hand, delta_reserved, reason)
            VALUES (%s, %s, %s, %s, 0, %s, 'RESERVE')
            """,
            (req["request_id"], order_id, sku, qty, qty),
        )
    return OK_RESERVED, "整单预占成功, 订单进入 RESERVED"


def _load_order_for_terminal(cur, req: dict[str, Any]) -> list[dict[str, Any]]:
    """ship/cancel 公共前置: 锁订单、校验状态、校验商品清单一致."""
    order_id = req["order_id"]
    cur.execute("SELECT status FROM order_records WHERE order_id = %s FOR UPDATE",
                (order_id,))
    existing = cur.fetchone()
    if existing is None:
        raise _BusinessError(ERR_ORDER_NOT_FOUND, f"订单不存在: {order_id}")
    status = existing[0]
    if status != "RESERVED":
        raise _BusinessError(
            ERR_INVALID_STATUS,
            f"订单 {order_id} 当前状态={status}, 仅 RESERVED 可执行 {req['action']}",
        )

    cur.execute(
        "SELECT sku, qty FROM order_items WHERE order_id = %s", (order_id,)
    )
    persisted = {sku: qty for sku, qty in cur.fetchall()}
    want = {it["sku"]: it["qty"] for it in req["items"]}
    if want != persisted:
        raise _BusinessError(
            ERR_ITEMS_MISMATCH,
            f"请求商品与订单 {order_id} 预占明细不一致, "
            f"请求={sorted(want.items())}, 预占={sorted(persisted.items())}",
        )
    return req["items"]


def _do_ship(cur, req: dict[str, Any], digest: str) -> tuple[str, str]:
    items = _load_order_for_terminal(cur, req)
    _lock_stock_rows(cur, [it["sku"] for it in items])  # 按 sku 排序加锁
    order_id = req["order_id"]
    for it in items:
        sku, qty = it["sku"], it["qty"]
        # 同时减少库存与预占; CHECK 约束保证不会变负.
        cur.execute(
            "UPDATE stock SET on_hand = on_hand - %s, reserved = reserved - %s "
            "WHERE sku = %s",
            (qty, qty, sku),
        )
        cur.execute(
            """
            INSERT INTO stock_ledger
                (request_id, order_id, sku, qty, delta_on_hand, delta_reserved, reason)
            VALUES (%s, %s, %s, %s, %s, %s, 'SHIP')
            """,
            (req["request_id"], order_id, sku, qty, -qty, -qty),
        )
    cur.execute(
        "UPDATE order_records SET status = 'SHIPPED', updated_at = now() "
        "WHERE order_id = %s",
        (order_id,),
    )
    return OK_SHIPPED, "发货成功, 库存与预占同步减少, 订单进入 SHIPPED"


def _do_cancel(cur, req: dict[str, Any], digest: str) -> tuple[str, str]:
    items = _load_order_for_terminal(cur, req)
    _lock_stock_rows(cur, [it["sku"] for it in items])
    order_id = req["order_id"]
    for it in items:
        sku, qty = it["sku"], it["qty"]
        cur.execute(
            "UPDATE stock SET reserved = reserved - %s WHERE sku = %s",
            (qty, sku),
        )
        cur.execute(
            """
            INSERT INTO stock_ledger
                (request_id, order_id, sku, qty, delta_on_hand, delta_reserved, reason)
            VALUES (%s, %s, %s, %s, 0, %s, 'CANCEL')
            """,
            (req["request_id"], order_id, sku, qty, -qty),
        )
    cur.execute(
        "UPDATE order_records SET status = 'CANCELLED', updated_at = now() "
        "WHERE order_id = %s",
        (order_id,),
    )
    return OK_CANCELLED, "取消成功, 预占已释放, 订单进入 CANCELLED"


_DISPATCH = {
    "reserve": _do_reserve,
    "ship": _do_ship,
    "cancel": _do_cancel,
}


def _snapshot_stock(cur) -> dict[str, dict[str, int]]:
    cur.execute(
        "SELECT sku, on_hand, reserved FROM stock ORDER BY sku"
    )
    return {
        sku: {"on_hand": on, "reserved": res, "available": on - res}
        for sku, on, res in cur.fetchall()
    }


def process_request(conn, raw_request: dict[str, Any]) -> RequestOutcome:
    """处理一条请求, 返回明确终态. 调用方传入独立连接.

    流程:
      1. 格式校验 (失败直接抛 ValidationError, 不访问数据库)
      2. 事务内先查 request_records 处理幂等 / 内容冲突
      3. 新请求在同一事务内完成订单、库存、流水、请求结果的全部写入
      4. 可重试数据库冲突 -> 有限指数退避后整体重试
    """
    req = validate_request(raw_request)
    digest = content_hash(req)
    rid = req["request_id"]
    last_conflict: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT content_hash, status, result_code, message, action "
                "FROM request_records WHERE request_id = %s",
                (rid,),
            )
            prior = cur.fetchone()
            if prior is not None:
                prior_hash, prior_status, prior_code, prior_msg, prior_action = prior
                if prior_hash != digest:
                    # 相同编号换内容: 拒绝执行, 原始台账一行绝不覆盖.
                    # 拒绝事件以保留后缀另存一行 REJECTED 记录 (冲突竞争时可重试).
                    cur.execute(
                        """
                        INSERT INTO request_records
                            (request_id, order_id, action, content_hash, status,
                             result_code, message, request_body)
                        VALUES (%s,%s,%s,%s,'REJECTED',%s,%s,%s::jsonb)
                        ON CONFLICT (request_id) DO NOTHING
                        """,
                        (rid + CONFLICT_MARKER_SUFFIX, req["order_id"],
                         req["action"], digest, REJECTED_CONFLICT,
                         "request_id 已存在但请求内容不同, 拒绝执行",
                         json_dumps(req)),
                    )
                    conn.commit()
                    return RequestOutcome(
                        rid, req["order_id"], req["action"], False,
                        REJECTED_CONFLICT,
                        "request_id 已存在但请求内容不同, 拒绝执行",
                        attempts=attempt,
                        stock_snapshot=_snapshot_stock(cur),
                    )
                # 完全相同的请求: 返回第一次已持久化的结果, 含失败结果.
                snapshot = _snapshot_stock(cur)
                conn.rollback()  # 结束只读幂等事务
                return RequestOutcome(
                    rid, req["order_id"], prior_action,
                    prior_status == "SUCCESS", prior_code, prior_msg,
                    replayed=True, attempts=attempt, stock_snapshot=snapshot,
                )

            # 新请求: 先登记台账 (作为流水外键锚点, 状态为临时 PENDING),
            # 业务在同一事务内执行, 结束后回填终态; 事务回滚则该行一并消失.
            cur.execute(
                """
                INSERT INTO request_records
                    (request_id, order_id, action, content_hash, status,
                     result_code, message, request_body)
                VALUES (%s,%s,%s,%s,'PENDING','PENDING','处理中',%s::jsonb)
                """,
                (rid, req["order_id"], req["action"], digest, json_dumps(req)),
            )
            try:
                code, message = _DISPATCH[req["action"]](cur, req, digest)
                success = True
                _finalize_request(cur, req, "SUCCESS", code, message)
            except _BusinessError as exc:
                code, message, success = exc.code, exc.message, False
                # 业务失败: 同一事务内把台账回填为 FAILED; 无任何库存/订单变化.
                _finalize_request(cur, req, "FAILED", code, message)

            # 崩溃点 1: 商品已更新、事务尚未提交.
            _maybe_crash(conn, "pre_commit", rid)
            conn.commit()
            # 崩溃点 2: 已提交、结果尚未返回.
            _maybe_crash(conn, "post_commit", rid)

            snapshot = _snapshot_stock(cur)
            return RequestOutcome(
                rid, req["order_id"], req["action"], success, code,
                message, attempts=attempt, stock_snapshot=snapshot,
            )
        except (psycopg2.errors.SerializationFailure,
                psycopg2.errors.DeadlockDetected,
                psycopg2.errors.UniqueViolation) as exc:
            conn.rollback()
            last_conflict = exc
            if attempt >= MAX_ATTEMPTS:
                break
            time.sleep(_backoff(attempt))
            continue
        finally:
            cur.close()

    # 重试耗尽: 返回明确终态码但不落 FAILED 台账 (所有尝试均已回滚, 无法判定结果,
    # 落 FAILED 可能与并发成功者冲突). 调用方用同一 request_id 再次请求即可安全续跑:
    # 若已有赢家提交则走幂等返回其结果, 否则作为首次请求重新执行.
    return RequestOutcome(
        rid, req["order_id"], req["action"], False, ERR_RETRY_EXHAUSTED,
        f"数据库冲突重试 {MAX_ATTEMPTS} 次仍失败: {last_conflict}",
        attempts=MAX_ATTEMPTS,
    )


def connect(dsn: str):
    """新建一个数据库连接 (autocommit 关闭, 由服务层显式提交)."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn
