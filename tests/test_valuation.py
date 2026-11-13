"""估值关系：应计/全价/净价、独立现金流求和、已付剔除、范围外拒绝。"""

from __future__ import annotations

from datetime import date

import pytest

from fixedincome.curve import PRICE_TOL, DiscountCurve
from fixedincome.errors import OutOfRangeError
from fixedincome.valuation import price_contract


def _curve(valuation_day: date) -> DiscountCurve:
    return DiscountCurve(
        valuation_date=valuation_day,
        pillar_dates=(date(2025, 8, 14), date(2027, 2, 16)),
        pillar_zero_rates=(0.025, 0.03),
    )


def test_clean_dirty_accrued_identity(calendar):
    contract = {
        "identifier": "B",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.025,
        "principal": 100.0,
        "end_of_month": False,
    }
    valuation = price_contract(contract, date(2025, 5, 14), calendar, _curve(date(2025, 5, 14)))
    # 独立求和核对：逐笔 PV 之和 == 全价；全价 - 应计 == 净价
    independent_sum = sum(
        cf["total_amount"] * cf["discount_factor"] for cf in valuation.priced_flows
    )
    assert independent_sum == pytest.approx(valuation.dirty_price, abs=PRICE_TOL)
    assert valuation.clean_price + valuation.accrued_interest == pytest.approx(
        valuation.dirty_price, abs=PRICE_TOL
    )
    assert valuation.accrued_interest == pytest.approx(100 * 0.025 * 89 / 365)


def test_paid_cashflows_excluded_on_and_before_valuation(calendar):
    # 估值日取第二期付款日 2026-02-16（修正后续后的实际付款日）：所有现金流视同已付
    contract = {
        "identifier": "B",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.025,
        "principal": 100.0,
        "end_of_month": False,
    }
    valuation = price_contract(
        contract, date(2026, 2, 16), calendar, _curve(date(2026, 2, 16))
    )
    assert valuation.settled is True
    assert valuation.priced_flows == []
    assert valuation.dirty_price == 0.0
    assert valuation.clean_price == 0.0
    assert valuation.accrued_interest == 0.0
    # 全部现金流仍可在 all_cashflows 中审计
    assert len(valuation.cashflows) == 2


def test_matured_contract_prices_without_curve(calendar):
    contract = {
        "identifier": "OLD",
        "start_date": date(2024, 1, 31),
        "maturity_date": date(2025, 1, 31),
        "coupon_months": 6,
        "coupon_rate": 0.03,
        "principal": 100.0,
        "end_of_month": True,
    }
    valuation = price_contract(contract, date(2025, 2, 14), calendar, None)
    assert valuation.settled is True
    assert valuation.dirty_price == valuation.clean_price == 0.0


def test_out_of_range_contract_rejected(calendar):
    contract = {
        "identifier": "LONG",
        "start_date": date(2025, 1, 31),
        "maturity_date": date(2028, 1, 31),  # 超出 2027-01-29 的曲线末端
        "coupon_months": 12,
        "coupon_rate": 0.03,
        "principal": 100.0,
        "end_of_month": True,
    }
    with pytest.raises(OutOfRangeError) as exc:
        price_contract(contract, date(2025, 2, 14), calendar, _curve(date(2025, 2, 14)))
    assert "超出曲线支持范围" in exc.value.message
    assert exc.value.details["supported_to"] == "2027-02-16"


def test_discount_factor_at_valuation_is_one(calendar):
    curve = _curve(date(2025, 5, 14))
    assert curve.discount_factor(date(2025, 5, 14)) == 1.0


def test_negative_rates_allow_df_above_one(calendar):
    # 负的连续复利零息利率 -> DF > 1，曲线不得因此报错
    curve = DiscountCurve(
        valuation_date=date(2025, 2, 14),
        pillar_dates=(date(2025, 8, 14), date(2026, 2, 16)),
        pillar_zero_rates=(-0.01, -0.015),
    )
    assert curve.discount_factor(date(2025, 8, 14)) > 1.0
    contract = {
        "identifier": "NEG",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.01,
        "principal": 100.0,
        "end_of_month": False,
    }
    valuation = price_contract(contract, date(2025, 2, 14), calendar, curve)
    # 负利率 + 低票息：全价高于本金现金流总和属正常
    assert valuation.dirty_price > sum(cf.total_amount for cf in valuation.cashflows) * 0.999
    assert valuation.clean_price == pytest.approx(valuation.dirty_price)
