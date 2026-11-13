"""CSV validation and change-plan construction.

Validation happens before any terminal contact.  The CSV is only a source of
intent: name/unit/price updates for existing codes.  No rows are added, deleted
or renamed.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

REQUIRED_COLUMNS = ("catalog_code", "name", "unit", "price", "expected_old_name")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# Plain non-negative decimal with at most two fractional digits.
_PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")


class PlanError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("CSV 校验失败")


@dataclass
class ChangeRow:
    line_no: int
    code: str
    name: str
    unit: str
    price: Decimal
    price_text: str
    expected_old_name: str
    raw: dict[str, str] = field(default_factory=dict)


def canonical_price(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), ".2f")


def _clean(value: str | None) -> str:
    return "" if value is None else value


def load_changes(path: str) -> tuple[list[ChangeRow], str]:
    """Read and validate the CSV.  Returns rows + sha256 of file bytes."""
    errors: list[str] = []
    try:
        with open(path, "rb") as handle:
            raw_bytes = handle.read()
    except OSError as exc:
        raise PlanError([f"无法读取 CSV: {exc}"]) from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()

    rows: list[ChangeRow] = []
    seen: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            extra = [c for c in header if c not in REQUIRED_COLUMNS]
            if missing:
                errors.append(f"表头缺少列: {', '.join(missing)}")
            if extra:
                errors.append(f"表头存在未知列: {', '.join(extra)}")
            if errors:
                raise PlanError(errors)

            for offset, record in enumerate(reader, start=2):
                if None in record:
                    errors.append(f"第 {offset} 行: 列数多于表头")
                    continue
                values = {name: _clean(record.get(name)) for name in REQUIRED_COLUMNS}
                row, row_errors = _validate_row(offset, values)
                errors.extend(row_errors)
                if row is None:
                    continue
                if row.code in seen:
                    errors.append(
                        f"第 {offset} 行: 编码 {row.code!r} 与第 {seen[row.code]} 行重复"
                    )
                    continue
                seen[row.code] = offset
                rows.append(row)
    except PlanError:
        raise
    except (csv.Error, UnicodeDecodeError) as exc:
        raise PlanError([f"CSV 解析失败: {exc}"]) from exc

    if not rows and not errors:
        errors.append("CSV 没有数据行")
    if errors:
        raise PlanError(errors)
    return rows, digest


def _validate_row(line_no: int, values: dict[str, str]) -> tuple[ChangeRow | None, list[str]]:
    errors: list[str] = []

    def require(column: str, label: str) -> str:
        value = values[column]
        if not value:
            errors.append(f"第 {line_no} 行: {label}不能为空")
            return value
        if value != value.strip():
            errors.append(f"第 {line_no} 行: {label}首尾不能有空白")
        if _CONTROL_RE.search(value):
            errors.append(f"第 {line_no} 行: {label}含控制字符")
        return value

    code = require("catalog_code", "编码")
    name = require("name", "名称")
    unit = require("unit", "单位")
    expected = require("expected_old_name", "预期旧名称")

    price_text = values["price"]
    price_value: Decimal | None = None
    if not price_text:
        errors.append(f"第 {line_no} 行: 价格不能为空")
    else:
        if price_text != price_text.strip():
            errors.append(f"第 {line_no} 行: 价格首尾不能有空白")
        if not _PRICE_RE.fullmatch(price_text):
            errors.append(f"第 {line_no} 行: 价格须为非负十进制且最多两位小数")
        else:
            try:
                price_value = Decimal(price_text)
            except InvalidOperation:
                errors.append(f"第 {line_no} 行: 价格不是合法数字")
            else:
                if not price_value.is_finite():
                    errors.append(f"第 {line_no} 行: 价格必须有限")
                    price_value = None
                elif price_value < 0:
                    errors.append(f"第 {line_no} 行: 价格不能为负")
                elif price_value != price_value.quantize(Decimal("0.01")):
                    errors.append(f"第 {line_no} 行: 价格最多两位小数")
    if _CONTROL_RE.search(price_text):
        errors.append(f"第 {line_no} 行: 价格含控制字符")

    if errors or price_value is None:
        return None, errors
    return (
        ChangeRow(
            line_no=line_no,
            code=code,
            name=name,
            unit=unit,
            price=price_value,
            price_text=canonical_price(price_value),
            expected_old_name=expected,
            raw=values,
        ),
        [],
    )
