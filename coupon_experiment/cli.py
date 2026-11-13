"""命令行入口：

    python -m coupon_experiment.cli \
        --plan fixtures/plan.json \
        --assignments fixtures/assignments.csv \
        --members fixtures/members.csv \
        --events fixtures/events.csv \
        --coverage fixtures/coverage.csv \
        --out results/base.json

一次只处理一个实验；金额按整数分。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis import run_analysis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="分层家庭随机优惠券实验的资格检查与随机化推断"
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--assignments", required=True)
    parser.add_argument("--members", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--coverage", required=True)
    parser.add_argument("--out", required=True, help="结构化结果 JSON 输出路径")
    args = parser.parse_args(argv)

    result = run_analysis(
        args.plan, args.assignments, args.members, args.events, args.coverage
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    eligible = result["eligible_for_causal_inference"]
    print(f"实验: {result['experiment_id']}")
    print(f"资格: {'合格，主推断有效' if eligible else '不合格，主推断被阻止（仅描述统计）'}")
    if result["blocking_error_codes"]:
        print("阻断性错误: " + ", ".join(result["blocking_error_codes"]))
    for d in result["diagnostics"]:
        print(f"  [{d['severity']}] {d['code']}: {d['message']} 单位={d['units']}")
    if eligible:
        inf = result["inference"]
        print(f"总体效应估计(分): {inf['point_estimates']['overall_effect_cents']}")
        for t in inf["randomization_tests"]:
            print(
                f"  随机化检验 {t['scope']} ({t['mode']}, "
                f"{t['n_draws']} 次): p={t['p_value_two_sided']:.4f}"
            )
        ci = inf["effect_interval"]
        print(
            "95% 反演区间(整数分细化): "
            f"{'(' if ci['lower_endpoint_open'] else '['}"
            f"{ci['integer_refined_95pct_interval_cents'][0]}, "
            f"{ci['integer_refined_95pct_interval_cents'][1]}"
            f"{')' if ci['upper_endpoint_open'] else ']'}"
        )
    print(f"结构化结果已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
