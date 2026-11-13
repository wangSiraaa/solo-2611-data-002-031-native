"""批次 JSON 解析、严格校验与规范序列化。

规范形式用于 content_hash：键按固定顺序排列、操作按 seq 升序排列，
保证语义相同的 JSON（空白、键序不同）得到同一哈希。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import BatchValidationError

SIDES = ("L", "R")
ACTIONS = ("put", "delete")
_BATCH_FIELDS = {"batch_id", "operations"}
_OP_FIELDS = {"seq", "action", "side", "row_id", "join_key", "qty", "unit_cents"}
# 64 位 BIGINT 取值范围
_BIGINT_MAX = (1 << 63) - 1
_BIGINT_MIN = -(1 << 63)


class _NoDupHook:
    """object_pairs_hook：拒绝重复键，避免同名字段互相覆盖后再被静默接受。"""

    def __call__(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise BatchValidationError(f"JSON 存在重复键: {key!r}")
            seen.add(key)
            out[key] = value
        return out


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool)


def _check_bigint(value: int, field: str) -> None:
    if not (_BIGINT_MIN <= value <= _BIGINT_MAX):
        raise BatchValidationError(f"字段 {field}={value} 超出 64 位整数范围")


def _require_str(obj: dict[str, Any], field: str) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise BatchValidationError(f"字段 {field!r} 必须是非空字符串")
    return value


def load_batch_document(raw: str | bytes) -> dict[str, Any]:
    """解析并做全部静态校验；任何非法输入直接抛 BatchValidationError。"""
    try:
        doc = json.loads(raw, object_pairs_hook=_NoDupHook())
    except json.JSONDecodeError as exc:
        raise BatchValidationError(f"批次不是合法 JSON: {exc.msg}") from exc
    if not isinstance(doc, dict):
        raise BatchValidationError("批次顶层必须是对象")

    extra = set(doc) - _BATCH_FIELDS
    if extra:
        raise BatchValidationError(f"批次含未知顶层字段: {sorted(extra)}")
    batch_id = _require_str(doc, "batch_id")

    operations = doc.get("operations")
    if not isinstance(operations, list):
        raise BatchValidationError("字段 'operations' 必须是数组")
    if not operations:
        raise BatchValidationError("批次操作列表不能为空")

    validated: list[dict[str, Any]] = []
    seen_seq: set[int] = set()
    for index, op in enumerate(operations):
        where = f"第 {index} 条操作"
        if not isinstance(op, dict):
            raise BatchValidationError(f"{where} 必须是对象")
        unknown = set(op) - _OP_FIELDS
        if unknown:
            raise BatchValidationError(f"{where} 含未知字段: {sorted(unknown)}")

        seq = op.get("seq")
        if not _is_int(seq) or seq <= 0:
            raise BatchValidationError(f"{where} 的 seq 必须是正整数")
        if seq in seen_seq:
            raise BatchValidationError(f"批次内 seq={seq} 重复")
        seen_seq.add(seq)

        action = op.get("action")
        if action not in ACTIONS:
            raise BatchValidationError(f"{where} 的 action 必须是 put/delete")
        side = op.get("side")
        if side not in SIDES:
            raise BatchValidationError(f"{where} 的 side 必须是 L/R")
        row_id = _require_str(op, "row_id")

        clean: dict[str, Any] = {
            "seq": seq,
            "action": action,
            "side": side,
            "row_id": row_id,
        }
        if action == "put":
            join_key = _require_str(op, "join_key")
            if side == "L":
                qty_field, qty_min = "qty", 1
            else:
                qty_field, qty_min = "unit_cents", 0
            value = op.get(qty_field)
            if not _is_int(value):
                raise BatchValidationError(
                    f"{where} 的 {qty_field} 必须是整数（不接受布尔值）"
                )
            if value < qty_min:
                label = "正整数" if qty_min == 1 else "非负整数"
                raise BatchValidationError(f"{where} 的 {qty_field} 必须是{label}")
            _check_bigint(value, qty_field)
            clean["join_key"] = join_key
            clean[qty_field] = value
        else:  # delete
            for forbidden in ("join_key", "qty", "unit_cents"):
                if forbidden in op:
                    raise BatchValidationError(
                        f"{where} 是 delete，不得携带字段 {forbidden!r}"
                    )
        validated.append(clean)

    validated.sort(key=lambda item: item["seq"])
    return {"batch_id": batch_id, "operations": validated}


def canonical_json(doc: dict[str, Any]) -> str:
    """稳定序列化：sort_keys + 固定分隔符，操作已按 seq 升序。"""
    return json.dumps(
        doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def content_hash(doc: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def affected_keys(doc: dict[str, Any]) -> list[str]:
    """静态推导本批触及的全部 join_key。

    put 直接取携带的 join_key；delete 目标行当时的键只能在事务内查，
    这里先按 row_id 占位，apply 阶段把真实键并入集合。
    """
    keys = {
        op["join_key"]
        for op in doc["operations"]
        if op["action"] == "put"
    }
    return sorted(keys)
