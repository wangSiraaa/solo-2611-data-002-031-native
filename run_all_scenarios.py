"""复现脚本：基础数据 + 三个异常替换场景，输出到 results/。

    python3 run_all_scenarios.py

每个场景只替换对应的那一个 CSV，其余沿用基础 fixture（与 fixtures/README 一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

from coupon_experiment.analysis import run_analysis

ROOT = Path(__file__).resolve().parent
FX = ROOT / "fixtures"
OUT = ROOT / "results"

SCENARIOS = [
    (
        "base",
        {
            "assignments": FX / "assignments.csv",
            "members": FX / "members.csv",
            "events": FX / "events.csv",
            "coverage": FX / "coverage.csv",
        },
    ),
    (
        "case_cross_arm",
        {
            "assignments": FX / "cases" / "cross_arm_assignments.csv",
            "members": FX / "members.csv",
            "events": FX / "events.csv",
            "coverage": FX / "coverage.csv",
        },
    ),
    (
        "case_incomplete_coverage",
        {
            "assignments": FX / "assignments.csv",
            "members": FX / "members.csv",
            "events": FX / "events.csv",
            "coverage": FX / "cases" / "incomplete_coverage.csv",
        },
    ),
    (
        "case_ambiguous_members",
        {
            "assignments": FX / "assignments.csv",
            "members": FX / "cases" / "ambiguous_members.csv",
            "events": FX / "events.csv",
            "coverage": FX / "coverage.csv",
        },
    ),
]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    summary = []
    for name, files in SCENARIOS:
        result = run_analysis(
            FX / "plan.json",
            files["assignments"],
            files["members"],
            files["events"],
            files["coverage"],
        )
        out_path = OUT / f"{name}.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        entry = {
            "scenario": name,
            "eligible_for_causal_inference": result[
                "eligible_for_causal_inference"
            ],
            "blocking_error_codes": result["blocking_error_codes"],
            "warning_codes": sorted(
                {d["code"] for d in result["diagnostics"]
                 if d["severity"] == "warning"}
            ),
            "result_file": str(out_path.relative_to(ROOT)),
        }
        if result["inference"] is not None:
            entry["overall_effect_cents"] = result["inference"][
                "point_estimates"
            ]["overall_effect_cents"]
            entry["interval_integer_cents"] = result["inference"][
                "effect_interval"
            ]["integer_refined_95pct_interval_cents"]
        summary.append(entry)

    (OUT / "scenario_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    for e in summary:
        print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
