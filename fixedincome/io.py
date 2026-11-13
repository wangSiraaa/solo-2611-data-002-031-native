"""输入加载与校验。

所有金额、利率、报价在 fixtures 中都是十进制字符串，这里统一经 Decimal 转 float，
避免浮点解析歧义；校验只接受单币种、固定本金、固定票息的输入子集。
"""

from __future__ import annotations

import json

from .dates import BusinessCalendar, parse_date
from .errors import ContractValidationError
from .schedule import parse_contract, parse_deposit


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_calendar(path: str) -> BusinessCalendar:
    return BusinessCalendar.from_dict(_read_json(path))


def load_contracts(path: str) -> list[dict]:
    raw = _read_json(path)
    if not isinstance(raw, list) or not raw:
        raise ContractValidationError("contracts.json 必须是非空数组")
    contracts = [parse_contract(item, kind="contract") for item in raw]
    ids = [c["identifier"] for c in contracts]
    if len(set(ids)) != len(ids):
        raise ContractValidationError("合约 contract_id 存在重复", ids=ids)
    return contracts


def load_market(path: str) -> tuple[list[dict], list[dict]]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ContractValidationError("market.json 必须是对象")
    deposits = [parse_deposit(item) for item in raw.get("deposits", [])]
    bonds = [parse_contract(item, kind="bond") for item in raw.get("bonds", [])]
    quote_ids = [d["identifier"] for d in deposits] + [b["identifier"] for b in bonds]
    if len(set(quote_ids)) != len(quote_ids):
        raise ContractValidationError("市场报价 quote_id 存在重复", quote_ids=quote_ids)
    return deposits, bonds


def load_plan(path: str) -> dict:
    raw = _read_json(path)
    required = {"valuation_date", "currency", "day_count", "scenario"}
    missing = required - set(raw)
    if missing:
        raise ContractValidationError(f"plan.json 缺少字段: {sorted(missing)}")
    if raw["day_count"] != "ACT/365":
        raise ContractValidationError(
            f"本工具只支持 ACT/365，plan 声明了 {raw['day_count']!r}"
        )
    scenario = raw["scenario"]
    for key in ("quote_id", "field", "absolute_shift"):
        if key not in scenario:
            raise ContractValidationError(f"plan.scenario 缺少字段 {key}")
    if scenario["field"] != "clean_price":
        raise ContractValidationError(
            f"情景扰动只支持 clean_price，收到 {scenario['field']!r}"
        )
    from decimal import Decimal

    return {
        "valuation_date": parse_date(raw["valuation_date"]),
        "currency": raw["currency"],
        "day_count": raw["day_count"],
        "no_stub_periods": bool(raw.get("no_stub_periods", True)),
        "same_day_payment_rule": raw.get(
            "same_day_payment_rule",
            "payments on valuation date are treated as paid",
        ),
        "scenario": {
            "quote_id": scenario["quote_id"],
            "field": scenario["field"],
            "absolute_shift": float(Decimal(str(scenario["absolute_shift"]))),
        },
    }


def input_summary(
    plan: dict,
    contracts: list[dict],
    deposits: list[dict],
    bonds: list[dict],
    calendar: BusinessCalendar,
) -> dict:
    """随报告保存的参数与输入摘要（不含求解结果，只含声明值）。"""
    return {
        "valuation_date": plan["valuation_date"].isoformat(),
        "currency": plan["currency"],
        "day_count": plan["day_count"],
        "no_stub_periods": plan["no_stub_periods"],
        "same_day_payment_rule": plan["same_day_payment_rule"],
        "scenario_plan": plan["scenario"],
        "calendar": calendar.to_summary(),
        "contracts": [
            {
                "contract_id": c["identifier"],
                "start_date": c["start_date"].isoformat(),
                "maturity_date": c["maturity_date"].isoformat(),
                "coupon_months": c["coupon_months"],
                "coupon_rate": c["coupon_rate"],
                "principal": c["principal"],
                "end_of_month": c["end_of_month"],
            }
            for c in contracts
        ],
        "deposits": [
            {
                "quote_id": d["identifier"],
                "start_date": d["start_date"].isoformat(),
                "maturity_date": d["maturity_date"].isoformat(),
                "simple_rate": d["simple_rate"],
            }
            for d in deposits
        ],
        "bonds": [
            {
                "quote_id": b["identifier"],
                "start_date": b["start_date"].isoformat(),
                "maturity_date": b["maturity_date"].isoformat(),
                "coupon_months": b["coupon_months"],
                "coupon_rate": b["coupon_rate"],
                "principal": b["principal"],
                "end_of_month": b["end_of_month"],
                "clean_price": b["clean_price"],
            }
            for b in bonds
        ],
    }
