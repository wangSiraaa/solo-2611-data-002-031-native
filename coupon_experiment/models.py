"""输入记录与诊断信息的数据结构。

全部数据合成、规模小，使用 dataclass 即可，不引入 pandas/numpy。
金额统一为整数分（amount_cents）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# 诊断严重级别：
# - error:   破坏原随机化设计或结果测量完整性，输入不合格，停止主推断
# - warning: 不破坏设计的质量问题（如曝光标记不一致、窗口外事件、未知覆盖行）
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class Plan:
    """plan.json 中与一个实验有关的声明。"""

    experiment_id: str
    unit: str
    treatment_arm: str
    control_arm: str
    strata: list["StratumPlan"]
    estimand: str
    window_start: str
    window_end: str
    metric: str
    currency: str
    require_complete_coverage: bool
    seed: int
    null: str
    search_bounds: tuple[int, int]
    search_step: int

    @property
    def eligible_households(self) -> set[str]:
        out: set[str] = set()
        for s in self.strata:
            out.update(s.households)
        return out


@dataclass
class StratumPlan:
    id: str
    households: list[str]
    treatment_slots: int


@dataclass
class AssignmentRow:
    household_id: str
    stratum: str
    arm: str
    exposed: bool | None  # None 表示布尔列无法解析


@dataclass
class MembershipRow:
    person_id: str
    household_id: str


@dataclass
class EventRow:
    event_id: str
    person_id: str
    occurred_at: str
    amount_cents: int | None  # None 表示金额不是整数


@dataclass
class CoverageRow:
    household_id: str
    complete: bool | None  # None 表示布尔列无法解析


@dataclass
class Diagnostic:
    """单条资格/质量诊断，须能定位到受影响单位。"""

    code: str
    severity: str
    message: str
    units: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "units": list(self.units),
            "detail": self.detail,
        }


@dataclass
class HouseholdMetric:
    """单个家庭的可追溯指标：每个数都能追到成员和事件。"""

    household_id: str
    stratum: str | None
    arm: str | None
    exposed: bool | None
    member_person_ids: list[str]
    in_window_event_ids: list[str]
    in_window_amount_cents: int
    out_of_window_event_ids: list[str]
    out_of_window_amount_cents: int
    duplicate_event_ids: list[str]
    unattributable_event_ids: list[str]
    covered: bool | None
    zero_recorded: bool
    metric_cents: int | None  # None = 覆盖不完整/归属不明，不能记零
    metric_status: str  # computed | missing_coverage | ambiguous_attribution

    def to_dict(self) -> dict:
        return {
            "household_id": self.household_id,
            "stratum": self.stratum,
            "arm": self.arm,
            "exposed": self.exposed,
            "member_person_ids": list(self.member_person_ids),
            "in_window_event_ids": list(self.in_window_event_ids),
            "in_window_amount_cents": self.in_window_amount_cents,
            "out_of_window_event_ids": list(self.out_of_window_event_ids),
            "out_of_window_amount_cents": self.out_of_window_amount_cents,
            "duplicate_event_ids": list(self.duplicate_event_ids),
            "unattributable_event_ids": list(self.unattributable_event_ids),
            "covered": self.covered,
            "zero_recorded": self.zero_recorded,
            "metric_cents": self.metric_cents,
            "metric_status": self.metric_status,
        }
