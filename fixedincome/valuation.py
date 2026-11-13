"""合约估值：逐笔现金流现值、应计、全价、净价。

价格关系（固定本金、固定票息）：
    全价 dirty = Σ 未来每笔现金流金额 × 付款日折现因子
    净价 clean = 全价 - 估值日应计利息
已付现金流（付款日 <= 估值日，含估值日当天，按 plan 视同已付）不计入现值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .curve import PRICE_TOL, DiscountCurve, RESIDUAL_TOL
from .errors import OutOfRangeError
from .schedule import CashFlow, accrued_interest, build_cashflows, build_schedule


@dataclass(frozen=True)
class Valuation:
    contract_id: str
    valuation_date: date
    cashflows: list[CashFlow]
    priced_flows: list[dict]
    dirty_price: float
    clean_price: float
    accrued_interest: float
    current_period_index: int | None
    settled: bool

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "valuation_date": self.valuation_date.isoformat(),
            "settled": self.settled,
            "current_period_index": self.current_period_index,
            "accrued_interest": self.accrued_interest,
            "dirty_price": self.dirty_price,
            "clean_price": self.clean_price,
            "present_value_sum": sum(item["present_value"] for item in self.priced_flows),
            "identity_checks": {
                "clean_plus_accrued_equals_dirty": abs(
                    (self.clean_price + self.accrued_interest) - self.dirty_price
                )
                <= PRICE_TOL,
                "per_cashflow_sum_equals_dirty": abs(
                    sum(item["present_value"] for item in self.priced_flows)
                    - self.dirty_price
                )
                <= PRICE_TOL,
                "tolerance": PRICE_TOL,
            },
            "all_cashflows": [cf.to_dict() for cf in self.cashflows],
            "priced_cashflows": self.priced_flows,
        }


def price_contract(
    contract: dict,
    valuation_date: date,
    calendar,
    curve: DiscountCurve | None,
) -> Valuation:
    """对单个固定息合约定价。

    curve 为 None 仅允许在合约已全部结清（没有未来现金流）时使用；
    任何未来现金流超出曲线支持范围都直接抛 OutOfRangeError。
    """
    periods = build_schedule(
        contract["start_date"],
        contract["maturity_date"],
        contract["coupon_months"],
        contract["end_of_month"],
        calendar,
    )
    cashflows = build_cashflows(contract, calendar)
    accrued, current = accrued_interest(contract, periods, valuation_date)
    current_index = current.index if current is not None else None

    future_flows = [cf for cf in cashflows if cf.payment_date > valuation_date]
    if not future_flows:
        # 到期且付款日均已过（含估值日当天视同已付）：不再需要曲线。
        return Valuation(
            contract_id=contract["identifier"],
            valuation_date=valuation_date,
            cashflows=cashflows,
            priced_flows=[],
            dirty_price=0.0,
            clean_price=0.0,
            accrued_interest=0.0,
            current_period_index=None,
            settled=True,
        )

    if curve is None:
        raise OutOfRangeError(
            f"合约 {contract['identifier']} 仍有未来现金流，但未提供折现曲线",
            contract_id=contract["identifier"],
        )
    last_payment = max(cf.payment_date for cf in future_flows)
    if last_payment > curve.supported_to:
        raise OutOfRangeError(
            f"合约 {contract['identifier']} 最后一笔付款日 {last_payment.isoformat()} "
            f"超出曲线支持范围（截至 {curve.supported_to.isoformat()}），"
            "拒绝靠外推给出价格",
            contract_id=contract["identifier"],
            last_payment_date=last_payment.isoformat(),
            supported_to=curve.supported_to.isoformat(),
        )

    priced: list[dict] = []
    for cf in cashflows:
        if cf.payment_date <= valuation_date:
            continue  # 已付现金流不再计入
        df = curve.discount_factor(cf.payment_date)
        pv = cf.total_amount * df
        priced.append(
            {
                "period_index": cf.period_index,
                "accrual_start": cf.accrual_start.isoformat(),
                "accrual_end": cf.accrual_end.isoformat(),
                "payment_date": cf.payment_date.isoformat(),
                "coupon_amount": cf.coupon_amount,
                "principal_amount": cf.principal_amount,
                "total_amount": cf.total_amount,
                "discount_factor": df,
                "present_value": pv,
            }
        )
    dirty = sum(item["present_value"] for item in priced)
    clean = dirty - accrued
    return Valuation(
        contract_id=contract["identifier"],
        valuation_date=valuation_date,
        cashflows=cashflows,
        priced_flows=priced,
        dirty_price=dirty,
        clean_price=clean,
        accrued_interest=accrued,
        current_period_index=current_index,
        settled=False,
    )


def reprice_bond(bond: dict, valuation_date: date, calendar, curve: DiscountCurve) -> dict:
    """用指定曲线对基准债券重新估值，返回净价/全价与残差（供 inspect 报告）。"""
    valuation = price_contract(bond, valuation_date, calendar, curve)
    target_clean = bond["clean_price"]
    return {
        "quote_id": bond["identifier"],
        "target_clean_price": target_clean,
        "model_clean_price": valuation.clean_price,
        "model_dirty_price": valuation.dirty_price,
        "accrued_interest": valuation.accrued_interest,
        "residual": valuation.clean_price - target_clean,
        "abs_residual": abs(valuation.clean_price - target_clean),
        "within_tolerance": abs(valuation.clean_price - target_clean) <= RESIDUAL_TOL,
    }
