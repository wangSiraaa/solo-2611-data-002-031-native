"""命令行入口：price / scenario / inspect。

用法：
    python -m fixedincome price     [--contract ID ...]
    python -m fixedincome scenario  [--contract ID ...]
    python -m fixedincome inspect
报告默认写入 reports/，可用 --output-dir 覆盖。
业务错误以结构化诊断 JSON 与退出码 2 返回，不打印堆栈、不返回猜测价格。
"""

from __future__ import annotations

import argparse
import json
import sys

from .curve import bootstrap_curve
from .errors import FixedIncomeError
from .io import (
    input_summary,
    load_calendar,
    load_contracts,
    load_market,
    load_plan,
)
from .reports import method_summary, run_scenario, save_report
from .valuation import price_contract, reprice_bond

DEFAULT_FIXTURES = "fixtures"
DEFAULT_OUTPUT = "reports"


def _load_all(fixtures_dir: str):
    calendar = load_calendar(f"{fixtures_dir}/calendar.json")
    contracts = load_contracts(f"{fixtures_dir}/contracts.json")
    deposits, bonds = load_market(f"{fixtures_dir}/market.json")
    plan = load_plan(f"{fixtures_dir}/plan.json")
    return calendar, contracts, deposits, bonds, plan


def _select(contracts: list[dict], ids: list[str] | None) -> list[dict]:
    if not ids:
        return contracts
    known = {c["identifier"] for c in contracts}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise FixedIncomeError(
            f"请求的合约不存在: {unknown}", unknown_contracts=unknown
        )
    return [c for c in contracts if c["identifier"] in ids]


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def cmd_price(args) -> dict:
    calendar, contracts, deposits, bonds, plan = _load_all(args.fixtures_dir)
    result = bootstrap_curve(plan["valuation_date"], deposits, bonds, calendar)
    selected = _select(contracts, args.contract)
    valuations = [
        price_contract(c, plan["valuation_date"], calendar, result.curve)
        for c in selected
    ]
    report = {
        "command": "price",
        "input_summary": input_summary(plan, contracts, deposits, bonds, calendar),
        "method": method_summary(),
        "curve": result.curve.to_summary(),
        "fitting_check": result.to_summary(),
        "valuations": [v.to_dict() for v in valuations],
    }
    path = save_report(args.output_dir, "price_report.json", report)

    _print_header("曲线拟合检查（报价残差）")
    for r in result.residuals:
        print(f"  {r.quote_id:10s} target={r.target_quote:.8f} "
              f"model={r.model_quote:.8f} residual={r.residual:+.2e}")
    _print_header("合约估值（每 100 本金）")
    for v in valuations:
        print(f"  {v.contract_id}: dirty={v.dirty_price:.10f} "
              f"clean={v.clean_price:.10f} accrued={v.accrued_interest:.10f} "
              f"未来现金流 {len(v.priced_flows)} 笔")
    print(f"\n报告已保存: {path}")
    return report


def cmd_scenario(args) -> dict:
    calendar, contracts, deposits, bonds, plan = _load_all(args.fixtures_dir)
    result = bootstrap_curve(plan["valuation_date"], deposits, bonds, calendar)
    shocked_result, scenario_payload = run_scenario(
        plan, contracts, deposits, bonds, calendar, result, args.contract
    )
    report = {
        "command": "scenario",
        "input_summary": input_summary(plan, contracts, deposits, bonds, calendar),
        "method": method_summary(),
        "scenario": scenario_payload,
    }
    path = save_report(args.output_dir, "scenario_report.json", report)

    s = scenario_payload["applied_scenario"]
    _print_header(
        f"情景：{s['quote_id']} {s['field']} {s['absolute_shift']:+.4f} "
        f"({s['base_quote']:.4f} -> {s['shocked_quote']:.4f})，已重新校准"
    )
    for item in scenario_payload["target_contracts"]:
        chg = item["price_changes"]
        rec = item["reconciliation"]
        print(f"  {item['contract_id']}: Δdirty={chg['dirty_price_change']:+.10f} "
              f"Δclean={chg['clean_price_change']:+.10f}")
        print(f"    逐现金流价差合计={rec['sum_of_per_cashflow_changes']:+.10f} "
              f"与 Δdirty 差={rec['difference']:+.2e} 核对={'通过' if rec['passes'] else '失败'}")
    print(f"\n报告已保存: {path}")
    return report


