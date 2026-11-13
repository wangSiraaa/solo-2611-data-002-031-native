"""输入加载与资格检查，并把事件按固定成员关系汇总到家庭。

检查对应题面要求的七类问题（任一 error 都会使输入不合格）：
  1. 名单缺失（plan 声明的家庭没有分配/覆盖记录，或出现未知家庭）
  2. 同家庭跨组或跨层（分配行重复且 arm/stratum 冲突，或 plan 内跨层）
  3. 固定名额违反（每层处理家庭数 != treatment_slots）
  4. 成员多重归属（同一 person 指向多个家庭）
  5. 未知成员事件（事件的 person 在成员表中不存在）
  6. 重复事件（event_id 重复）
  7. 覆盖缺失（无覆盖行、complete=false 或无法解析）

另加金额非整数、时间戳无法解析、曝光标记与 arm 不一致等质量诊断。
原则：冲突或缺测可能破坏原随机化设计，error 一律阻止主推断，
受影响单位逐条列出；描述统计仍然计算但明确标注，不删除任何家庭。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    AssignmentRow,
    CoverageRow,
    Diagnostic,
    EventRow,
    HouseholdMetric,
    MembershipRow,
    Plan,
    StratumPlan,
)

_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f"}


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

def load_plan(path: str | Path) -> Plan:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    strata = [
        StratumPlan(
            id=s["id"],
            households=list(s["eligible_households"]),
            treatment_slots=int(s["treatment_slots"]),
        )
        for s in raw["strata"]
    ]
    inf = raw.get("inference", {})
    bounds = inf.get("search_bounds_cents", [-5000, 5000])
    return Plan(
        experiment_id=raw["experiment_id"],
        unit=raw["unit"],
        treatment_arm=raw["treatment"],
        control_arm=raw["control"],
        strata=strata,
        estimand=raw.get("estimand", ""),
        window_start=raw["window_start"],
        window_end=raw["window_end"],
        metric=raw.get("metric", "sum(amount_cents)"),
        currency=raw.get("currency", ""),
        require_complete_coverage=bool(raw.get("require_complete_coverage", True)),
        seed=int(raw.get("seed", 0)),
        null=inf.get("null", "no effect on any household"),
        search_bounds=(int(bounds[0]), int(bounds[1])),
        search_step=int(inf.get("search_step_cents", 50)),
    )


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def load_assignments(path: str | Path) -> list[AssignmentRow]:
    rows = []
    for r in _read_csv(path):
        rows.append(
            AssignmentRow(
                household_id=r["household_id"].strip(),
                stratum=r["stratum"].strip(),
                arm=r["arm"].strip(),
                exposed=_parse_bool(r.get("exposed")),
            )
        )
    return rows


def load_memberships(path: str | Path) -> list[MembershipRow]:
    return [
        MembershipRow(person_id=r["person_id"].strip(), household_id=r["household_id"].strip())
        for r in _read_csv(path)
    ]


def load_events(path: str | Path) -> list[EventRow]:
    out: list[EventRow] = []
    for r in _read_csv(path):
        amount: int | None
        try:
            amount = int(r["amount_cents"].strip())
        except (ValueError, AttributeError):
            amount = None
        out.append(
            EventRow(
                event_id=r["event_id"].strip(),
                person_id=r["person_id"].strip(),
                occurred_at=r["occurred_at"].strip(),
                amount_cents=amount,
            )
        )
    return out


def load_coverage(path: str | Path) -> list[CoverageRow]:
    return [
        CoverageRow(household_id=r["household_id"].strip(), complete=_parse_bool(r.get("complete")))
        for r in _read_csv(path)
    ]


def parse_ts(value: str) -> datetime | None:
    """解析 ISO-8601 UTC 时间戳；无法解析返回 None。"""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------
# 资格检查 + 家庭指标汇总
# --------------------------------------------------------------------------

def validate_and_aggregate(
    plan: Plan,
    assignments: list[AssignmentRow],
    memberships: list[MembershipRow],
    events: list[EventRow],
    coverage: list[CoverageRow],
) -> tuple[list[Diagnostic], dict[str, HouseholdMetric]]:
    """返回 (诊断列表, 按家庭可追溯指标)。诊断分 error/warning 两级。"""
    diags: list[Diagnostic] = []

    win_start = parse_ts(plan.window_start)
    win_end = parse_ts(plan.window_end)
    if win_start is None or win_end is None or win_start >= win_end:
        diags.append(
            Diagnostic(
                "PLAN_BAD_WINDOW",
                SEVERITY_ERROR,
                "plan 窗口无法解析或不是左闭右开的非空区间",
                [plan.window_start, plan.window_end],
            )
        )

    # ---- plan 自身一致性 -------------------------------------------------
    plan_stratum_of: dict[str, str] = {}
    for s in plan.strata:
        if not (1 <= s.treatment_slots < len(s.households)):
            diags.append(
                Diagnostic(
                    "PLAN_BAD_SLOTS",
                    SEVERITY_ERROR,
                    "处理名额必须落在 [1, 层家庭数-1]，否则层内不存在完全随机化",
                    [s.id],
                    {"treatment_slots": s.treatment_slots, "n_households": len(s.households)},
                )
            )
        if len(set(s.households)) != len(s.households):
            dup = sorted({h for h in s.households if s.households.count(h) > 1})
            diags.append(
                Diagnostic(
                    "PLAN_DUPLICATE_IN_STRATUM",
                    SEVERITY_ERROR,
                    "plan 同一层内家庭名单重复",
                    dup,
                    {"stratum": s.id},
                )
            )
        for h in s.households:
            if h in plan_stratum_of:
                diags.append(
                    Diagnostic(
                        "PLAN_HOUSEHOLD_CROSS_STRATUM",
                        SEVERITY_ERROR,
                        "plan 中同一家庭出现在多个层",
                        [h],
                        {"strata": [plan_stratum_of[h], s.id]},
                    )
                )
            else:
                plan_stratum_of[h] = s.id
    eligible = set(plan_stratum_of)

    # ---- 分配表 ----------------------------------------------------------
    seen_assign: dict[str, list[AssignmentRow]] = {}
    for r in assignments:
        seen_assign.setdefault(r.household_id, []).append(r)

    for h, rows in seen_assign.items():
        if len(rows) > 1:
            arms = sorted({r.arm for r in rows})
            strs = sorted({r.stratum for r in rows})
            cross = len(arms) > 1 or len(strs) > 1
            diags.append(
                Diagnostic(
                    "ASSIGNMENT_CROSS_ARM" if cross else "ASSIGNMENT_DUPLICATE",
                    SEVERITY_ERROR,
                    "同一家庭存在多条分配记录" + ("，且跨组或跨层冲突" if cross else "（内容重复）"),
                    [h],
                    {"rows": [r.__dict__ for r in rows]},
                )
            )
        if h not in eligible:
            diags.append(
                Diagnostic(
                    "ASSIGNMENT_UNKNOWN_HOUSEHOLD",
                    SEVERITY_ERROR,
                    "分配家庭不在 plan 的入组名单中",
                    [h],
                )
            )
        for r in rows:
            if h in plan_stratum_of and r.stratum != plan_stratum_of[h]:
                diags.append(
                    Diagnostic(
                        "ASSIGNMENT_STRATUM_MISMATCH",
                        SEVERITY_ERROR,
                        "分配行的层与 plan 声明不一致（跨层）",
                        [h],
                        {"assigned": r.stratum, "plan": plan_stratum_of[h]},
                    )
                )
            if r.arm not in (plan.treatment_arm, plan.control_arm):
                diags.append(
                    Diagnostic(
                        "ASSIGNMENT_BAD_ARM",
                        SEVERITY_ERROR,
                        "arm 既不是处理组也不是对照组",
                        [h],
                        {"arm": r.arm},
                    )
                )
            if r.exposed is None:
                diags.append(
                    Diagnostic(
                        "EXPOSED_UNPARSEABLE",
                        SEVERITY_WARNING,
                        "exposed 标记无法解析为布尔值",
                        [h],
                    )
                )
            elif r.arm == plan.control_arm and r.exposed:
                diags.append(
                    Diagnostic(
                        "EXPOSED_INCONSISTENT",
                        SEVERITY_WARNING,
                        "对照组成员出现 exposed=true；曝光不用于选择 ITT 队列，仅提示",
                        [h],
                    )
                )
            elif r.arm == plan.treatment_arm and not r.exposed:
                diags.append(
                    Diagnostic(
                        "EXPOSED_INCONSISTENT",
                        SEVERITY_WARNING,
                        "处理组成员未领券（exposed=false）；ITT 保留该家庭，不剔除",
                        [h],
                    )
                )

    missing_assign = sorted(eligible - set(seen_assign))
    if missing_assign:
        diags.append(
            Diagnostic(
                "ASSIGNMENT_MISSING",
                SEVERITY_ERROR,
                "plan 入组家庭缺少分配记录（名单缺失）",
                missing_assign,
            )
        )

    # 固定名额：只统计 arm 合法的行，冲突家庭的多条记录按集合去重后计数
    for s in plan.strata:
        treated = set()
        for h in s.households:
            for r in seen_assign.get(h, []):
                if r.arm == plan.treatment_arm:
                    treated.add(h)
        if len(treated) != s.treatment_slots:
            diags.append(
                Diagnostic(
                    "SLOT_VIOLATION",
                    SEVERITY_ERROR,
                    "层内处理家庭数不等于 plan 固定名额",
                    s.households,
                    {
                        "stratum": s.id,
                        "observed_treated": sorted(treated),
                        "observed_count": len(treated),
                        "required_slots": s.treatment_slots,
                    },
                )
            )

    # ---- 成员关系 --------------------------------------------------------
    person_households: dict[str, set[str]] = {}
    pair_count: dict[tuple[str, str], int] = {}
    household_members: dict[str, set[str]] = {h: set() for h in eligible}
    for m in memberships:
        person_households.setdefault(m.person_id, set()).add(m.household_id)
        pair_count[(m.person_id, m.household_id)] = pair_count.get(
            (m.person_id, m.household_id), 0
        ) + 1
        if m.household_id in household_members:
            household_members[m.household_id].add(m.person_id)
        else:
            diags.append(
                Diagnostic(
                    "MEMBER_UNKNOWN_HOUSEHOLD",
                    SEVERITY_WARNING,
                    "成员关系指向 plan 名单外的家庭",
                    [m.household_id],
                    {"person_id": m.person_id},
                )
            )

    ambiguous_persons = {p for p, hs in person_households.items() if len(hs) > 1}
    for p in sorted(ambiguous_persons):
        diags.append(
            Diagnostic(
                "MEMBER_MULTI_HOUSEHOLD",
                SEVERITY_ERROR,
                "成员多重归属：同一 person 指向多个家庭，事件无法唯一汇总",
                [p],
                {"household_ids": sorted(person_households[p])},
            )
        )
    for (p, h), cnt in sorted(pair_count.items()):
        if cnt > 1:
            diags.append(
                Diagnostic(
                    "MEMBER_DUPLICATE_PAIR",
                    SEVERITY_WARNING,
                    "成员关系行重复",
                    [p],
                    {"household_id": h, "count": cnt},
                )
            )
    for h in sorted(eligible):
        if not household_members[h]:
            diags.append(
                Diagnostic(
                    "MEMBER_EMPTY_HOUSEHOLD",
                    SEVERITY_WARNING,
                    "入组家庭没有任何成员记录",
                    [h],
                )
            )

    person_to_hh: dict[str, str] = {
        p: next(iter(hs)) for p, hs in person_households.items() if len(hs) == 1
    }

    # ---- 覆盖 ------------------------------------------------------------
    coverage_map: dict[str, bool | None] = {}
    for c in coverage:
        if c.household_id not in eligible:
            diags.append(
                Diagnostic(
                    "COVERAGE_UNKNOWN_HOUSEHOLD",
                    SEVERITY_WARNING,
                    "覆盖记录指向 plan 名单外的家庭",
                    [c.household_id],
                )
            )
            continue
        if c.complete is None:
            diags.append(
                Diagnostic(
                    "COVERAGE_UNPARSEABLE",
                    SEVERITY_ERROR,
                    "complete 标记无法解析，按覆盖不完整处理",
                    [c.household_id],
                )
            )
        coverage_map[c.household_id] = c.complete
    for h in sorted(eligible):
        if h not in coverage_map:
            diags.append(
                Diagnostic(
                    "COVERAGE_MISSING",
                    SEVERITY_ERROR,
                    "家庭缺少覆盖记录（覆盖缺失）",
                    [h],
                )
            )
        elif plan.require_complete_coverage and coverage_map[h] is not True:
            diags.append(
                Diagnostic(
                    "COVERAGE_INCOMPLETE",
                    SEVERITY_ERROR,
                    "complete=false：窗口内可能漏事件，无事件也不能记零",
                    [h],
                )
            )

    # ---- 事件 ------------------------------------------------------------
    event_id_count: dict[str, int] = {}
    for e in events:
        event_id_count[e.event_id] = event_id_count.get(e.event_id, 0) + 1
    duplicate_ids = sorted({eid for eid, c in event_id_count.items() if c > 1})
    for eid in duplicate_ids:
        diags.append(
            Diagnostic(
                "EVENT_DUPLICATE_ID",
                SEVERITY_ERROR,
                "event_id 重复，所有同 ID 事件均不计入金额",
                [eid],
                {"count": event_id_count[eid]},
            )
        )

    # 每个家庭的事件归集容器
    metrics: dict[str, HouseholdMetric] = {}
    arm_of = {h: (seen_assign[h][0].arm if h in seen_assign else None) for h in eligible}
    exposed_of = {h: (seen_assign[h][0].exposed if h in seen_assign else None) for h in eligible}
    for h in eligible:
        metrics[h] = HouseholdMetric(
            household_id=h,
            stratum=plan_stratum_of.get(h),
            arm=arm_of[h],
            exposed=exposed_of[h],
            member_person_ids=sorted(household_members[h]),
            in_window_event_ids=[],
            in_window_amount_cents=0,
            out_of_window_event_ids=[],
            out_of_window_amount_cents=0,
            duplicate_event_ids=[],
            unattributable_event_ids=[],
            covered=coverage_map.get(h),
            zero_recorded=False,
            metric_cents=None,
            metric_status="computed",
        )

    # 多重归属成员关联到的所有候选家庭，都要标记归属不明
    ambiguous_candidate_hh: dict[str, set[str]] = {}
    for p in ambiguous_persons:
        ambiguous_candidate_hh[p] = {
            h for h in person_households[p] if h in eligible
        }

    for e in events:
        if e.event_id in duplicate_ids:
            hh = person_to_hh.get(e.person_id)
            if hh in metrics:
                metrics[hh].duplicate_event_ids.append(e.event_id)
            continue
        if e.amount_cents is None:
            diags.append(
                Diagnostic(
                    "EVENT_NON_INTEGER_AMOUNT",
                    SEVERITY_ERROR,
                    "金额不是整数分，事件不计入",
                    [e.event_id],
                    {"person_id": e.person_id},
                )
            )
            continue
        ts = parse_ts(e.occurred_at)
        if ts is None:
            diags.append(
                Diagnostic(
                    "EVENT_BAD_TIMESTAMP",
                    SEVERITY_WARNING,
                    "时间戳无法解析，事件不进入窗口",
                    [e.event_id],
                    {"occurred_at": e.occurred_at},
                )
            )
            continue

        if e.person_id in ambiguous_persons:
            diags.append(
                Diagnostic(
                    "EVENT_AMBIGUOUS_PERSON",
                    SEVERITY_ERROR,
                    "事件来自多重归属成员，无法唯一汇总到家庭",
                    [e.event_id],
                    {
                        "person_id": e.person_id,
                        "candidate_households": sorted(ambiguous_candidate_hh[e.person_id]),
                    },
                )
            )
            for h in ambiguous_candidate_hh[e.person_id]:
                metrics[h].unattributable_event_ids.append(e.event_id)
                metrics[h].metric_status = "ambiguous_attribution"
            continue
        if e.person_id not in person_to_hh:
            diags.append(
                Diagnostic(
                    "EVENT_UNKNOWN_PERSON",
                    SEVERITY_ERROR,
                    "事件的成员不在成员表中，无法归集（未知成员事件）",
                    [e.event_id],
                    {"person_id": e.person_id},
                )
            )
            continue

        hh = person_to_hh[e.person_id]
        if hh not in metrics:
            # 成员挂在名单外家庭：已在成员检查处告警，这里不重复
            continue
        if win_start is not None and win_end is not None and win_start <= ts < win_end:
            metrics[hh].in_window_event_ids.append(e.event_id)
            metrics[hh].in_window_amount_cents += e.amount_cents
        else:
            metrics[hh].out_of_window_event_ids.append(e.event_id)
            metrics[hh].out_of_window_amount_cents += e.amount_cents

    # ---- 定家庭指标：覆盖完整才可记零 ------------------------------------
    for h, m in metrics.items():
        if m.metric_status == "ambiguous_attribution":
            m.metric_cents = None
        elif m.covered is not True:
            m.metric_status = "missing_coverage"
            m.metric_cents = None
        else:
            m.metric_cents = m.in_window_amount_cents
            m.zero_recorded = m.in_window_amount_cents == 0
        for key in ("in_window_event_ids", "out_of_window_event_ids",
                    "duplicate_event_ids", "unattributable_event_ids"):
            setattr(m, key, sorted(set(getattr(m, key))))

    return diags, metrics


def is_eligible(diags: list[Diagnostic]) -> bool:
    """无 error 诊断即合格；warning 不阻止主推断。"""
    return not any(d.severity == SEVERITY_ERROR for d in diags)
