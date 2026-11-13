"""确认解释之后的字段类型转换。

转换只发生在切分确认之后；转换成败绝不反过来影响切分选择。
失败时保留原值与原因。
"""

from __future__ import annotations

import decimal


def convert_field(raw: str, type_name: str) -> dict:
    """按声明类型转换提取到的原始字符串。

    返回 {"raw", "type", "ok", "value", "error"?}：
    - string：原样保留（字符串号保留前导零，如 "001"）。
    - int：十进制整数，JSON 值为数值。
    - decimal：有限精确十进制，JSON 值为其精确文本形式，
      避免经浮点抹掉精度（如 "0.00" 不变成 0.0）。
    """
    result = {"raw": raw, "type": type_name, "ok": True}
    if type_name == "string":
        result["value"] = raw
        return result
    if type_name == "int":
        try:
            result["value"] = int(raw, 10)
        except ValueError:
            result.update(ok=False, value=raw,
                          error=f"cannot convert {raw!r} to int")
        return result
    if type_name == "decimal":
        try:
            value = decimal.Decimal(raw)
        except decimal.InvalidOperation:
            result.update(ok=False, value=raw,
                          error=f"cannot convert {raw!r} to decimal")
            return result
        if not value.is_finite():
            result.update(ok=False, value=raw,
                          error=f"non-finite decimal: {raw!r}")
            return result
        result["value"] = str(value)
        return result
    result.update(ok=False, value=raw, error=f"unknown type {type_name!r}")
    return result
