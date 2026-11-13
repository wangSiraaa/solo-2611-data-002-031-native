"""曲线自举校准：已知曲线小例、残差核对、负利率与各类不可校准诊断。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from fixedincome.curve import (
    RESIDUAL_TOL,
    DiscountCurve,
    bootstrap_curve,
)
from fixedincome.errors import CalibrationError
from fixedincome.valuation import price_contract

VAL = date(2025, 2, 14)


def _clean_price_from_curve(bond: dict, curve: DiscountCurve, calendar) -> float:
    """用已知曲线给债券生成理论净价，作为合成报价来源。"""
    valuation = price_contract(bond, VAL, calendar, curve)
    return valuation.clean_price


def test_known_curve_quotes_calibrate_back_with_small_residuals(calendar):
    # 给定一条已知（分段线性零息利率）曲线，给存款与债券造一组小报价，
    # 校准后必须还原曲线且重报残差极小。
    known = DiscountCurve(
        valuation_date=VAL,
        pillar_dates=(
            date(2025, 5, 14),
            date(2025, 8, 14),
            date(2026, 2, 16),
            date(2027, 2, 15),
        ),
        pillar_zero_rates=(0.021, 0.023, 0.026, 0.030),
    )
    # 存款简单利率由已知连续复利零息利率反解，保证两套工具描述同一条曲线：
    # simple = (exp(r*t) - 1) / t
    def _simple_rate(zero_rate: float, maturity_day: date) -> float:
        yf = (maturity_day - VAL).days / 365
        return (np.exp(zero_rate * yf) - 1.0) / yf

    deposits = [
        {
            "identifier": "DEP-3M",
            "start_date": VAL,
            "maturity_date": date(2025, 5, 14),
            "simple_rate": _simple_rate(0.021, date(2025, 5, 14)),
        },
        {
            "identifier": "DEP-6M",
            "start_date": VAL,
            "maturity_date": date(2025, 8, 14),
            "simple_rate": _simple_rate(0.023, date(2025, 8, 14)),
        },
    ]
    bond_1y = {
        "identifier": "B1",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.026,
        "principal": 100.0,
        "end_of_month": False,
    }
    bond_2y = {
        "identifier": "B2",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2027, 2, 14),
        "coupon_months": 12,
        "coupon_rate": 0.031,
        "principal": 100.0,
        "end_of_month": False,
    }
    bond_1y["clean_price"] = _clean_price_from_curve(bond_1y, known, calendar)
    bond_2y["clean_price"] = _clean_price_from_curve(bond_2y, known, calendar)

    result = bootstrap_curve(VAL, deposits, [bond_1y, bond_2y], calendar)

    # 1) 支点日期与到期付款日一致（2027-02-14 为周日，修正后续至 02-15）
    assert result.curve.pillar_dates == (
        date(2025, 5, 14), date(2025, 8, 14),
        date(2026, 2, 16), date(2027, 2, 15),
    )
    # 2) 所有重报残差显著小于容忍
    assert max(abs(r.residual) for r in result.residuals) < 1e-10
    # 3) 还原的支点零息利率与已知曲线一致。存款支点给的是简单利率，
    #    需先换算成连续复利零息利率再比较；债券支点直接等于已知零息利率。
    from fixedincome.dates import year_fraction

    for dep, pillar_day in (
        (deposits[0], date(2025, 5, 14)),
        (deposits[1], date(2025, 8, 14)),
    ):
        yf = year_fraction(VAL, pillar_day)
        expected_zero = -np.log(1.0 / (1.0 + dep["simple_rate"] * yf)) / yf
        assert result.curve.zero_rate(pillar_day) == pytest.approx(
            expected_zero, abs=1e-12
        )
    for day, expected in (
        (date(2026, 2, 16), 0.026),
        (date(2027, 2, 15), 0.030),
    ):
        assert result.curve.zero_rate(day) == pytest.approx(expected, abs=1e-9)
    # 4) 全量重报检查结果同样在容忍内
    assert result.to_summary()["max_abs_residual"] < RESIDUAL_TOL


def test_deposit_only_calibration_analytic(calendar):
    deposits = [
        {"identifier": "D1", "start_date": VAL, "maturity_date": date(2025, 8, 14),
         "simple_rate": 0.023},
    ]
    result = bootstrap_curve(VAL, deposits, [], calendar)
    yf = (date(2025, 8, 14) - VAL).days / 365
    expected_df = 1 / (1 + 0.023 * yf)
    assert result.curve.discount_factor(date(2025, 8, 14)) == pytest.approx(
        expected_df, abs=1e-14
    )
    assert result.residuals[0].residual == pytest.approx(0.0, abs=1e-15)


def test_negative_rate_calibration_allows_df_above_one(calendar):
    # 存款给负简单利率：解析 DF > 1，必须被接受
    deposits = [
        {"identifier": "DNEG", "start_date": VAL, "maturity_date": date(2025, 8, 14),
         "simple_rate": -0.01},
    ]
    result = bootstrap_curve(VAL, deposits, [], calendar)
    assert result.curve.discount_factor(date(2025, 8, 14)) > 1.0
    assert result.curve.zero_rate(date(2025, 8, 14)) < 0.0

    # 负利率环境下的债券（高净价）也应可校准
    known = DiscountCurve(
        valuation_date=VAL,
        pillar_dates=(date(2025, 8, 14), date(2026, 2, 16)),
        pillar_zero_rates=(-0.012, -0.008),
    )
    bond = {
        "identifier": "BNEG",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.005,
        "principal": 100.0,
        "end_of_month": False,
        "clean_price": 0.0,
    }
    bond["clean_price"] = _clean_price_from_curve(bond, known, calendar)
    result2 = bootstrap_curve(VAL, deposits, [bond], calendar)
    assert max(abs(r.residual) for r in result2.residuals) < 1e-10
    assert all(r < 0.01 for r in result2.curve.pillar_zero_rates)


def test_no_instruments_rejected():
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [], None)
    assert "没有任何可校准工具" in exc.value.message


def test_forward_start_deposit_rejected(calendar):
    deposits = [
        {"identifier": "FWD", "start_date": date(2025, 3, 1),
         "maturity_date": date(2025, 9, 1), "simple_rate": 0.02},
    ]
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, deposits, [], calendar)
    assert "不是估值日" in exc.value.message


def test_duplicate_pillar_rejected(calendar):
    # 两只债券到期付款日相同 -> 重复支点冲突
    bond_a = {
        "identifier": "BA", "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14), "coupon_months": 6,
        "coupon_rate": 0.02, "principal": 100.0, "end_of_month": False,
        "clean_price": 100.0,
    }
    bond_b = dict(bond_a, identifier="BB", clean_price=99.0)
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [bond_a, bond_b], calendar)
    assert "重复" in exc.value.message


def test_inconsistent_bond_quote_no_solution_diagnostic(calendar):
    # 第一只债券把 1Y 支点定在合理水平；第二只给一个极端净价，
    # 使其在求根区间两端都无法匹配 -> 必须给无解诊断，而不是返回初值价格。
    bond_ok = {
        "identifier": "BOK", "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14), "coupon_months": 6,
        "coupon_rate": 0.026, "principal": 100.0, "end_of_month": False,
        "clean_price": 100.0,
    }
    bond_bad = {
        "identifier": "BBAD", "start_date": date(2025, 2, 14),
        "maturity_date": date(2027, 2, 14), "coupon_months": 12,
        "coupon_rate": 0.03, "principal": 100.0, "end_of_month": False,
        "clean_price": 100000.0,  # 远超任何合理利率区间能达到的现值
    }
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [bond_ok, bond_bad], calendar)
    assert "无解" in exc.value.message
    assert exc.value.details["quote_id"] == "BBAD"
    # 诊断包含两端残差，可审计
    assert "residual_at_bracket" in exc.value.details


def test_bond_without_future_cashflows_rejected(calendar):
    bond_old = {
        "identifier": "BOLD", "start_date": date(2024, 2, 14),
        "maturity_date": date(2025, 2, 14), "coupon_months": 6,
        "coupon_rate": 0.02, "principal": 100.0, "end_of_month": False,
        "clean_price": 100.0,
    }
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [bond_old], calendar)
    assert "没有任何现金流" in exc.value.message


def test_bond_repricing_uses_same_calendar_and_rules(calendar):
    # 校准债券与目标合约共用同一套日程函数：用含月底/假日的债券验证
    bond = {
        "identifier": "BEOM", "start_date": date(2025, 1, 31),
        "maturity_date": date(2026, 1, 31), "coupon_months": 6,
        "coupon_rate": 0.026, "principal": 100.0, "end_of_month": True,
        "clean_price": 99.9,
    }
    deposits = [
        {"identifier": "D3", "start_date": VAL, "maturity_date": date(2025, 5, 14),
         "simple_rate": 0.021},
    ]
    result = bootstrap_curve(VAL, deposits, [bond], calendar)
    residual = next(r for r in result.residuals if r.quote_id == "BEOM")
    assert abs(residual.residual) < RESIDUAL_TOL
    # 债券最后付款日确实经过修正后续（2026-01-31 周六 -> 01-30）
    assert result.curve.pillar_dates[-1] == date(2026, 1, 30)


def test_calibrated_curve_rejects_extrapolation():
    curve = DiscountCurve(
        valuation_date=VAL,
        pillar_dates=(date(2025, 8, 14),),
        pillar_zero_rates=(0.02,),
    )
    from fixedincome.errors import OutOfRangeError

    with pytest.raises(OutOfRangeError):
        curve.zero_rate(date(2026, 8, 14))
    # 首支点之前平坦外推，不报错
    assert curve.zero_rate(date(2025, 3, 1)) == pytest.approx(0.02)
