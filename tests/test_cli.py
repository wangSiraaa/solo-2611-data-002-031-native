"""端到端 CLI、情景重估与诊断退出码测试。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from fixedincome.cli import main
from fixedincome.curve import bootstrap_curve
from fixedincome.io import load_calendar, load_contracts, load_market, load_plan
from fixedincome.reports import run_scenario
from fixedincome.valuation import price_contract

FIXTURES = "fixtures"


def _load():
    calendar = load_calendar(f"{FIXTURES}/calendar.json")
    contracts = load_contracts(f"{FIXTURES}/contracts.json")
    deposits, bonds = load_market(f"{FIXTURES}/market.json")
    plan = load_plan(f"{FIXTURES}/plan.json")
    return calendar, contracts, deposits, bonds, plan


def test_price_command_end_to_end(tmp_path, capsys):
    rc = main(["--output-dir", str(tmp_path), "price"])
    assert rc == 0
    report = json.loads((tmp_path / "price_report.json").read_text(encoding="utf-8"))
    assert report["command"] == "price"
    # 输入摘要随报告保存
    assert report["input_summary"]["currency"] == "SIM"
    assert report["input_summary"]["day_count"] == "ACT/365"
    # 两个目标合约均完成估值，恒等核对通过
    valuations = {v["contract_id"]: v for v in report["valuations"]}
    assert set(valuations) == {"TARGET-A", "TARGET-B"}
    for v in valuations.values():
        assert v["identity_checks"]["clean_plus_accrued_equals_dirty"] is True
        assert v["identity_checks"]["per_cashflow_sum_equals_dirty"] is True
    # TARGET-A 估值日落在第一期中间，应有应计
    assert valuations["TARGET-A"]["accrued_interest"] == pytest.approx(
        100 * 0.032 * 14 / 365
    )
    # TARGET-B 估值日即起息日，应计为 0
    assert valuations["TARGET-B"]["accrued_interest"] == 0.0
    out = capsys.readouterr().out
    assert "dirty=" in out


def test_inspect_command_end_to_end(tmp_path):
    rc = main(["--output-dir", str(tmp_path), "inspect"])
    assert rc == 0
    report = json.loads((tmp_path / "inspect_report.json").read_text(encoding="utf-8"))
    # 四个支点、四个报价残差均在容忍内
    assert len(report["curve"]["pillars"]) == 4
    assert report["fitting_check"]["max_abs_residual"] < 1e-8
    for b in report["bond_repricing"]:
        assert b["within_tolerance"] is True
    # 支持范围显式声明
    assert report["supported_maturity_range"]["last_pillar"] == "2027-01-29"


def test_scenario_command_recalibrates_and_reconciles(tmp_path):
    rc = main(["--output-dir", str(tmp_path), "scenario"])
    assert rc == 0
    report = json.loads((tmp_path / "scenario_report.json").read_text(encoding="utf-8"))
    scn = report["scenario"]
    # 扰动确实施加在报价上，且两套曲线都是完整校准结果
    assert scn["applied_scenario"]["quote_id"] == "BOND-2Y"
    assert scn["applied_scenario"]["shocked_quote"] == pytest.approx(100.15)
    assert len(scn["base_calibration"]["curve"]["pillars"]) == 4
    assert len(scn["shocked_calibration"]["curve"]["pillars"]) == 4
    # 扰动后曲线对全部报价的重报残差仍在容忍内（新报价被精确拟合）
    assert scn["shocked_calibration"]["fitting_check"]["max_abs_residual"] < 1e-8
    for item in scn["target_contracts"]:
        rec = item["reconciliation"]
        assert rec["passes"] is True
        # 逐现金流价差之和必须等于全价差
        assert rec["difference"] == pytest.approx(0.0, abs=1e-9)


def test_scenario_full_recalibration_changes_curve_not_single_factor():
    calendar, contracts, deposits, bonds, plan = _load()
    base = bootstrap_curve(plan["valuation_date"], deposits, bonds, calendar)
    shocked, payload = run_scenario(
        plan, contracts, deposits, bonds, calendar, base, None
    )
    base_rates = dict(zip(base.curve.pillar_dates, base.curve.pillar_zero_rates))
    shock_rates = dict(zip(shocked.curve.pillar_dates, shocked.curve.pillar_zero_rates))
    # 完整重新拟合的判据：
    # (a) 受扰动的 2Y 支点利率确实发生变化，且方向为利率下行（净价上行）；
    last_pillar = base.curve.supported_to
    assert shock_rates[last_pillar] < base_rates[last_pillar]
    # (b) 新曲线是对“扰动后完整报价集”重新求解的结果：全部报价（含被扰动债券
    #     的新净价 100.15）都被重新拟合，残差在容忍内——不是只替换一个 DF；
    assert max(abs(r.residual) for r in shocked.residuals) < 1e-10
    shocked_bond = next(r for r in shocked.residuals if r.quote_id == "BOND-2Y")
    assert shocked_bond.target_quote == pytest.approx(100.15)
    assert abs(shocked_bond.model_quote - 100.15) < 1e-10
    # (c) 存款等未被扰动的工具解析支点保持不变（它们没有自由度可“掰”）。
    dep_pillars = [d["maturity_date"] for d in deposits]
    for day in dep_pillars:
        assert shock_rates[day] == pytest.approx(base_rates[day], abs=1e-14)
    # 净价上行 -> 利率下行 -> 目标合约价格上行
    target_a = next(
        c for c in payload["target_contracts"] if c["contract_id"] == "TARGET-A"
    )
    assert target_a["price_changes"]["dirty_price_change"] > 0


def test_scenario_unknown_quote_rejected(tmp_path):
    # 改写 plan 指向不存在的报价
    plan_path = tmp_path / "plan.json"
    original = json.loads(Path(f"{FIXTURES}/plan.json").read_text(encoding="utf-8"))
    original["scenario"]["quote_id"] = "GHOST"
    plan_path.write_text(json.dumps(original), encoding="utf-8")
    # 其余 fixtures 仍从默认目录读取：构造独立 plan 目录
    alt = tmp_path / "fixtures"
    alt.mkdir()
    for name in ("contracts.json", "market.json", "calendar.json", "plan.json"):
        if name == "plan.json":
            (alt / name).write_text(json.dumps(original), encoding="utf-8")
        else:
            (alt / name).write_text(
                Path(f"{FIXTURES}/{name}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
    rc = main(["--fixtures-dir", str(alt), "--output-dir", str(tmp_path / "out"),
               "scenario"])
    assert rc == 2
    diag = json.loads((tmp_path / "out" / "diagnostic.json").read_text(encoding="utf-8"))
    assert diag["diagnostic_code"] == "calibration_error"
    assert "GHOST" in diag["message"]


def test_stub_contract_cli_returns_diagnostic(tmp_path):
    alt = tmp_path / "fixtures"
    alt.mkdir()
    contracts = json.loads(Path(f"{FIXTURES}/contracts.json").read_text(encoding="utf-8"))
    contracts.append(
        {
            "contract_id": "STUB",
            "start_date": "2025-01-31",
            "maturity_date": "2026-09-30",
            "coupon_months": 6,
            "coupon_rate": "0.03",
            "principal": "100",
            "end_of_month": True,
        }
    )
    for name in ("market.json", "calendar.json", "plan.json"):
        (alt / name).write_text(
            Path(f"{FIXTURES}/{name}").read_text(encoding="utf-8"), encoding="utf-8"
        )
    (alt / "contracts.json").write_text(json.dumps(contracts), encoding="utf-8")
    rc = main(["--fixtures-dir", str(alt), "--output-dir", str(tmp_path / "out"),
               "price", "--contract", "STUB"])
    assert rc == 2
    diag = json.loads((tmp_path / "out" / "diagnostic.json").read_text(encoding="utf-8"))
    assert diag["diagnostic_code"] == "schedule_error"


def test_unknown_contract_id_cli(tmp_path):
    rc = main(["--output-dir", str(tmp_path), "price", "--contract", "NOPE"])
    assert rc == 2


def test_fixture_targets_within_supported_range():
    calendar, contracts, deposits, bonds, plan = _load()
    result = bootstrap_curve(plan["valuation_date"], deposits, bonds, calendar)
    for contract in contracts:
        valuation = price_contract(
            contract, plan["valuation_date"], calendar, result.curve
        )
        assert valuation.dirty_price > 0
        # 每笔计入估值的现金流付款日都不晚于曲线末端支点
        for cf in valuation.priced_flows:
            assert date.fromisoformat(cf["payment_date"]) <= result.curve.supported_to
