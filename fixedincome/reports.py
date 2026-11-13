"""报告组装与落盘。

报告为自描述 JSON：包含输入摘要、方法与容差、校准曲线、报价残差、逐笔现金流
以及（情景下）扰动前后两套重新校准结果与逐现金流价差。
"""

from __future__ import annotations

import json
import os

from .curve import PRICE_TOL, RESIDUAL_TOL, SOLVER_BRACKET, SOLVER_RTOL, SOLVER_XTOL
from .curve import CalibrationResult, bootstrap_curve
from .errors import CalibrationError
from .valuation import Valuation, price_contract


def method_summary() -> dict:
    return {
        "day_count": "ACT/365 Fixed",
        "accrual_boundaries": "unadjusted month-rolled calendar dates (end-of-month convention supported)",
        "payment_adjustment": "modified following on the supplied holiday calendar",
        "stub_policy": "reject undeclared stub periods; no silent extra period",
        "curve_representation": "continuously-compounded zero rates, linear interpolation between pillars",
        "curve_short_end": "flat to first pillar; DF(0)=1",
        "curve_long_end": "no extrapolation beyond last pillar; out-of-range contracts are rejected",
        "deposit_pricing": "simple interest: DF = 1 / (1 + rate * ACT/365)",
        "bond_pricing": "dirty = clean + accrued; dirty equals PV of remaining cashflows",
        "solver": {
            "method": "scipy.optimize.brentq",
            "bracket": list(SOLVER_BRACKET),
            "xtol": SOLVER_XTOL,
            "rtol": SOLVER_RTOL,
            "note": "negative rates are allowed; discount factors may exceed 1",
        },
        "tolerances": {
            "solver_xtol": SOLVER_XTOL,
            "residual_tolerance": RESIDUAL_TOL,
            "price_identity_tolerance": PRICE_TOL,
        },
    }


def valuation_section(valuation: Valuation) -> dict:
    return valuation.to_dict()


def save_report(output_dir: str, name: str, payload: dict) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
    return path


def run_scenario(
    plan: dict,
    contracts: list[dict],
    deposits: list[dict],
    bonds: list[dict],
    calendar,
    base_result: CalibrationResult,
    target_contract_ids: list[str] | None,
):
    """按 plan 对一条报价做绝对扰动，重新校准曲线后重估目标合约。

    正式情景严禁直接改最终价格或单个折现因子：这里完整复制报价集、修改报价后
    重新执行 bootstrap_curve，再重估合约，并逐现金流给出价差。
    """
    scenario = plan["scenario"]
    quote_id = scenario["quote_id"]
    shift = scenario["absolute_shift"]
    target = next((b for b in bonds if b["identifier"] == quote_id), None)
    if target is None:
        raise CalibrationError(
            f"情景报价 {quote_id} 不在市场材料的债券报价中，无法施加扰动",
            quote_id=quote_id,
            available=[b["identifier"] for b in bonds],
        )

    shocked_bonds = []
    for bond in bonds:
        if bond["identifier"] == quote_id:
            shocked = dict(bond)
            shocked["clean_price"] = bond["clean_price"] + shift
            shocked_bonds.append(shocked)
        else:
            shocked_bonds.append(dict(bond))
    shocked_result = bootstrap_curve(
        plan["valuation_date"],
        [dict(d) for d in deposits],
        shocked_bonds,
        calendar,
    )

    selected = contracts
    if target_contract_ids:
        unknown = set(target_contract_ids) - {c["identifier"] for c in contracts}
        if unknown:
            raise CalibrationError(
                f"情景目标合约不存在: {sorted(unknown)}",
                unknown_contracts=sorted(unknown),
            )
        selected = [c for c in contracts if c["identifier"] in target_contract_ids]

    contract_reports = []
    for contract in selected:
        base_val = price_contract(
            contract, plan["valuation_date"], calendar, base_result.curve
        )
        shock_val = price_contract(
            contract, plan["valuation_date"], calendar, shocked_result.curve
        )
        base_by_period = {item["period_index"]: item for item in base_val.priced_flows}
        shock_by_period = {item["period_index"]: item for item in shock_val.priced_flows}
        all_indices = sorted(set(base_by_period) | set(shock_by_period))
        per_flow = []
        for idx in all_indices:
            b = base_by_period.get(idx)
            s = shock_by_period.get(idx)
            per_flow.append(
                {
                    "period_index": idx,
                    "payment_date": (s or b)["payment_date"],
                    "total_amount": (s or b)["total_amount"],
                    "base_discount_factor": None if b is None else b["discount_factor"],
                    "shocked_discount_factor": None if s is None else s["discount_factor"],
                    "base_present_value": None if b is None else b["present_value"],
                    "shocked_present_value": None if s is None else s["present_value"],
                    "present_value_change": (
                        0.0
                        if b is None or s is None
                        else s["present_value"] - b["present_value"]
                    ),
                }
            )
        flow_sum_change = sum(item["present_value_change"] for item in per_flow)
        dirty_change = shock_val.dirty_price - base_val.dirty_price
        contract_reports.append(
            {
                "contract_id": contract["identifier"],
                "base": {
                    "dirty_price": base_val.dirty_price,
                    "clean_price": base_val.clean_price,
                    "accrued_interest": base_val.accrued_interest,
                },
                "shocked": {
                    "dirty_price": shock_val.dirty_price,
                    "clean_price": shock_val.clean_price,
                    "accrued_interest": shock_val.accrued_interest,
                },
                "price_changes": {
                    "dirty_price_change": dirty_change,
                    "clean_price_change": shock_val.clean_price - base_val.clean_price,
                    "accrued_interest_change": (
                        shock_val.accrued_interest - base_val.accrued_interest
                    ),
                },
                "per_cashflow_price_change": per_flow,
                "reconciliation": {
                    "sum_of_per_cashflow_changes": flow_sum_change,
                    "dirty_price_change": dirty_change,
                    "difference": flow_sum_change - dirty_change,
                    "tolerance": PRICE_TOL,
                    "passes": abs(flow_sum_change - dirty_change) <= PRICE_TOL,
                },
            }
        )

    return shocked_result, {
        "applied_scenario": {
            "quote_id": quote_id,
            "field": scenario["field"],
            "absolute_shift": shift,
            "base_quote": target["clean_price"],
            "shocked_quote": target["clean_price"] + shift,
        },
        "base_calibration": {
            "curve": base_result.curve.to_summary(),
            "fitting_check": base_result.to_summary(),
        },
        "shocked_calibration": {
            "curve": shocked_result.curve.to_summary(),
            "fitting_check": shocked_result.to_summary(),
        },
        "target_contracts": contract_reports,
    }
