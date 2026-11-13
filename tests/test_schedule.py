"""计息日程、现金流与残缺期处置。"""

from __future__ import annotations

from datetime import date

import pytest

from fixedincome.errors import ContractValidationError, ScheduleError
from fixedincome.schedule import (
    accrued_interest,
    build_cashflows,
    build_schedule,
    parse_contract,
)


def test_schedule_boundaries_vs_payment_dates_kept_separate(calendar):
    contract = parse_contract(
        {
            "contract_id": "A",
            "start_date": "2025-01-31",
            "maturity_date": "2027-01-31",
            "coupon_months": 6,
            "coupon_rate": "0.032",
            "principal": "100",
            "end_of_month": True,
        }
    )
    periods = build_schedule(
        contract["start_date"], contract["maturity_date"],
        contract["coupon_months"], contract["end_of_month"], calendar,
    )
    # 4 个完整半年期
    assert [p.accrual_end for p in periods] == [
        date(2025, 7, 31), date(2026, 1, 31), date(2026, 7, 31), date(2027, 1, 31)
    ]
    # 付款日经过修正后续，且 2026-01-31 / 2027-01-31 与计息边界不同
    assert [p.payment_date for p in periods] == [
        date(2025, 7, 31), date(2026, 1, 30), date(2026, 7, 31), date(2027, 1, 29)
    ]
    assert periods[1].accrual_end != periods[1].payment_date
    assert periods[3].accrual_end != periods[3].payment_date


def test_cashflow_amounts_act365_and_single_redemption(calendar):
    contract = parse_contract(
        {
            "contract_id": "A",
            "start_date": "2025-01-31",
            "maturity_date": "2027-01-31",
            "coupon_months": 6,
            "coupon_rate": "0.032",
            "principal": "100",
            "end_of_month": True,
        }
    )
    flows = build_cashflows(contract, calendar)
    assert len(flows) == 4
    # 仅最后一期含还本，且还本金额等于固定本金
    assert sum(cf.principal_amount for cf in flows) == 100.0
    assert flows[-1].principal_amount == 100.0
    assert all(cf.principal_amount == 0.0 for cf in flows[:-1])
    # 票息 = 本金 × 票息 × 当期 ACT/365
    assert flows[0].coupon_amount == pytest.approx(100 * 0.032 * 181 / 365)
    assert flows[1].coupon_amount == pytest.approx(100 * 0.032 * 184 / 365)
    assert all(cf.total_amount == cf.coupon_amount + cf.principal_amount for cf in flows)


def test_undeclared_stub_period_is_rejected(calendar):
    # 起息 2025-01-31，到期 2026-01-30：半年滚动的规则边界为 7/31、1/31，
    # 到期日落在两个规则边界之间 -> 残缺期，拒绝而不是补一期
    with pytest.raises(ScheduleError) as exc:
        build_schedule(date(2025, 1, 31), date(2026, 1, 30), 6, True, calendar)
    assert "残缺期" in exc.value.message
    # 诊断给出最后一个合法规则边界与越过点，且越过点不得被补进日程
    assert exc.value.details["last_regular_boundary"] == "2025-07-31"
    assert exc.value.details["overshoot_boundary"] == "2026-01-31"


def test_stub_rejection_via_contract(calendar):
    bad = {
        "contract_id": "BAD",
        "start_date": "2025-02-14",
        "maturity_date": "2025-11-14",  # 9 个月：规则边界 8/14 之后下一跳 2026-02-14
        "coupon_months": 6,             # 已越过到期日 -> 残缺，绝不补成两期
        "coupon_rate": "0.025",
        "principal": "100",
        "end_of_month": False,
    }
    with pytest.raises(ScheduleError) as exc:
        build_cashflows(parse_contract(bad), calendar)
    assert exc.value.details["last_regular_boundary"] == "2025-08-14"
    assert exc.value.details["overshoot_boundary"] == "2026-02-14"


def test_accrued_interest_mid_period(calendar):
    contract = parse_contract(
        {
            "contract_id": "B",
            "start_date": "2025-02-14",
            "maturity_date": "2026-02-14",
            "coupon_months": 6,
            "coupon_rate": "0.025",
            "principal": "100",
            "end_of_month": False,
        }
    )
    periods = build_schedule(
        contract["start_date"], contract["maturity_date"],
        contract["coupon_months"], contract["end_of_month"], calendar,
    )
    valuation = date(2025, 5, 14)  # 恰在第一期中间
    accrued, current = accrued_interest(contract, periods, valuation)
    assert current.index == 1
    assert accrued == pytest.approx(100 * 0.025 * 89 / 365)


def test_accrued_zero_before_start_and_on_payment_boundary(calendar):
    contract = parse_contract(
        {
            "contract_id": "B",
            "start_date": "2025-02-14",
            "maturity_date": "2026-02-14",
            "coupon_months": 6,
            "coupon_rate": "0.025",
            "principal": "100",
            "end_of_month": False,
        }
    )
    periods = build_schedule(
        contract["start_date"], contract["maturity_date"],
        contract["coupon_months"], contract["end_of_month"], calendar,
    )
    accrued, current = accrued_interest(contract, periods, date(2025, 2, 14))
    assert accrued == 0.0 and current is None
    # 期末边界当天视同支付，应计归零并指向下一期判定
    accrued, current = accrued_interest(contract, periods, date(2025, 8, 14))
    assert accrued == 0.0
    assert current is not None and current.index == 1
    # 到期之后
    accrued, current = accrued_interest(contract, periods, date(2026, 3, 1))
    assert accrued == 0.0 and current is None


def test_unsupported_contract_fields_rejected():
    with pytest.raises(ContractValidationError) as exc:
        parse_contract(
            {
                "contract_id": "X",
                "start_date": "2025-01-31",
                "maturity_date": "2026-01-31",
                "coupon_months": 12,
                "coupon_rate": "0.02",
                "principal": "100",
                "end_of_month": True,
                "amortization_schedule": [],
            }
        )
    assert "不支持" in exc.value.message


def test_negative_coupon_rejected():
    with pytest.raises(ContractValidationError):
        parse_contract(
            {
                "contract_id": "X",
                "start_date": "2025-01-31",
                "maturity_date": "2026-01-31",
                "coupon_months": 12,
                "coupon_rate": "-0.01",
                "principal": "100",
                "end_of_month": True,
            }
        )


def test_invalid_principal_and_dates_rejected():
    base = {
        "contract_id": "X",
        "start_date": "2025-01-31",
        "maturity_date": "2026-01-31",
        "coupon_months": 12,
        "coupon_rate": "0.02",
        "principal": "0",
        "end_of_month": True,
    }
    with pytest.raises(ContractValidationError):
        parse_contract(dict(base))
    base["principal"] = "100"
    base["maturity_date"] = "2025-01-30"
    with pytest.raises(ContractValidationError):
        parse_contract(dict(base))