def cmd_inspect(args) -> dict:
    calendar, contracts, deposits, bonds, plan = _load_all(args.fixtures_dir)
    result = bootstrap_curve(plan["valuation_date"], deposits, bonds, calendar)
    selected = _select(contracts, args.contract)
    bond_repricing = [
        reprice_bond(b, plan["valuation_date"], calendar, result.curve) for b in bonds
    ]
    schedule_views = []
    for contract in selected:
        valuation = price_contract(
            contract, plan["valuation_date"], calendar, result.curve
        )
        schedule_views.append(
            {
                "contract_id": contract["identifier"],
                "current_period_index": valuation.current_period_index,
                "accrued_interest": valuation.accrued_interest,
                "all_cashflows": [cf.to_dict() for cf in valuation.cashflows],
                "priced_cashflows": valuation.priced_flows,
            }
        )
    report = {
        "command": "inspect",
        "input_summary": input_summary(plan, contracts, deposits, bonds, calendar),
        "method": method_summary(),
        "supported_maturity_range": {
            "valuation_date": plan["valuation_date"].isoformat(),
            "first_pillar": result.curve.pillar_dates[0].isoformat(),
            "last_pillar": result.curve.supported_to.isoformat(),
            "long_end_policy": "beyond last pillar: reject (no extrapolation)",
        },
        "curve": result.curve.to_summary(),
        "fitting_check": result.to_summary(),
        "bond_repricing": bond_repricing,
        "contract_schedules": schedule_views,
    }
    path = save_report(args.output_dir, "inspect_report.json", report)

    _print_header("折现曲线支点")
    for pillar in result.curve.to_summary()["pillars"]:
        print(f"  {pillar['date']}  t={pillar['year_fraction']:.6f}  "
              f"zero={pillar['zero_rate']:.10f}  DF={pillar['discount_factor']:.10f}")
    _print_header("基准债券同规则重新报价")
    for b in bond_repricing:
        print(f"  {b['quote_id']:10s} target_clean={b['target_clean_price']:.6f} "
              f"model_clean={b['model_clean_price']:.10f} "
              f"residual={b['residual']:+.2e} 核对={'通过' if b['within_tolerance'] else '失败'}")
    _print_header("合约逐笔现金流（计息边界 vs 付款日）")
    for view in schedule_views:
        print(f"  {view['contract_id']}: 共 {len(view['all_cashflows'])} 笔，"
              f"计入估值 {len(view['priced_cashflows'])} 笔，"
              f"当前计息期 index={view['current_period_index']}")
        for cf in view["all_cashflows"]:
            print(
                f"    #{cf['period_index']} 计息 {cf['accrual_start']} -> "
                f"{cf['accrual_end']} | 付款 {cf['payment_date']} | "
                f"票息 {cf['coupon_amount']:.6f} 还本 {cf['principal_amount']:.2f}"
            )
    print(f"\n报告已保存: {path}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fixedincome",
        description="单币种固定本金固定票息合约本地估值工具",
    )
    parser.add_argument(
        "--fixtures-dir", default=DEFAULT_FIXTURES, help="fixtures 目录（默认 fixtures/）"
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT, help="报告输出目录（默认 reports/）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_price = sub.add_parser("price", help="校准曲线并输出目标合约全价/净价/应计")
    p_price.add_argument("--contract", action="append", help="指定 contract_id，可重复")
    p_price.set_defaults(func=cmd_price)

    p_scn = sub.add_parser("scenario", help="按 plan 扰动报价、重新校准并给出逐现金流价差")
    p_scn.add_argument("--contract", action="append", help="指定目标 contract_id，可重复")
    p_scn.set_defaults(func=cmd_scenario)

    p_ins = sub.add_parser("inspect", help="输出曲线支点、拟合残差与逐笔现金流明细")
    p_ins.add_argument("--contract", action="append", help="指定 contract_id，可重复")
    p_ins.set_defaults(func=cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FixedIncomeError as exc:
        diagnostic = exc.to_diagnostic()
        print("\n业务诊断（未给出价格）:", file=sys.stderr)
        print(json.dumps(diagnostic, ensure_ascii=False, indent=2, default=str),
              file=sys.stderr)
        try:
            save_report(args.output_dir, "diagnostic.json", diagnostic)
        except Exception:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
