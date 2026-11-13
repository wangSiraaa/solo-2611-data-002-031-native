"""测试公共工具：在临时目录生成最小合法 fixture，并支持单点替换制造异常。"""

from __future__ import annotations

import json
from pathlib import Path

PLAN = {
    "experiment_id": "t-exp",
    "unit": "household_id",
    "treatment": "B",
    "control": "A",
    "assignment_design": "complete randomization within strata",
    "strata": [
        {"id": "S1", "eligible_households": ["H01", "H02", "H03", "H04"],
         "treatment_slots": 2},
        {"id": "S2", "eligible_households": ["H05", "H06"],
         "treatment_slots": 1},
    ],
    "estimand": "ATE over eligible households, stratum weights n_s/N",
    "window_start": "2026-11-01T00:00:00Z",
    "window_end": "2026-11-08T00:00:00Z",
    "metric": "sum(amount_cents)",
    "currency": "CNY",
    "require_complete_coverage": True,
    "seed": 1701,
    "inference": {
        "null": "no effect on any household",
        "search_bounds_cents": [-2000, 2000],
        "search_step_cents": 50,
    },
}

ASSIGNMENTS = [
    ("H01", "S1", "A", "false"),
    ("H02", "S1", "A", "false"),
    ("H03", "S1", "B", "true"),
    ("H04", "S1", "B", "false"),
    ("H05", "S2", "A", "false"),
    ("H06", "S2", "B", "true"),
]

MEMBERS = [
    ("P01", "H01"), ("P02", "H02"), ("P03", "H03"),
    ("P04", "H04"), ("P05", "H05"), ("P06", "H06"),
    ("P07", "H01"),
]

EVENTS = [
    ("E01", "P01", "2026-11-02T00:00:00Z", 100),
    ("E02", "P07", "2026-11-02T00:00:00Z", 200),
    ("E03", "P02", "2026-11-03T00:00:00Z", 300),
    ("E04", "P03", "2026-11-03T00:00:00Z", 400),
    ("E05", "P04", "2026-11-03T00:00:00Z", 500),
    ("E06", "P05", "2026-11-03T00:00:00Z", 600),
    ("E07", "P06", "2026-11-03T00:00:00Z", 700),
    ("E08", "P01", "2026-11-08T00:00:00Z", 999),  # 恰在 window_end，窗口外
]

COVERAGE = [(h, "true") for h in ["H01", "H02", "H03", "H04", "H05", "H06"]]


# 较大设计：两层各 8 户、各抽 4 户处理 -> C(8,4)^2 = 4900 个合法分配，
# 最小双侧 p 可达 1/4900 < 0.05，反演区间可以封闭。
BIG_PLAN = {
    "experiment_id": "big-exp",
    "unit": "household_id",
    "treatment": "B",
    "control": "A",
    "assignment_design": "complete randomization within strata",
    "strata": [
        {"id": "S1", "eligible_households": [f"H{i:02d}" for i in range(1, 9)],
         "treatment_slots": 4},
        {"id": "S2", "eligible_households": [f"H{i:02d}" for i in range(9, 17)],
         "treatment_slots": 4},
    ],
    "estimand": "ATE over eligible households, stratum weights n_s/N",
    "window_start": "2026-11-01T00:00:00Z",
    "window_end": "2026-11-08T00:00:00Z",
    "metric": "sum(amount_cents)",
    "currency": "CNY",
    "require_complete_coverage": True,
    "seed": 1701,
    "inference": {
        "null": "no effect on any household",
        "search_bounds_cents": [-3000, 3000],
        "search_step_cents": 50,
    },
}

# 处理户结果 = 对照基线 + 350 常数加性效应；基线只取决于层内位置，
# 保证处理/对照基线均值相同，点估计恰好恢复 350。
_BASE = [100, 200, 300, 400, 500, 600, 700, 800]
# 每层取位置 {0,3,4,7} 为处理户：处理/对照基线均值都是 450，
# 点估计恰好恢复常数加性效应 350。
_BIG_TREATED = {"H01", "H04", "H05", "H08", "H09", "H12", "H13", "H16"}


def _big_rows():
    assignments, members, events, coverage = [], [], [], []
    all_hh = (BIG_PLAN["strata"][0]["eligible_households"]
              + BIG_PLAN["strata"][1]["eligible_households"])
    for idx, h in enumerate(all_hh):
        sid = "S1" if idx < 8 else "S2"
        within = idx % 8
        arm = "B" if h in _BIG_TREATED else "A"
        assignments.append((h, sid, arm, "true" if arm == "B" else "false"))
        p = f"P{idx + 1:02d}"
        members.append((p, h))
        amount = _BASE[within] + (350 if arm == "B" else 0)
        events.append((f"E{idx + 1:02d}", p, "2026-11-03T00:00:00Z", amount))
        coverage.append((h, "true"))
    return assignments, members, events, coverage


BIG_ASSIGNMENTS, BIG_MEMBERS, BIG_EVENTS, BIG_COVERAGE = _big_rows()


def write_fixture(
    tmp: Path,
    *,
    plan: dict | None = None,
    assignments=None,
    members=None,
    events=None,
    coverage=None,
) -> dict[str, Path]:
    plan = plan or PLAN
    assignments = ASSIGNMENTS if assignments is None else assignments
    members = MEMBERS if members is None else members
    events = EVENTS if events is None else events
    coverage = COVERAGE if coverage is None else coverage

    paths = {}
    paths["plan"] = tmp / "plan.json"
    paths["plan"].write_text(json.dumps(plan), encoding="utf-8")

    for key, header, rows in [
        ("assignments", "household_id,stratum,arm,exposed", assignments),
        ("members", "person_id,household_id", members),
        ("events", "event_id,person_id,occurred_at,amount_cents", events),
        ("coverage", "household_id,complete", coverage),
    ]:
        p = tmp / f"{key}.csv"
        with p.open("w", encoding="utf-8", newline="") as fh:
            fh.write(header + "\n")
            for r in rows:
                fh.write(",".join(str(x) for x in r) + "\n")
        paths[key] = p
    return paths
