"""端到端分析流水线：加载 -> 资格检查 -> 家庭汇总 -> （合格时）随机化推断。

不合格输入：停止主要效应结论，不删除任何家庭，只输出明确标注的描述统计。
合格输入：输出分层及总体估计、遵守固定名额的随机化检验、95% 反演区间。
"""

from __future__ import annotations

from pathlib import Path

from .design import DesignSpace
from .inference import (
    additive_effect_interval,
    overall_effect,
    randomization_tests,
    stratum_estimates,
)
from .models import Plan
from .validation import (
    is_eligible,
    load_assignments,
    load_coverage,
    load_events,
    load_memberships,
    load_plan,
    validate_and_aggregate,
)


def _household_rollup(metrics: dict) -> dict:
    """未曝光、零消费、窗口外、归属不明、覆盖缺失等情况的计数与名单。"""
    unexposed, zero_spend, out_of_window = [], [], []
    no_metric, missing_coverage, ambiguous = [], [], []
    for h, m in sorted(metrics.items()):
        if m.exposed is False:
            unexposed.append(h)
        if m.zero_recorded:
            zero_spend.append(h)
        if m.out_of_window_event_ids:
            out_of_window.append(
                {
                    "household_id": h,
                    "event_ids": list(m.out_of_window_event_ids),
                    "amount_cents": m.out_of_window_amount_cents,
                }
            )
        if m.metric_cents is None:
            no_metric.append(h)
        if m.metric_status == "missing_coverage":
            missing_coverage.append(h)
        if m.metric_status == "ambiguous_attribution":
            ambiguous.append(h)
    return {
        "n_households": len(metrics),
        "unexposed_households": unexposed,
        "zero_spend_households": zero_spend,
        "households_with_out_of_window_events": out_of_window,
        "households_without_usable_metric": no_metric,
        "missing_coverage_households": missing_coverage,
        "ambiguous_attribution_households": ambiguous,
    }


def _descriptive(plan: Plan, metrics: dict) -> dict:
    """描述统计：任何资格状态下都给出，但绝不冒充实验效应结论。"""
    usable = {h: m for h, m in metrics.items() if m.metric_cents is not None}
    by_arm: dict[str, dict] = {}
    for arm in (plan.treatment_arm, plan.control_arm):
        vals = [m.metric_cents for m in usable.values() if m.arm == arm]
        by_arm[arm] = {
            "n_households_with_metric": len(vals),
            "mean_cents": (sum(vals) / len(vals)) if vals else None,
            "sum_cents": sum(vals) if vals else None,
        }
    out: dict = {
        "label": "DESCRIPTIVE ONLY — 非随机化推断结论；指标缺失家庭已剔除并列出",
        "arm_summary": by_arm,
        "naive_pooled_mean_difference_cents": None,
        "naive_pooled_warning": (
            "直接合并两组家庭求均值差，忽略两层处理比例不同，"
            "不是 plan 目标 estimand，禁止作为实验效应解读"
        ),
    }
    t = [m.metric_cents for m in usable.values() if m.arm == plan.treatment_arm]
    c = [m.metric_cents for m in usable.values() if m.arm == plan.control_arm]
    if t and c:
        out["naive_pooled_mean_difference_cents"] = sum(t) / len(t) - sum(c) / len(c)
    return out


def run_analysis(
    plan_path: str | Path,
    assignments_path: str | Path,
    members_path: str | Path,
    events_path: str | Path,
    coverage_path: str | Path,
) -> dict:
    plan = load_plan(plan_path)
    assignments = load_assignments(assignments_path)
    memberships = load_memberships(members_path)
    events = load_events(events_path)
    coverage = load_coverage(coverage_path)

    diags, metrics = validate_and_aggregate(
        plan, assignments, memberships, events, coverage
    )
    eligible = is_eligible(diags)
    error_codes = sorted({d.code for d in diags if d.severity == "error"})

    result: dict = {
        "experiment_id": plan.experiment_id,
        "eligible_for_causal_inference": eligible,
        "blocking_error_codes": error_codes,
        "design": {
            "unit": plan.unit,
            "treatment_arm": plan.treatment_arm,
            "control_arm": plan.control_arm,
            "window": [plan.window_start, plan.window_end],
            "window_convention": "left-closed right-open [start, end)",
            "metric": plan.metric,
            "currency": plan.currency,
            "estimand": plan.estimand,
            "strata": [
                {
                    "id": s.id,
                    "eligible_households": list(s.households),
                    "n_households": len(s.households),
                    "treatment_slots": s.treatment_slots,
                    "control_slots": len(s.households) - s.treatment_slots,
                }
                for s in plan.strata
            ],
            "seed": plan.seed,
        },
        "diagnostics": [d.to_dict() for d in diags],
        "data_quality_rollup": _household_rollup(metrics),
        "household_metrics": [metrics[h].to_dict() for h in sorted(metrics)],
        "descriptive_statistics": _descriptive(plan, metrics),
        "inference": None,
    }

    if not eligible:
        result["inference_blocked_reason"] = (
            "存在 error 级诊断（见 blocking_error_codes），原随机化设计或结果测量"
            "完整性已被破坏；不删除受影响家庭继续声称主要推断有效，仅保留描述统计。"
        )
        return result

    # ---- 合格输入：ITT 主推断（未领券家庭仍按分配 arm 保留） -------------
    space = DesignSpace(plan)
    arm_of = {h: metrics[h].arm for h in space.household_stratum}
    observed = space.observed_allocation(arm_of, plan.treatment_arm)
    assert space.is_legal(observed), "资格检查通过但观测分配不满足固定名额"

    ests = stratum_estimates(plan, metrics)
    point = overall_effect(plan, ests)
    tests = randomization_tests(plan, space, metrics, observed)
    interval = additive_effect_interval(plan, space, metrics, observed)

    result["inference"] = {
        "cohort": "ITT：全部入组家庭，按原分配 arm 分组；未领券(exposed=false)不剔除",
        "point_estimates": {
            "overall_effect_cents": point,
            "weighting": "stratum weight = planned n_s / N（与实际处理比例无关）",
            "by_stratum": [e.to_dict() for e in ests],
        },
        "randomization_tests": [t.to_dict() for t in tests],
        "effect_interval": interval.to_dict(),
        "design_basis": (
            "层内完全随机化、层间独立；检验仅在满足各层固定名额的合法分配空间上"
            "重抽（小设计精确枚举，否则固定种子按层采样）；不跨层重排家庭，"
            "不对订单做普通 bootstrap。"
        ),
    }
    return result
