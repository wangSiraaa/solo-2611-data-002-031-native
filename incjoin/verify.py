"""验证：用独立的完整 SQL JOIN + GROUP BY 重算并逐行比对。

本模块的重算只用于验证，不参与增量实现；即使 incjoin.service 全部失效，
这里的结果也只依赖 l_source / r_source 两张源表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import connect
from .errors import VerifyError

# 完整重算明细：直接从两张源表 JOIN，不读 join_detail
_RECOMPUTE_DETAIL = """
SELECT l.row_id AS l_row_id, r.row_id AS r_row_id,
       l.join_key, l.qty, r.unit_cents,
       l.qty * r.unit_cents AS amount_cents
FROM l_source l
JOIN r_source r ON r.join_key = l.join_key
"""

# 完整重算汇总：基于完整 JOIN 的结果 GROUP BY，不读 key_summary
_RECOMPUTE_SUMMARY = """
SELECT x.join_key,
       count(*)               AS pair_count,
       sum(x.qty)             AS qty_total,
       sum(x.amount_cents)    AS amount_total_cents
FROM (
    SELECT l.join_key, l.qty, l.qty * r.unit_cents AS amount_cents
    FROM l_source l
    JOIN r_source r ON r.join_key = l.join_key
) x
GROUP BY x.join_key
"""


@dataclass
class VerifyReport:
    ok: bool = True
    detail_expected: int = 0
    detail_materialized: int = 0
    summary_expected: int = 0
    summary_materialized: int = 0
    errors: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail_expected": self.detail_expected,
            "detail_materialized": self.detail_materialized,
            "summary_expected": self.summary_expected,
            "summary_materialized": self.summary_materialized,
            "checks": self.checks,
            "errors": self.errors,
        }


def _fetch_map(cur, sql_text: str, key_cols: int) -> dict:
    cur.execute(sql_text)
    out: dict[tuple, tuple] = {}
    for row in cur.fetchall():
        key = tuple(row[:key_cols])
        if key in out:
            raise VerifyError(f"重算结果出现重复键 {key}，SQL 本身有误")
        out[key] = tuple(row)
    return out


def _diff_maps(
    expected: dict, actual: dict, label: str, report: VerifyReport
) -> None:
    only_expected = sorted(set(expected) - set(actual))
    only_actual = sorted(set(actual) - set(expected))
    for key in only_expected:
        report.fail(f"{label} 缺少物化行: key={key} expected={expected[key]}")
    for key in only_actual:
        report.fail(f"{label} 存在多余物化行: key={key} actual={actual[key]}")
    for key in sorted(set(expected) & set(actual)):
        if expected[key] != actual[key]:
            report.fail(
                f"{label} 行内容不一致: key={key} "
                f"expected={expected[key]} actual={actual[key]}"
            )


def verify(dsn: str | None) -> VerifyReport:
    report = VerifyReport()
    with connect(dsn) as conn, conn.cursor() as cur:
        # 1) 明细逐行比对（完整重算 vs 物化）
        expected_detail = _fetch_map(cur, _RECOMPUTE_DETAIL, key_cols=2)
        actual_detail = _fetch_map(
            cur,
            """
            SELECT l_row_id, r_row_id, join_key, qty, unit_cents, amount_cents
            FROM join_detail
            """,
            key_cols=2,
        )
        report.detail_expected = len(expected_detail)
        report.detail_materialized = len(actual_detail)
        _diff_maps(expected_detail, actual_detail, "join_detail", report)
        report.checks.append(
            f"明细逐行比对: 重算 {len(expected_detail)} 行 / "
            f"物化 {len(actual_detail)} 行"
        )

        # 2) 汇总逐行比对
        expected_summary = _fetch_map(cur, _RECOMPUTE_SUMMARY, key_cols=1)
        actual_summary = _fetch_map(
            cur,
            """
            SELECT join_key, pair_count, qty_total, amount_total_cents
            FROM key_summary
            """,
            key_cols=1,
        )
        report.summary_expected = len(expected_summary)
        report.summary_materialized = len(actual_summary)
        _diff_maps(expected_summary, actual_summary, "key_summary", report)
        report.checks.append(
            f"汇总逐行比对: 重算 {len(expected_summary)} 行 / "
            f"物化 {len(actual_summary)} 行"
        )

        # 3) 守恒：汇总行数等于有配对的键数；汇总总和等于明细总和
        cur.execute(
            """
            SELECT count(*), coalesce(sum(qty), 0), coalesce(sum(amount_cents), 0)
            FROM join_detail
            """
        )
        d_count, d_qty, d_amount = cur.fetchone()
        cur.execute(
            """
            SELECT count(*), coalesce(sum(pair_count), 0),
                   coalesce(sum(qty_total), 0),
                   coalesce(sum(amount_total_cents), 0)
            FROM key_summary
            """
        )
        s_keys, s_pairs, s_qty, s_amount = cur.fetchone()
        if d_count != s_pairs:
            report.fail(
                f"配对数不守恒: 明细 {d_count} != 汇总 pair_count 合计 {s_pairs}"
            )
        if d_qty != s_qty:
            report.fail(
                f"数量不守恒: 明细 qty 合计 {d_qty} != 汇总 qty_total 合计 {s_qty}"
            )
        if d_amount != s_amount:
            report.fail(
                f"金额不守恒: 明细 amount 合计 {d_amount} != "
                f"汇总 amount_total 合计 {s_amount}"
            )
        report.checks.append(
            f"守恒核对: 配对 {d_count}、数量 {d_qty}、金额(分) {d_amount}"
        )

        # 4) 每个汇总键必须能与明细 GROUP BY 对上（已由步骤 2 覆盖，
        #    再补一条零配对键不得残留的检查）
        cur.execute(
            """
            SELECT k.join_key FROM key_summary k
            WHERE k.pair_count = 0
               OR NOT EXISTS (
                    SELECT 1 FROM join_detail d WHERE d.join_key = k.join_key
               )
            """
        )
        stale = [r[0] for r in cur.fetchall()]
        if stale:
            report.fail(f"存在零配对/悬空汇总行: {stale}")

        # 5) 局部性核对：每个回执声明的 affected_keys 必须覆盖其明细/汇总改动
        cur.execute(
            """
            SELECT b.batch_id, b.affected_keys,
                   (SELECT count(*) FROM join_detail d
                    WHERE d.last_changed_batch = b.batch_id) AS d_touched,
                   (SELECT count(*) FROM key_summary k
                    WHERE k.last_changed_batch = b.batch_id) AS k_touched
            FROM batch_receipt b
            """
        )
        for batch_id, affected, d_touched, k_touched in cur.fetchall():
            affected_set = set(affected)
            cur.execute(
                "SELECT DISTINCT join_key FROM join_detail "
                "WHERE last_changed_batch = %s",
                (batch_id,),
            )
            bad_detail = {r[0] for r in cur.fetchall()} - affected_set
            cur.execute(
                "SELECT join_key FROM key_summary "
                "WHERE last_changed_batch = %s",
                (batch_id,),
            )
            bad_summary = {r[0] for r in cur.fetchall()} - affected_set
            if bad_detail or bad_summary:
                report.fail(
                    f"批次 {batch_id!r} 越界改动: 明细键 {sorted(bad_detail)} "
                    f"汇总键 {sorted(bad_summary)}，声明 {sorted(affected_set)}"
                )
        report.checks.append("批次局部性: 所有改动键均在回执 affected_keys 内")

    return report
