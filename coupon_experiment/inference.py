"""分层 ITT 估计、随机化检验与效应区间。

设计依据
--------
1. 估计量。每层是完全随机化（n_s 中抽 k_s 个处理），层内处理/对照均值
   之差是该层平均处理效应的无偏估计。总体目标量是全部入组家庭的平均
   效应，按 plan 预定家庭数 n_s/N 加权（与实际抽中多少处理无关），因此
   两层处理比例不同也不能直接合并两组订单求差。
2. 随机化检验。尖锐零假设（plan.inference.null：对任何家庭都无效应）
   下，家庭结果在原分配设计的合法分配空间中服从置换分布。检验只在
   DesignSpace 的合法分配上重抽，固定每层名额、层间独立。
3. 效应区间。采用常数加性效应反演：假设处理对每个家庭的效应都是同一
   常数 tau（尖锐、个体无异质性），从处理家庭观测结果中减去 tau 得到
   零假设下的潜在对照结果，再对每个候选 tau 做双侧随机化检验；p>=0.05
   的 tau 集合即为 95% 区间。搜索范围/分辨率来自 plan；网格扫描后对
   相邻边界做整数分细化。接受集触及搜索边界时按开放端点报告，不冒充
   有限置信界。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .design import Allocation, DesignSpace
from .models import Plan

EXACT_ENUM_LIMIT = 200_000
TWO_SIDED_ALPHA = 0.05


@dataclass
class StratumEstimate:
    stratum_id: str
    n_households: int
    treatment_slots: int
    control_slots: int
    treated_mean_cents: float
    control_mean_cents: float
    effect_cents: float

    def to_dict(self) -> dict:
        return {
            "stratum_id": self.stratum_id,
            "n_households": self.n_households,
            "treatment_slots": self.treatment_slots,
            "control_slots": self.control_slots,
            "treated_mean_cents": self.treated_mean_cents,
            "control_mean_cents": self.control_mean_cents,
            "effect_cents": self.effect_cents,
        }


@dataclass
class TestResult:
    scope: str
    mode: str  # exact | monte_carlo
    n_legal_allocations: int
    n_draws: int
    observed_statistic_cents: float
    p_value_two_sided: float

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "mode": self.mode,
            "n_legal_allocations": self.n_legal_allocations,
            "n_draws": self.n_draws,
            "observed_statistic_cents": self.observed_statistic_cents,
            "p_value_two_sided": self.p_value_two_sided,
            "null_hypothesis": "sharp null: no effect on any household",
        }


@dataclass
class IntervalResult:
    method: str
    assumption: str
    alpha: float
    search_bounds_cents: tuple[int, int]
    grid_step_cents: int
    grid_accepted: list[int]
    grid_interval_cents: tuple[int | None, int | None]
    integer_interval_cents: tuple[int | None, int | None]
    lower_open: bool
    upper_open: bool
    accepted_set_contiguous: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "assumption": self.assumption,
            "alpha": self.alpha,
            "search_bounds_cents": list(self.search_bounds_cents),
            "grid_step_cents": self.grid_step_cents,
            "grid_accepted_cents": list(self.grid_accepted),
            "grid_95pct_interval_cents": list(self.grid_interval_cents),
            "integer_refined_95pct_interval_cents": list(self.integer_interval_cents),
            "lower_endpoint_open": self.lower_open,
            "upper_endpoint_open": self.upper_open,
            "accepted_set_contiguous": self.accepted_set_contiguous,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 估计量
# --------------------------------------------------------------------------

def _household_outcomes(metrics: dict, eligible: set[str]) -> dict[str, int]:
    return {h: m.metric_cents for h, m in metrics.items() if h in eligible}


def stratum_estimates(plan: Plan, metrics: dict) -> list[StratumEstimate]:
    out: list[StratumEstimate] = []
    for s in plan.strata:
        treated_vals = [
            metrics[h].metric_cents
            for h in s.households
            if metrics[h].arm == plan.treatment_arm
        ]
        control_vals = [
            metrics[h].metric_cents
            for h in s.households
            if metrics[h].arm == plan.control_arm
        ]
        t_mean = sum(treated_vals) / len(treated_vals)
        c_mean = sum(control_vals) / len(control_vals)
        out.append(
            StratumEstimate(
                stratum_id=s.id,
                n_households=len(s.households),
                treatment_slots=len(treated_vals),
                control_slots=len(control_vals),
                treated_mean_cents=t_mean,
                control_mean_cents=c_mean,
                effect_cents=t_mean - c_mean,
            )
        )
    return out


def overall_effect(plan: Plan, ests: list[StratumEstimate]) -> float:
    """按预定家庭数 n_s/N 加权的层效应平均（目标 estimand）。"""
    total_n = sum(s.n_households for s in ests)
    return sum(e.effect_cents * e.n_households for e in ests) / total_n


# --------------------------------------------------------------------------
# 随机化检验
# --------------------------------------------------------------------------

def _statistic_for_allocation(
    alloc: Allocation,
    space: DesignSpace,
    outcomes: dict[str, int],
    plan: Plan,
) -> float:
    """与 point estimator 同一统计量：分层均值差按 n_s/N 加权。"""
    total = 0.0
    total_n = 0
    for sp in space.strata:
        t_vals = [outcomes[h] for h in sp.households if alloc.is_treated(h)]
        c_vals = [outcomes[h] for h in sp.households if not alloc.is_treated(h)]
        total += (sum(t_vals) / len(t_vals) - sum(c_vals) / len(c_vals)) * len(sp.households)
        total_n += len(sp.households)
    return total / total_n


def _statistic_for_stratum(
    alloc: Allocation,
    stratum_id: str,
    space: DesignSpace,
    outcomes: dict[str, int],
) -> float:
    sp = next(s for s in space.strata if s.id == stratum_id)
    t_vals = [outcomes[h] for h in sp.households if alloc.is_treated(h)]
    c_vals = [outcomes[h] for h in sp.households if not alloc.is_treated(h)]
    return sum(t_vals) / len(t_vals) - sum(c_vals) / len(c_vals)


def _two_sided_p(observed: float, null_stats: list[float]) -> float:
    """双侧 p：零分布中 |统计量| 不小于观测 |统计量| 的比例（含观测，避免 p=0）。"""
    obs_abs = abs(observed)
    ge = sum(1 for v in null_stats if abs(v) >= obs_abs - 1e-12)
    return ge / len(null_stats)


def _null_distribution(
    space: DesignSpace,
    plan: Plan,
    outcomes: dict[str, int],
    seed: int,
    label: str,
    stat_fn,
) -> tuple[list[float], str, int, int]:
    n_exact = space.n_allocations_exact
    if n_exact <= EXACT_ENUM_LIMIT:
        allocs = space.enumerate_allocations()
        mode = "exact"
        n_draws = len(allocs)
    else:
        # 蒙特卡洛：每层独立按固定名额采样，B 为默认 10000（可由 seed 复现）
        n_draws = 10_000
        allocs = space.sample_allocations(n_draws, seed=seed, label=label)
        mode = "monte_carlo"
    return [stat_fn(a) for a in allocs], mode, n_exact, n_draws


def randomization_tests(
    plan: Plan,
    space: DesignSpace,
    metrics: dict,
    observed: Allocation,
) -> list[TestResult]:
    outcomes = _household_outcomes(metrics, set(space.household_stratum))
    results: list[TestResult] = []

    # 总体检验（分层加权统计量）
    null_stats, mode, n_exact, n_draws = _null_distribution(
        space, plan, outcomes, plan.seed, "overall",
        lambda a: _statistic_for_allocation(a, space, outcomes, plan),
    )
    obs_stat = _statistic_for_allocation(observed, space, outcomes, plan)
    results.append(
        TestResult(
            scope="overall",
            mode=mode,
            n_legal_allocations=n_exact,
            n_draws=n_draws,
            observed_statistic_cents=obs_stat,
            p_value_two_sided=_two_sided_p(obs_stat, null_stats),
        )
    )

    # 分层检验：在同一份合法分配空间上取该层统计量（名额仍由整体设计约束）
    for sp in space.strata:
        s_null, s_mode, s_n_exact, s_draws = _null_distribution(
            space, plan, outcomes, plan.seed, f"stratum-{sp.id}",
            lambda a, sid=sp.id: _statistic_for_stratum(a, sid, space, outcomes),
        )
        s_obs = _statistic_for_stratum(observed, sp.id, space, outcomes)
        results.append(
            TestResult(
                scope=sp.id,
                mode=s_mode,
                n_legal_allocations=s_n_exact,
                n_draws=s_draws,
                observed_statistic_cents=s_obs,
                p_value_two_sided=_two_sided_p(s_obs, s_null),
            )
        )
    return results


# --------------------------------------------------------------------------
# 常数加性效应反演区间
# --------------------------------------------------------------------------

def _shifted_outcomes(
    alloc: Allocation, outcomes: dict[str, int], tau: int
) -> dict[str, int]:
    """加性模型下零假设潜在结果：处理户 Y(0)=Y(1)-tau，对照户不变。"""
    return {
        h: (y - tau if alloc.is_treated(h) else y)
        for h, y in outcomes.items()
    }


def additive_effect_interval(
    plan: Plan,
    space: DesignSpace,
    metrics: dict,
    observed: Allocation,
    mc_draws: int = 10_000,
) -> IntervalResult:
    outcomes = _household_outcomes(metrics, set(space.household_stratum))
    lo, hi = plan.search_bounds
    step = plan.search_step
    if step <= 0 or lo >= hi:
        raise ValueError("search_bounds/search_step 配置无效")

    exact = space.n_allocations_exact <= EXACT_ENUM_LIMIT
    if exact:
        allocs = space.enumerate_allocations()
        mode_note = f"精确枚举 {len(allocs)} 个合法分配"
    else:
        allocs = space.sample_allocations(mc_draws, seed=plan.seed, label="ci-inversion")
        mode_note = f"固定种子蒙特卡洛 {len(allocs)} 个合法分配"

    def p_at_tau(tau: int) -> float:
        shifted = _shifted_outcomes(observed, outcomes, tau)
        null_stats = [
            _statistic_for_allocation(a, space, shifted, plan) for a in allocs
        ]
        obs_stat = _statistic_for_allocation(observed, space, shifted, plan)
        return _two_sided_p(obs_stat, null_stats)

    # 1) 网格扫描
    grid = list(range(lo, hi + 1, step))
    accepted_grid = [tau for tau in grid if p_at_tau(tau) >= TWO_SIDED_ALPHA]

    if not accepted_grid:
        return IntervalResult(
            method="constant additive-effect randomization inversion (two-sided)",
            assumption="sharp constant additive treatment effect tau (cents), identical for every household",
            alpha=TWO_SIDED_ALPHA,
            search_bounds_cents=(lo, hi),
            grid_step_cents=step,
            grid_accepted=[],
            grid_interval_cents=(None, None),
            integer_interval_cents=(None, None),
            lower_open=True,
            upper_open=True,
            accepted_set_contiguous=True,
            note=(
                "在给定搜索范围与分辨率下没有任何 tau 通过 5% 双侧随机化检验；"
                "不能解释为区间不存在，应扩大搜索范围后重算。"
            ),
        )

    grid_lo, grid_hi = min(accepted_grid), max(accepted_grid)
    contiguous = accepted_grid == list(range(grid_lo, grid_hi + 1, step))

    # 2) 整数分细化：从网格接受集两端向外逐整数检验，直到被拒绝或触及
    #    搜索边界。常数加性模型下接受集是闭区间，故扩展到首个被拒点即可，
    #    得到整数分尺度上精确的端点，而不是只向两侧扩一个网格步。
    int_lo, int_hi = grid_lo, grid_hi
    tau = grid_lo - 1
    while tau >= lo and p_at_tau(tau) >= TWO_SIDED_ALPHA:
        int_lo = tau
        tau -= 1
    tau = grid_hi + 1
    while tau <= hi and p_at_tau(tau) >= TWO_SIDED_ALPHA:
        int_hi = tau
        tau += 1

    lower_open = int_lo <= lo and p_at_tau(lo) >= TWO_SIDED_ALPHA
    upper_open = int_hi >= hi and p_at_tau(hi) >= TWO_SIDED_ALPHA
    if lower_open:
        int_lo = None
    if upper_open:
        int_hi = None

    note = mode_note
    if lower_open or upper_open:
        note += (
            "；接受集触及搜索边界，对应端点为开放端点（未封闭），"
            "不是有限置信界，需扩大 search_bounds 重算"
        )
    if not contiguous:
        note += "；网格接受集不连续，已如实保留，区间解释需谨慎"

    return IntervalResult(
        method="constant additive-effect randomization inversion (two-sided)",
        assumption="sharp constant additive treatment effect tau (cents), identical for every household",
        alpha=TWO_SIDED_ALPHA,
        search_bounds_cents=(lo, hi),
        grid_step_cents=step,
        grid_accepted=accepted_grid,
        grid_interval_cents=(grid_lo, grid_hi),
        integer_interval_cents=(int_lo, int_hi),
        lower_open=lower_open,
        upper_open=upper_open,
        accepted_set_contiguous=contiguous,
        note=note,
    )
