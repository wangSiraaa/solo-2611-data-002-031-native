"""Shared synthetic-report domain.

This module is deliberately *not* imported by the helper.  The generated site
uses it; the helper only sees its output through HTTP.  Keeping the business
data generator in one place makes the "independent ground truth" export
comparable with what a browser/helper can actually extract from the pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ORGS = [
    ("ORG_ALPHA", "Alpha 制造"),
    ("ORG_BETA", "Beta 商贸"),
    ("ORG_GAMMA", "Gamma 物流"),
    ("ORG_DELTA", "Delta 研发"),
]

# (machine value, display name) for each semantic report type.
TYPES = [
    ("T_SALES", "销售汇总"),
    ("T_COST", "成本明细"),
    ("T_STOCK", "库存周转"),
    ("T_AUDIT", "审计台账"),
]

MONTHS = [(f"{y}-{m:02d}", f"{y}年{m}月") for y in (2023, 2024) for m in range(1, 13)]


@dataclass(frozen=True)
class Row:
    code: str
    name: str
    amount: str  # plain decimal string, no thousands separators


def _amount(base: int, seed: int, i: int) -> str:
    cents = (base * 100 + seed * 7919 + i * 104729) % 9_999_999
    return f"{cents // 100}.{cents % 100:02d}"


def generate_rows(org_key: str, month: str, type_key: str) -> list[Row]:
    """Deterministic rows for one (org, month, type) query."""
    seed = (sum(map(ord, org_key)) * 31 + int(month[:4]) + int(month[5:]) * 17
            + sum(map(ord, type_key)))
    if type_key == "T_SALES":
        rows = [
            Row(f"S-{ord(org_key[-1]):03d}-{i:02d}", f"渠道{i} 销售额", _amount(100 + i, seed, i))
            for i in range(1, 6)
        ]
        return rows[: 4 + seed % 2]
    if type_key == "T_COST":
        n = 3 + seed % 3
        return [
            Row(f"C-{ord(org_key[-1]):03d}-{i:02d}", f"成本科目{i} 明细", _amount(40 + i, seed, i))
            for i in range(1, n + 1)
        ]
    if type_key == "T_STOCK":
        n = 5 + seed % 3
        return [
            Row(f"I-{ord(org_key[-1]):03d}-{i:02d}", f"物料{i} 库存周转", _amount(20 + i, seed, i))
            for i in range(1, n + 1)
        ]
    n = 2 + seed % 2
    return [
        Row(f"A-{ord(org_key[-1]):03d}-{i:02d}", f"审计事项{i}", _amount(60 + i, seed, i))
        for i in range(1, n + 1)
    ]


def summarize(rows: list[Row]) -> dict:
    total = Decimal("0.00")
    for r in rows:
        total += Decimal(r.amount)
    return {"count": len(rows), "total": f"{total:.2f}"}


def amount_columns(type_key: str) -> list[str]:
    """Column layout for a report type (amount cell is always last)."""
    layouts = {
        "T_SALES": ["编号", "渠道", "销售额"],
        "T_COST": ["科目号", "科目", "金额"],
        "T_STOCK": ["物料号", "物料", "周转金额"],
        "T_AUDIT": ["事项号", "事项", "金额"],
    }
    return layouts[type_key]


def valid_month(month: str) -> bool:
    try:
        y, m = int(month[:4]), int(month[5:])
    except (ValueError, IndexError):
        return False
    return 2023 <= y <= 2024 and 1 <= m <= 12
