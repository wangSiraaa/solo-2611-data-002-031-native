"""需求点名的异常处置专项：到期已付、错误还本/条款、不可校准输入、扰动缺失报价。

原则：任何无可用解、支点不足或范围外的情形都必须给具体诊断，
绝不返回由任意初值算出的价格。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fixedincome.curve import bootstrap_curve
from fixedincome.errors import (
    CalibrationError,
    ContractValidationError,
    OutOfRangeError,
    ScheduleError,
)
from fixedincome.valuation import price_contract

VAL = date(2025, 2, 14)


def test_matured_fully_paid_contract_is_settled(calendar):
    """到期且付款日均已过的合约：零价格、settled 标记、无需曲线。"""
    contract = {
        "identifier": "PAID",
        "start_date": date(2024, 1, 31),
        "maturity_date": date(2025, 1, 31),
        "coupon_months": 6,
        "coupon_rate": 0.03,
        "principal": 100.0,
        "end_of_month": True,
    }
    # 最后付款日 2025-01-30（修正后续），估值日 2025-02-14 已在其后
    valuation = price_contract(contract, VAL, calendar, None)
    assert valuation.settled is True
    assert valuation.priced_flows == []
    assert valuation.dirty_price == valuation.clean_price == 0.0
    # 历史现金流仍完整保留供审计
    assert len(valuation.cashflows) == 2
    assert valuation.cashflows[-1].principal_amount == 100.0


def test_payment_on_valuation_date_treated_as_paid(calendar):
    """plan 规则：估值日当天的付款视同已付，不计入现值。"""
    contract = {
        "identifier": "SAMEDAY",
        "start_date": date(2024, 8, 14),
        "maturity_date": date(2025, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.025,
        "principal": 100.0,
        "end_of_month": False,
    }
    # 估值日恰为最后付款日 2025-02-14（营业日）
    valuation = price_contract(contract, VAL, calendar, None)
    assert valuation.settled is True
    assert valuation.priced_flows == []
    assert valuation.accrued_interest == 0.0


def test_amortizing_contract_rejected_as_unsupported():
    """分期还本字段（任何形式）必须被识别并拒绝，而不是按固定本金处理。"""
    from fixedincome.schedule import parse_contract

    with pytest.raises(ContractValidationError) as exc:
        parse_contract(
            {
                "contract_id": "AMORT",
                "start_date": "2025-01-31",
                "maturity_date": "2027-01-31",
                "coupon_months": 12,
                "coupon_rate": "0.03",
                "principal": "100",
                "end_of_month": True,
                "amortization": [{"date": "2026-01-31", "amount": "50"}],
            }
        )
    assert exc.value.details["unsupported_fields"] == ["amortization"]


def test_floating_rate_keyword_rejected():
    from fixedincome.schedule import parse_contract

    with pytest.raises(ContractValidationError) as exc:
        parse_contract(
            {
                "contract_id": "FLOAT",
                "start_date": "2025-01-31",
                "maturity_date": "2026-01-31",
                "coupon_months": 12,
                "coupon_rate": "0.03",
                "principal": "100",
                "end_of_month": True,
                "floating_rate_index": "SIM-OIS",
                "reset_margin": "0.005",
            }
        )
    assert "不支持" in exc.value.message


def test_uncalibratable_extreme_quote_gives_specific_diagnostic(calendar):
    """支点存在但报价无解：必须带 bracket 残差的具体诊断，不返回初值价格。"""
    bond = {
        "identifier": "EXTREME",
        "start_date": date(2025, 2, 14),
        "maturity_date": date(2026, 2, 14),
        "coupon_months": 6,
        "coupon_rate": 0.02,
        "principal": 100.0,
        "end_of_month": False,
        "clean_price": -50.0,  # 负净价，合理利率区间内无解
    }
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [bond], calendar)
    assert "无解" in exc.value.message
    detail = exc.value.details
    assert detail["quote_id"] == "EXTREME"
    assert len(detail["residual_at_bracket"]) == 2
    assert "target_dirty_price" in detail


def test_insufficient_pillars_for_long_contract(calendar):
    """支点不足（曲线只覆盖短端）时，长期合约报范围外诊断。"""
    deposits = [
        {"identifier": "D6M", "start_date": VAL,
         "maturity_date": date(2025, 8, 14), "simple_rate": 0.023},
    ]
    result = bootstrap_curve(VAL, deposits, [], calendar)
    long_contract = {
        "identifier": "LONG",
        "start_date": date(2025, 1, 31),
        "maturity_date": date(2027, 1, 31),
        "coupon_months": 12,
        "coupon_rate": 0.03,
        "principal": 100.0,
        "end_of_month": True,
    }
    with pytest.raises(OutOfRangeError) as exc:
        price_contract(long_contract, VAL, calendar, result.curve)
    assert exc.value.details["supported_to"] == "2025-08-14"
    assert "2027" in exc.value.details["last_payment_date"]


def test_empty_market_rejected():
    with pytest.raises(CalibrationError) as exc:
        bootstrap_curve(VAL, [], [], None)
    assert exc.value.diagnostic_code == "calibration_error"


def test_stub_contract_never_silently_extended(calendar):
    """残缺期合约不允许通过任何估值路径被偷偷补期。"""
    contract = {
        "identifier": "STUB",
        "start_date": date(2025, 1, 31),
        "maturity_date": date(2026, 1, 30),
        "coupon_months": 6,
        "coupon_rate": 0.03,
        "principal": 100.0,
        "end_of_month": True,
    }
    deposits = [
        {"identifier": "D", "start_date": VAL,
         "maturity_date": date(2025, 8, 14), "simple_rate": 0.02},
    ]
    result = bootstrap_curve(VAL, deposits, [], calendar)
    with pytest.raises(ScheduleError):
        price_contract(contract, VAL, calendar, result.curve)


def test_scenario_missing_quote_rejected_via_api(calendar):
    """情景引用不存在的报价：在重新校准之前就给明确诊断。"""
    from fixedincome.reports import run_scenario

    plan = {
        "valuation_date": VAL,
        "currency": "SIM",
        "day_count": "ACT/365",
        "no_stub_periods": True,
        "same_day_payment_rule": "payments on valuation date are treated as paid",
        "scenario": {
            "quote_id": "GHOST",
            "field": "clean_price",
            "absolute_shift": 0.05,
        },
    }
    contracts = [
        {
            "identifier": "C", "start_date": date(2025, 2, 14),
            "maturity_date": date(2026, 2, 14), "coupon_months": 6,
            "coupon_rate": 0.025, "principal": 100.0, "end_of_month": False,
        }
    ]
    deposits = [
        {"identifier": "D", "start_date": VAL,
         "maturity_date": date(2025, 8, 14), "simple_rate": 0.023},
    ]
    bonds = [
        {
            "identifier": "B", "start_date": date(2025, 2, 14),
            "maturity_date": date(2026, 2, 14), "coupon_months": 6,
            "coupon_rate": 0.026, "principal": 100.0, "end_of_month": False,
            "clean_price": 99.9,
        }
    ]
    base = bootstrap_curve(VAL, deposits, bonds, calendar)
    with pytest.raises(CalibrationError) as exc:
        run_scenario(plan, contracts, deposits, bonds, calendar, base, None)
    assert "GHOST" in exc.value.message


def test_cli_diagnostic_file_written_for_bad_market(tmp_path):
    """端到端：不可校准市场材料 -> 退出码 2 + 结构化 diagnostic.json。"""
    from fixedincome.cli import main

    alt = tmp_path / "fixtures"
    alt.mkdir()
    market = {
        "deposits": [],
        "bonds": [
            {
                "quote_id": "BAD",
                "start_date": "2025-02-14",
                "maturity_date": "2026-02-14",
                "coupon_months": 6,
                "coupon_rate": "0.02",
                "principal": "100",
                "end_of_month": False,
                "clean_price": "-9999",
            }
        ],
    }
    for name in ("contracts.json", "calendar.json", "plan.json"):
        (alt / name).write_text(
            Path(f"fixtures/{name}").read_text(encoding="utf-8"), encoding="utf-8"
        )
    (alt / "market.json").write_text(json.dumps(market), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = main(["--fixtures-dir", str(alt), "--output-dir", str(out_dir), "inspect"])
    assert rc == 2
    diag = json.loads((out_dir / "diagnostic.json").read_text(encoding="utf-8"))
    assert diag["diagnostic_code"] == "calibration_error"
    assert diag["details"]["quote_id"] == "BAD"
