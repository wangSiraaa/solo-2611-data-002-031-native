"""规范化规则，Python 与最终核验共用同一份实现（单一事实来源）。"""
from __future__ import annotations

from decimal import Decimal


class NormalizeError(ValueError):
    """规范化失败，例如去空白后 code 为空。"""


def normalize_code(raw: str) -> str:
    """去首尾空白，且*仅*把 ASCII 小写 a-z 转成大写。

    非 ASCII 字符（如 é、中、ü）保持原样，不做大小写转换。
    去空白后为空串则抛 NormalizeError —— 空规范 code 属于格式错误。
    """
    s = raw.strip()
    if s == "":
        raise NormalizeError("规范 code 为空（raw_code 去首尾空白后为空串）")
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append(chr(ord(ch) - 32))
        else:
            out.append(ch)
    return "".join(out)


def normalize_amount(amount_cents: int) -> Decimal:
    """分 -> 元，精确十进制运算，绝不经过浮点。"""
    return Decimal(amount_cents).scaleb(-2)
