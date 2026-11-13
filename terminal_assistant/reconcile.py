"""Pure reconciliation logic (no PTY, no database).

These helpers decide a readback disposition from visible values only.  They
are kept separate so the decision rules can be unit-tested exhaustively.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def price(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def values_equal(actual: dict | None, expected: dict | None) -> bool:
    if not actual or not expected:
        return False
    return (
        actual.get("name") == expected.get("name")
        and actual.get("unit") == expected.get("unit")
        and price(actual.get("price")) == price(expected.get("price"))
        and price(actual.get("price")) is not None
    )


def classify_post_save(pre: dict, plan: dict, values: dict, revision: int | None) -> tuple[str, str]:
    """Classify a readback taken after a visible save-success + editor exit.

    Returns (status, detail).  A value match with an unexpected revision is an
    anomaly, not a silent success.
    """
    values_match = values_equal(values, plan)
    expected_rev = pre.get("revision")
    expected_rev = expected_rev + 1 if isinstance(expected_rev, int) else None
    if values_match and revision == expected_rev:
        return "confirmed", "退出编辑后重新查询，现值与计划一致且修订号递增，完成该条"
    if values_match:
        return (
            "readback_revision_anomaly",
            f"现值与计划一致，但修订号 {revision} 未等于查询时 {pre.get('revision')}+1；需人工核查",
        )
    return "readback_mismatch", "保存成功提示后回读与计划不一致，需人工核查；未覆盖写入"


def classify_post_disconnect(
    pre: dict, plan: dict, values: dict, revision: int | None
) -> tuple[str, str]:
    """Classify a fresh-terminal readback after a lost success notice.

    Never claims exactly-once.  Values all equal to plan => current-value
    confirmed.  Still-old values with unchanged revision => retry-eligible.
    Anything else => conflict/unknown.
    """
    if values_equal(values, plan):
        expected_rev = pre.get("revision")
        note = (
            "；修订号恰好递增"
            if isinstance(expected_rev, int) and revision == expected_rev + 1
            else ""
        )
        return (
            "confirmed_current",
            "重启终端后按编码回读，现值全等于计划值 → 当前值已确认。"
            "这不证明恰好一次写入，仅证明当前值正确" + note,
        )
    old_match = values_equal(values, pre)
    rev_same = revision == pre.get("revision")
    if old_match and rev_same:
        return "old_unchanged", "断线后回读仍为旧值且修订号未变"
    return (
        "conflict_after_disconnect",
        "断线后回读既非计划值也非原值（或修订号不一致）→ 冲突/未知；未覆盖写入",
    )
