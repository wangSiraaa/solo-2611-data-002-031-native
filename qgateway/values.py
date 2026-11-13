"""结果值的类型可识别编码。

驱动以原生 Python 类型取出数据（int / Decimal / datetime / date / bytes / None ...），
这里再编码成带 ``t`` 类型标签的 JSON 值。这样精确整数（int8）、NUMERIC、
带时区时间戳、日期与 NULL 在 NDJSON 帧协议里都保持可识别类型，而不是被 JSON
数字/字符串悄悄抹平。客户端与独立核对工具按相同规则解码。
"""
from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any

# 类型标签与 PostgreSQL 类型名保持一致，便于阅读与独立核对
TAG_INT2 = "int2"
TAG_INT4 = "int4"
TAG_INT8 = "int8"
TAG_NUMERIC = "numeric"
TAG_FLOAT4 = "float4"
TAG_FLOAT8 = "float8"
TAG_BOOL = "bool"
TAG_TEXT = "text"
TAG_TIMESTAMPTZ = "timestamptz"
TAG_DATE = "date"
TAG_BYTEA = "bytea"
TAG_JSON = "json"

# PostgreSQL 内置类型 OID -> 标签
_OID_TAG = {
    16: TAG_BOOL,
    17: TAG_BYTEA,
    18: TAG_TEXT,   # char
    19: TAG_TEXT,   # name
    20: TAG_INT8,
    21: TAG_INT2,
    23: TAG_INT4,
    25: TAG_TEXT,
    700: TAG_FLOAT4,
    701: TAG_FLOAT8,
    1082: TAG_DATE,
    1083: TAG_TEXT,
    1114: TAG_TIMESTAMPTZ,  # timestamp（按带时间戳值编码）
    1184: TAG_TIMESTAMPTZ,
    1700: TAG_NUMERIC,
    114: TAG_JSON,
    3802: TAG_JSON,
    2950: TAG_TEXT,  # uuid
}


def encode_value(value: Any, type_oid: int | None = None) -> Any:
    """把驱动返回的 Python 值编码成带类型标签的 JSON 值。

    优先使用列的 PostgreSQL 类型 OID 决定标签（int8 列即便值很小也标 int8），
    未知 OID 再按 Python 类型回退。
    """
    if value is None:
        # NULL：JSON null，列定义另外给出可空性；None 本身与任何非 NULL 标签可区分
        return None
    oid_tag = _OID_TAG.get(type_oid) if type_oid is not None else None
    if oid_tag is not None:
        return _encode_with_tag(value, oid_tag)
    return _encode_fallback(value)


def _encode_with_tag(value: Any, tag: str) -> Any:
    if tag in (TAG_INT2, TAG_INT4, TAG_INT8):
        # 精确整数以 JSON number 承载，标签保证解码回 int 而非浮点
        return {"t": tag, "v": int(value)}
    if tag == TAG_NUMERIC:
        # NUMERIC：保留十进制文本，杜绝二进制浮点误差
        return {"t": TAG_NUMERIC, "v": format(decimal.Decimal(str(value)), "f")}
    if tag == TAG_FLOAT4:
        return {"t": TAG_FLOAT4, "v": float(value)}
    if tag == TAG_FLOAT8:
        return {"t": TAG_FLOAT8, "v": float(value)}
    if tag == TAG_BOOL:
        return {"t": TAG_BOOL, "v": bool(value)}
    if tag == TAG_TIMESTAMPTZ:
        return {"t": TAG_TIMESTAMPTZ, "v": value.isoformat(sep="T", timespec="microseconds")}
    if tag == TAG_DATE:
        return {"t": TAG_DATE, "v": value.isoformat()}
    if tag == TAG_BYTEA:
        import base64

        return {"t": TAG_BYTEA, "v": base64.b64encode(bytes(value)).decode("ascii")}
    if tag == TAG_JSON:
        return {"t": TAG_JSON, "v": value}
    return {"t": TAG_TEXT, "v": str(value)}


def _encode_fallback(value: Any) -> Any:
    if isinstance(value, bool):
        return {"t": TAG_BOOL, "v": value}
    if isinstance(value, int):
        tag = TAG_INT4 if -(2**31) <= value < 2**31 else TAG_INT8
        return {"t": tag, "v": value}
    if isinstance(value, decimal.Decimal):
        return {"t": TAG_NUMERIC, "v": format(value, "f")}
    if isinstance(value, float):
        return {"t": TAG_FLOAT8, "v": value}
    if isinstance(value, _dt.datetime):
        return {"t": TAG_TIMESTAMPTZ, "v": value.isoformat(sep="T", timespec="microseconds")}
    if isinstance(value, _dt.date):
        return {"t": TAG_DATE, "v": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        import base64

        return {"t": TAG_BYTEA, "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (dict, list)):
        return {"t": TAG_JSON, "v": value}
    return {"t": TAG_TEXT, "v": str(value)}


def decode_value(node: Any) -> Any:
    """客户端/核对工具使用的逆编码，还原为带原生类型语义的 Python 值。"""
    if node is None:
        return None
    if not isinstance(node, dict) or "t" not in node:
        # 兼容没有标签的普通 JSON
        return node
    tag, raw = node["t"], node["v"]
    if tag in (TAG_INT2, TAG_INT4, TAG_INT8):
        return int(raw)
    if tag == TAG_NUMERIC:
        return decimal.Decimal(raw)
    if tag in (TAG_FLOAT4, TAG_FLOAT8):
        return float(raw)
    if tag == TAG_BOOL:
        return bool(raw)
    if tag == TAG_TIMESTAMPTZ:
        return _dt.datetime.fromisoformat(raw)
    if tag == TAG_DATE:
        return _dt.date.fromisoformat(raw)
    if tag == TAG_BYTEA:
        import base64

        return base64.b64decode(raw.encode("ascii"))
    if tag == TAG_JSON:
        return raw
    return raw
