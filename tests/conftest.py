"""pytest 共享辅助：用合成输入构建日历、合约、报价与已知曲线。

这些构造不依赖 fixtures 目录中的具体数字，确保实现没有硬编码样例。
"""

from __future__ import annotations

from datetime import date as _date

import pytest

from fixedincome.curve import DiscountCurve
from fixedincome.dates import BusinessCalendar
from fixedincome.schedule import build_cashflows, parse_contract, parse_deposit


@pytest.fixture
def calendar() -> BusinessCalendar:
    # 与 fixtures 相同结构的独立假日表（覆盖周末与若干周中假日）。
    holidays = [
        "2025-01-31", "2025-02-03", "2025-02-04", "2025-05-01", "2025-05-02",
        "2025-10-01", "2026-01-01", "2026-05-01", "2027-01-01",
    ]
    return BusinessCalendar(
        weekend_weekdays=(5, 6),
        holidays=frozenset(_date.fromisoformat(h) for h in holidays),
    )


def make_contract(
    contract_id: str = "C1",
    start: str = "2025-01-31",
    maturity: str = "2027-01-31",
    coupon_months: int = 6,
    coupon_rate: float | str = 0.032,
    principal: float | str = 100.0,
    end_of_month: bool = True,
) -> dict:
    return parse_contract(
        {
            "contract_id": contract_id,
            "start_date": start,
            "maturity_date": maturity,
            "coupon_months": coupon_months,
            "coupon_rate": str(coupon_rate),
            "principal": str(principal),
            "end_of_month": end_of_month,
        }
    )


def make_bond(
    quote_id: str,
    start: str,
    maturity: str,
    coupon_months: int,
    coupon_rate: float | str,
    end_of_month: bool,
    clean_price: float | str,
    principal: float | str = 100.0,
) -> dict:
    return parse_contract(
        {
            "quote_id": quote_id,
            "start_date": start,
            "maturity_date": maturity,
            "coupon_months": coupon_months,
            "coupon_rate": str(coupon_rate),
            "principal": str(principal),
            "end_of_month": end_of_month,
            "clean_price": str(clean_price),
        },
        kind="bond",
    )


def make_deposit(quote_id: str, start: str, maturity: str, rate: float | str) -> dict:
    return parse_deposit(
        {
            "quote_id": quote_id,
            "start_date": start,
            "maturity_date": maturity,
            "simple_rate": str(rate),
        }
    )


def flat_curve(valuation_day: _date, rate: float, pillar_dates: list[_date]) -> DiscountCurve:
    """构造指定平坦连续复利利率的曲线，用于已知曲线小例。"""
    return DiscountCurve(
        valuation_date=valuation_day,
        pillar_dates=tuple(pillar_dates),
        pillar_zero_rates=tuple(rate for _ in pillar_dates),
    )


__all__ = [
    "calendar",
    "make_contract",
    "make_bond",
    "make_deposit",
    "flat_curve",
    "build_cashflows",
]
