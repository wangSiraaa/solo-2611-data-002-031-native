"""请求与库存输入的纯函数校验, 以及请求内容指纹.

校验严格按整数处理:
- sku / order_id / request_id 必须为非空字符串
- 数量必须为正整数 (拒绝 bool / float / 负数 / 0)
- 单个订单内 SKU 不得重复
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ACTIONS = ("reserve", "ship", "cancel")

# 内部用于保存"同编号不同内容"拒绝记录的保留后缀, 正常 request_id 不允许使用.
CONFLICT_MARKER_SUFFIX = "::content-conflict"


class ValidationError(ValueError):
    """输入不满足格式约束."""


def is_positive_int(value: Any) -> bool:
    """仅接受真正的 int 正整数; bool 是 int 的子类, 显式拒绝."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {field} 必须为非空字符串")
    return value


def validate_items(items: Any) -> list[dict[str, Any]]:
    """校验商品列表, 返回规范化后的列表 (保持输入顺序)."""
    if not isinstance(items, list) or not items:
        raise ValidationError("items 必须为非空列表")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"items[{idx}] 必须为对象")
        sku = _non_empty_str(item.get("sku"), f"items[{idx}].sku").strip()
        qty = item.get("qty")
        if not is_positive_int(qty):
            raise ValidationError(f"items[{idx}].qty 必须为正整数")
        if sku in seen:
            raise ValidationError(f"订单内 SKU 重复: {sku}")
        seen.add(sku)
        normalized.append({"sku": sku, "qty": qty})
    return normalized


def validate_request(raw: Any) -> dict[str, Any]:
    """校验一条请求 JSON, 返回规范化字典."""
    if not isinstance(raw, dict):
        raise ValidationError("请求必须为 JSON 对象")
    request_id = _non_empty_str(raw.get("request_id"), "request_id").strip()
    if request_id.endswith(CONFLICT_MARKER_SUFFIX):
        raise ValidationError(
            f"request_id 不允许使用保留后缀 {CONFLICT_MARKER_SUFFIX}"
        )
    order_id = _non_empty_str(raw.get("order_id"), "order_id").strip()
    action = raw.get("action")
    if action not in ACTIONS:
        raise ValidationError(f"action 必须为 {ACTIONS} 之一")
    items = validate_items(raw.get("items"))
    return {
        "request_id": request_id,
        "order_id": order_id,
        "action": action,
        "items": items,
    }


def canonical_request(req: dict[str, Any]) -> bytes:
    """请求业务内容的规范化 JSON 字节串.

    只包含业务字段 (request_id 除外): order_id / action / items,
    items 按输入顺序保留; 键排序保证序列化稳定.
    """
    payload = {
        "order_id": req["order_id"],
        "action": req["action"],
        "items": [{"sku": it["sku"], "qty": it["qty"]} for it in req["items"]],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(req: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request(req)).hexdigest()


def validate_stock_row(sku: Any, qty: Any) -> tuple[str, int]:
    """校验 CSV 中的一行库存."""
    sku = _non_empty_str(sku, "sku").strip()
    if not is_nonneg_int(qty):
        raise ValidationError(f"SKU {sku} 的库存必须为非负整数")
    return sku, qty
