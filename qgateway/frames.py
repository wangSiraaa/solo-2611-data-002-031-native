"""逐帧 NDJSON 协议的帧构造与序列化。

帧类型：
  meta     首帧：请求标识、query_id、列定义
  rows     数据帧：一批带类型标签的行
  terminal 唯一终态帧：success / row_budget_exceeded / byte_budget_exceeded /
                       time_limit_exceeded / query_error / cancelled /
                       client_disconnected / send_failed
"""
from __future__ import annotations

import json
from typing import Any

from .values import encode_value

META = "meta"
ROWS = "rows"
TERMINAL = "terminal"

# 成功
T_SUCCESS = "success"
# 超过目录预算
T_ROW_BUDGET = "row_budget_exceeded"
T_BYTE_BUDGET = "byte_budget_exceeded"
T_TIME_LIMIT = "time_limit_exceeded"
# 数据库报错
T_QUERY_ERROR = "query_error"
# 被客户端或关闭流程取消
T_CANCELLED = "cancelled"
# 客户端断连 / 网络发送失败（终态无法送达，仅留在服务状态）
T_CLIENT_DISCONNECTED = "client_disconnected"
T_SEND_FAILED = "send_failed"

TERMINAL_REASONS = frozenset(
    {
        T_SUCCESS,
        T_ROW_BUDGET,
        T_BYTE_BUDGET,
        T_TIME_LIMIT,
        T_QUERY_ERROR,
        T_CANCELLED,
        T_CLIENT_DISCONNECTED,
        T_SEND_FAILED,
    }
)


def meta_frame(
    request_id: str,
    query_id: str,
    columns: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": META,
        "request_id": request_id,
        "query_id": query_id,
        "columns": columns,
        # 回显已绑定的参数，供导出结果独立重放核对；客户端不能借此修改 SQL
        "params": params or {},
    }


def rows_frame(
    seq: int, rows: list[tuple[Any, ...]], column_oids: list[int] | None = None
) -> tuple[dict[str, Any], int]:
    """构造数据帧并返回 (帧, 本帧线上字节数)。字节数按实际 NDJSON 文本计算。"""
    encoded = [
        [encode_value(v, column_oids[idx] if column_oids else None) for idx, v in enumerate(row)]
        for row in rows
    ]
    frame = {"type": ROWS, "seq": seq, "row_count": len(encoded), "rows": encoded}
    return frame, _frame_bytes(frame)


def terminal_frame(
    request_id: str,
    status: str,
    *,
    rows_sent: int = 0,
    bytes_sent: int = 0,
    detail: str | None = None,
    sqlstate: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_REASONS:
        raise ValueError(f"未知终态 {status}")
    frame: dict[str, Any] = {
        "type": TERMINAL,
        "request_id": request_id,
        "status": status,
        "rows_sent": rows_sent,
        "bytes_sent": bytes_sent,
    }
    if detail is not None:
        frame["detail"] = detail
    if sqlstate is not None:
        frame["sqlstate"] = sqlstate
    return frame


def _frame_bytes(frame: dict[str, Any]) -> int:
    # 线上形态为 JSON 文本 + "\n"，字节统计与实际发送保持一致
    return len(json.dumps(frame, ensure_ascii=False).encode("utf-8")) + 1


def serialize(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
