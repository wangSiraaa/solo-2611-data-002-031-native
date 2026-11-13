"""点-in-time 资格层：按给定时刻构造"当时真的能看到"的数据快照。

规则（对 sales 与 calendar 同样适用）：
1. 只有 available_at <= 发起时刻 的行可见，迟到行不进入快照；
2. 同一业务键（record_id 或 series+date）在同一 revision 下出现内容不同的多行
   => 同修订冲突，该键在该时刻不可信，相关折整体阻断（不猜、不任选一行）；
3. 无冲突时取 revision 最大的可见行作为"当时最高修订"；
4. 没有任何可见行 => 缺测，缺测就是缺测，绝不补零。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass
class Conflict:
    key: str
    revision: int
    values: tuple[Any, ...]
    source_lines: tuple[int, ...]
    available_at: tuple[str, ...]


@dataclass
class Snapshot:
    """某一时刻、某一数据源的可见快照。"""

    as_of: datetime
    visible: pd.DataFrame          # 无冲突、每个业务键仅保留最高修订
    conflicts: list[Conflict] = field(default_factory=list)

    def conflict_keys(self) -> set[str]:
        return {c.key for c in self.conflicts}


def _snapshot_generic(
    df: pd.DataFrame,
    as_of: pd.Timestamp,
    key_cols: list[str],
    value_col: str,
    key_renderer,
    has_revision: bool = True,
) -> tuple[pd.DataFrame, list[Conflict]]:
    visible = df[df["available_at"] <= pd.Timestamp(as_of)].copy()
    if visible.empty:
        return visible.iloc[0:0].copy(), []

    # 1) 同键同 revision 内容冲突检测（内容不同即冲突；完全重复行视为安全重复）。
    #    日历无 revision 维度时，同键多行内容不同即冲突。
    conflicts: list[Conflict] = []
    conflict_mask = pd.Series(False, index=visible.index)
    grouping = (["revision"] if has_revision else []) + key_cols
    for group_key, grp in visible.groupby(grouping, sort=False):
        # 单元素分组时 pandas 返回标量，统一成 tuple 再解包。
        group_tuple = group_key if isinstance(group_key, tuple) else (group_key,)
        if has_revision:
            rev, *key_parts = group_tuple
        else:
            rev, key_parts = -1, list(group_tuple)
        distinct = grp[value_col].dropna().unique()
        # 完全相同的物理重复行不构成冲突；值有分歧才阻断。
        if len(distinct) > 1:
            conflicts.append(
                Conflict(
                    key=key_renderer(key_parts),
                    revision=int(rev),
                    values=tuple(float(v) if value_col == "sales" else int(v) for v in distinct),
                    source_lines=tuple(int(x) for x in grp["source_line"].tolist()),
                    available_at=tuple(
                        pd.Timestamp(x).isoformat() for x in grp["available_at"].tolist()
                    ),
                )
            )
            conflict_mask.loc[grp.index] = True

    trusted = visible.loc[~conflict_mask].copy()
    if trusted.empty:
        return trusted, conflicts

    # 2) 每个业务键取最高修订（同修订在此处已保证内容一致）；
    #    无修订维度时同键多行内容一致，保留任一行即可。
    sort_cols = (["revision"] if has_revision else []) + ["available_at", "source_line"]
    trusted = trusted.sort_values(sort_cols)
    trusted = trusted.groupby(key_cols, as_index=False, sort=False).tail(1)
    trusted = trusted.sort_values(key_cols).reset_index(drop=True)
    return trusted, conflicts


def sales_snapshot(sales: pd.DataFrame, as_of: datetime) -> Snapshot:
    """构造 sales 在 as_of 时刻的可信快照。"""

    def render_key(parts: tuple) -> str:
        # key_cols = ["record_id"]，parts 只有一个元素
        return str(parts[0])

    visible, conflicts = _snapshot_generic(
        sales, as_of, key_cols=["record_id"], value_col="sales", key_renderer=render_key
    )
    return Snapshot(as_of=as_of, visible=visible, conflicts=conflicts)


def calendar_snapshot(calendar: pd.DataFrame, as_of: datetime) -> Snapshot:
    """构造 calendar 在 as_of 时刻的可信快照（迟公布停业不会倒灌）。"""

    def render_key(parts: tuple) -> str:
        series_id, business_date = parts
        return f"{series_id}:{business_date}"

    visible, conflicts = _snapshot_generic(
        calendar,
        as_of,
        key_cols=["series_id", "business_date"],
        value_col="planned_open",
        key_renderer=render_key,
        has_revision=False,
    )
    return Snapshot(as_of=as_of, visible=visible, conflicts=conflicts)


def conflicts_in_window(
    snapshot: Snapshot,
    series_id: str,
    date_from: Any,
    date_to: Any,
) -> list[Conflict]:
    """返回快照中、指定门店且业务日期落在 [date_from, date_to] 的冲突。

    窗口覆盖训练窗 + 最大滞后 + 预测目标日，即该折真正可能消费到的全部记录。
    """
    out: list[Conflict] = []
    for c in snapshot.conflicts:
        # sales 冲突键形如 "A:2025-02-01"；calendar 冲突键同样是 "series:date"
        sid, _, dstr = c.key.partition(":")
        if sid != series_id:
            continue
        try:
            bdate = pd.Timestamp(dstr).date()
        except ValueError:
            continue
        if date_from <= bdate <= date_to:
            out.append(c)
    return out
