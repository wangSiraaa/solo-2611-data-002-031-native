"""独立极小分层设计核对（不调用 coupon_experiment 的任何推断函数）。

设计：
  层 S1：家庭 a,b,c，固定处理名额 1（C(3,1)=3 种）
  层 S2：家庭 d,e，  固定处理名额 1（C(2,1)=2 种）
合法分配总数 = 3 * 2 = 6，逐一独立列出。

核对内容：
  1. 手工列举全部 6 个可行分配，核对每个都满足两层固定名额；
  2. 构造一个故意违规的分配（S1 抽 2 户），确认它不在可行集合中；
  3. 用自写的置换循环计算尖锐零假设下的双侧精确 p 值，
     并验证“检验只使用合法名额”——p 的分母恰为 6；
  4. 常数加性效应反演：对 tau 网格自写检验，确认真实加性效应落在接受集内。

运行：python3 tests/tiny_design_check.py
"""

from __future__ import annotations

from itertools import combinations

S1 = ("a", "b", "c")
S2 = ("d", "e")
K1, K2 = 1, 1
ALPHA = 0.05

# 合成观测结果（整数分）与观测分配：S1 处理 b，S2 处理 e
OUTCOMES = {"a": 100, "b": 300, "c": 200, "d": 150, "e": 350}
OBSERVED_TREATED = frozenset({"b", "e"})
TRUE_ADDITIVE_EFFECT = 120  # 构造数据时隐含的加性效应（仅用于第 4 步核对）


def feasible_allocations():
    """独立枚举：两层各抽固定名额，层间笛卡尔积。"""
    out = []
    for t1 in combinations(S1, K1):
        for t2 in combinations(S2, K2):
            out.append(frozenset(t1) | frozenset(t2))
    return out


def stat(treated, outcomes):
    """与主工具同形式的分层均值差，按 n_s/N 加权，独立实现。"""
    def means(units, k):
        t = [outcomes[h] for h in units if h in treated]
        c = [outcomes[h] for h in units if h not in treated]
        assert len(t) == k, "名额被违反"
        return sum(t) / len(t) - sum(c) / len(c)

    d1, d2 = means(S1, K1), means(S2, K2)
    return (d1 * len(S1) + d2 * len(S2)) / (len(S1) + len(S2))


def two_sided_p(observed, allocations, outcomes):
    null_stats = [stat(a, outcomes) for a in allocations]
    obs_abs = abs(observed)
    ge = sum(1 for v in null_stats if abs(v) >= obs_abs - 1e-12)
    return ge / len(null_stats), null_stats


def main() -> int:
    failures = []

    # 1) 可行分配清单与名额合法性
    allocs = feasible_allocations()
    expected = [
        frozenset({"a", "d"}), frozenset({"a", "e"}),
        frozenset({"b", "d"}), frozenset({"b", "e"}),
        frozenset({"c", "d"}), frozenset({"c", "e"}),
    ]
    print("可行分配（共 %d 个）：" % len(allocs))
    for a in allocs:
        n1 = sum(1 for h in S1 if h in a)
        n2 = sum(1 for h in S2 if h in a)
        line = f"  S1处理={sorted(h for h in a if h in S1)} " \
               f"S2处理={sorted(h for h in a if h in S2)} 名额=({n1},{n2})"
        print(line)
        if (n1, n2) != (K1, K2):
            failures.append(f"名额违反: {a}")
    if set(allocs) != set(expected):
        failures.append("枚举集合与手工清单不一致")
    if len(allocs) != 6:
        failures.append(f"可行分配数应为 6，实际 {len(allocs)}")

    # 2) 违规分配不在可行集合内
    illegal = frozenset({"a", "b", "e"})  # S1 抽了 2 户
    if illegal in allocs:
        failures.append("违规分配错误地进入可行集合")
    print(f"\n违规分配 {set(illegal)} 被可行集合拒绝: {illegal not in allocs}")

    # 3) 精确随机化检验，分母必须等于合法分配数
    obs_stat = stat(OBSERVED_TREATED, OUTCOMES)
    p, null_stats = two_sided_p(obs_stat, allocs, OUTCOMES)
    print(f"\n观测统计量={obs_stat:.2f} 分")
    print("零分布（每个值对应一个合法分配）：",
          [round(v, 2) for v in null_stats])
    print(f"双侧精确 p={p:.4f}（分母={len(null_stats)}=合法分配数，"
          f"无任何跨层重排或订单 bootstrap）")
    if len(null_stats) != len(allocs):
        failures.append("检验使用了非法分配")
    if not (1 / len(allocs) <= p <= 1.0):
        failures.append("p 值超出可行范围")

    # 4) 加性效应反演：把 TRUE_ADDITIVE_EFFECT 从处理户扣除后，零假设应不被拒绝
    shifted = {
        h: (v - TRUE_ADDITIVE_EFFECT if h in OBSERVED_TREATED else v)
        for h, v in OUTCOMES.items()
    }
    p_at_true, _ = two_sided_p(
        stat(OBSERVED_TREATED, shifted), allocs, shifted
    )
    grid_accepted = []
    for tau in range(-500, 501, 20):
        sh = {h: (v - tau if h in OBSERVED_TREATED else v)
              for h, v in OUTCOMES.items()}
        pt, _ = two_sided_p(stat(OBSERVED_TREATED, sh), allocs, sh)
        if pt >= ALPHA:
            grid_accepted.append(tau)
    print(f"\n反演：真实加性效应 {TRUE_ADDITIVE_EFFECT} 处 p={p_at_true:.4f} "
          f"(>=0.05: {p_at_true >= ALPHA})")
    print(f"网格接受集 [{min(grid_accepted)}, {max(grid_accepted)}]，"
          f"覆盖真实效应: {min(grid_accepted) <= TRUE_ADDITIVE_EFFECT <= max(grid_accepted)}")
    if p_at_true < ALPHA:
        failures.append("真实加性效应被反演检验拒绝")
    if not (min(grid_accepted) <= TRUE_ADDITIVE_EFFECT <= max(grid_accepted)):
        failures.append("接受集未覆盖真实加性效应")

    print("\n" + ("全部核对通过 ✔" if not failures else "存在失败: " + "; ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
