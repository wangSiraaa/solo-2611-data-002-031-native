"""在线迁移引擎。

关键机制
========
1. 可靠变更捕获
   record_changes 由源表触发器写入，与源写入同一事务提交；回滚的写入在
   record_changes 里同样不存在，迁移天然看不到。

2. 不把“最大编号”当“全部已提交”
   change_seq 来自 SEQUENCE，长事务可能先领号后提交，编号之间出现“洞”。
   引擎维护 *连续水位线* watermark：只在 change_seq 连续前缀上推进；
   洞位之后、已经可见的变更可以先应用（版本守卫保证幂等），但不推进
   watermark。等迟到事务提交后再补推。

3. 回填与变更的竞争（核心）
   - 每个块在一个事务内先按主键范围取 id，再逐行 SELECT ... FOR UPDATE：
     * 删除已提交 -> 读不到行 -> 跳过，不可能复活；
     * 删除/更新未提交 -> 行锁阻塞至其提交，再按结果处理；
     * 回填持锁期间其他事务无法修改该行，“先读旧行后收到删除”无处发生。
   - 对“显式持有的旧快照行”（apply_backfill_row）再读一次当前版本：
     行已消失 -> 跳过；当前版本更新 -> 旧回填过期，交给变更流。
   - 版本守卫（canonical.row_version 比较）保证旧快照/旧变更无法覆盖新版本。

4. 崩溃续跑
   所有进度（块、水位线、状态、失败位置）都在数据库里持久化；每个块与
   每批变更在提交点一起落盘。进程被 SIGKILL 后未提交的工作整体回滚，
   resume 从最后一个已提交进度继续，目标表不清空、不重新全量复制。

5. 单实例互斥
   固定 key 的 pg_advisory_lock 保证不同进程不能同时推进同一迁移。
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Any

from .normalize import NormalizeError, normalize_amount, normalize_code

# 固定咨询锁键（"ONLM" 的 32 位值），整个迁移共用
MIGRATION_LOCK_KEY = 0x4F4E4C4D
CHANGE_BATCH = 200

# 故障注入点：均位于“工作已完成、事务尚未提交”之处
FAULT_STEP_COMMIT = "step_commit"
FAULT_DRAIN_COMMIT = "drain_commit"
FAULT_FINALIZE_COMMIT = "finalize_commit"
KNOWN_FAULT_POINTS = (FAULT_STEP_COMMIT, FAULT_DRAIN_COMMIT, FAULT_FINALIZE_COMMIT)


class MigrationError(RuntimeError):
    pass


class FinalizeBlocked(MigrationError):
    """finalize 核验未通过（含格式错误），迁移不能标记 READY。"""


# --------------------------------------------------------------------------- #
# 故障注入
# --------------------------------------------------------------------------- #
class FaultGate:
    """通过环境变量 ONLMIG_FAULT 控制的故障注入。

    语法: "注入点:第几次命中[:提交前睡眠秒数]"，例如
      ONLMIG_FAULT=step_commit:2          第 2 次块提交前杀死进程
      ONLMIG_FAULT=finalize_commit:1      finalize 提交前杀死进程
    用 os._exit 模拟 SIGKILL：不执行 Python 清理、不提交事务。
    """

    def __init__(self, spec: str | None = None):
        self.spec = spec
        self.point: str | None = None
        self.count = 0
        self.sleep_before_kill = 0.0
        self._hits: dict[str, int] = {}
        if spec:
            parts = spec.split(":")
            self.point, cnt = parts[0], parts[1]
            if self.point not in KNOWN_FAULT_POINTS:
                raise ValueError(f"未知故障注入点: {self.point}")
            self.count = int(cnt)
            if len(parts) > 2:
                self.sleep_before_kill = float(parts[2])

    @classmethod
    def from_env(cls) -> "FaultGate":
        return cls(os.environ.get("ONLMIG_FAULT"))

    def hit(self, point: str) -> None:
        if not self.point or point != self.point:
            return
        self._hits[point] = self._hits.get(point, 0) + 1
        if self._hits[point] != self.count:
            return
        if self.sleep_before_kill:
            import time

            time.sleep(self.sleep_before_kill)
        sys.stdout.flush()
        sys.stderr.flush()
        print(f"\n[故障注入] 在 {point} 第 {self.count} 次命中点杀死进程（os._exit，未提交）")
        sys.stdout.flush()
        os._exit(2)


# --------------------------------------------------------------------------- #
# 互斥锁与元数据
# --------------------------------------------------------------------------- #
def acquire_lock(conn) -> None:
    """阻塞获取迁移咨询锁；拿不到说明已有迁移进程在推进。"""
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", [MIGRATION_LOCK_KEY]).fetchone()[0]
    if not got:
        raise MigrationError("另一个迁移进程正在推进同一迁移（咨询锁被占用），本进程退出")


def release_lock(conn) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_KEY])


def lock_held_by_other(conn) -> bool:
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", [MIGRATION_LOCK_KEY]).fetchone()[0]
    if got:
        conn.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_KEY])
        return False
    return True


def get_meta(conn) -> dict[str, Any]:
    row = conn.execute(
        """SELECT state, backfill_after_id, backfill_done, watermark, applied_max,
                  chunk_size, fail_stage, fail_message
             FROM migration_meta WHERE id = 1"""
    ).fetchone()
    if row is None:
        raise MigrationError("迁移尚未 start")
    keys = ("state", "backfill_after_id", "backfill_done", "watermark",
            "applied_max", "chunk_size", "fail_stage", "fail_message")
    return dict(zip(keys, row))


def start(conn, chunk_size: int) -> bool:
    """启动迁移：登记元数据并获取互斥锁。返回 True 表示新建。"""
    acquire_lock(conn)
    with conn.transaction():
        row = conn.execute("SELECT 1 FROM migration_meta WHERE id=1").fetchone()
        if row is not None:
            return False
        conn.execute(
            "INSERT INTO migration_meta (id, state, chunk_size) VALUES (1, 'RUNNING', %s)",
            [chunk_size],
        )
    return True


def reset(conn) -> None:
    """仅演示用：清空全部迁移产物、变更日志与源表，回到完全初始状态。"""
    acquire_lock(conn)
    with conn.transaction():
        # TRUNCATE 级联清空；TRUNCATE 不触发行触发器，审计日志也由 CASCADE 之外的
        # 显式清空保证干净（record_changes 无外键，逐一列出即可）
        conn.execute("TRUNCATE records, record_changes, canonical_records,"
                     " migration_chunks, migration_errors, migration_meta RESTART IDENTITY")
        conn.execute("ALTER SEQUENCE record_changes_seq RESTART WITH 1")
    release_lock(conn)


def _mark_failed(conn, stage: str, message: str) -> None:
    conn.execute(
        """UPDATE migration_meta
              SET state='FAILED', fail_stage=%s, fail_message=%s, updated_at=now()
            WHERE id=1""",
        [stage, message[:2000]],
    )


# --------------------------------------------------------------------------- #
# 单行处理：变更流与回填共用，全部带版本守卫
# --------------------------------------------------------------------------- #
def _delete_guarded(conn, rid: int, version: int, stage: str) -> None:
    """按版本守卫删除目标行；该 id 被删除即其全部历史失败位置一并失效。"""
    conn.execute(
        "DELETE FROM canonical_records WHERE id=%s AND row_version <= %s",
        [rid, version],
    )
    # 一个 id 同一时刻只有一个存活版本；行已删除，任何历史版本的错误位置
    # 都是过期信息，全部清除（避免 migration_errors 残留幽灵记录）。
    conn.execute("DELETE FROM migration_errors WHERE id=%s", [rid])


def _upsert_live_row(conn, rid: int, version: int, raw_code: str,
                     amount_cents: int, active: bool, stage: str) -> str:
    """规范化并按版本守卫写入。返回 'ok' 或 'error'。"""
    try:
        code = normalize_code(raw_code)
        amount = normalize_amount(amount_cents)
    except NormalizeError as exc:
        # 新版本是错误行：登记失败位置，并撤掉可能存在的旧版本规范行
        # 与该 id 的历史错误记录（同一 id 只允许保留当前版本的失败位置）
        conn.execute(
            """INSERT INTO migration_errors (id, row_version, stage, raw_code, message)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id, row_version) DO UPDATE
                 SET stage=EXCLUDED.stage, raw_code=EXCLUDED.raw_code,
                     message=EXCLUDED.message, created_at=now()""",
            [rid, version, stage, raw_code, str(exc)],
        )
        conn.execute("DELETE FROM migration_errors WHERE id=%s AND row_version <> %s",
                     [rid, version])
        conn.execute(
            "DELETE FROM canonical_records WHERE id=%s AND row_version <= %s",
            [rid, version],
        )
        return "error"

    # 版本守卫：只在目标缺失或更旧时写入；新变更已应用、旧回填稍后到达时
    # 这条语句影响 0 行，旧值无法覆盖新值。
    conn.execute(
        """INSERT INTO canonical_records (id, code, amount, active, row_version)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE
             SET code=EXCLUDED.code, amount=EXCLUDED.amount,
                 active=EXCLUDED.active, row_version=EXCLUDED.row_version
           WHERE canonical_records.row_version < EXCLUDED.row_version""",
        [rid, code, amount, active, version],
    )
    conn.execute(
        "DELETE FROM migration_errors WHERE id=%s AND row_version <= %s",
        [rid, version],
    )
    return "ok"


def apply_change(conn, change: tuple[int, int, str, int], stage: str = "change") -> str:
    """应用一条 record_changes 记录（调用方负责事务）。"""
    seq, rid, op, version = change
    if op == "D":
        _delete_guarded(conn, rid, version, stage)
        return "delete"
    src = conn.execute(
        "SELECT raw_code, amount_cents, active FROM records WHERE id=%s", [rid]
    ).fetchone()
    if src is None:
        # 变更可见但源行已不存在（后续删除已提交）：按删除处理
        _delete_guarded(conn, rid, version, stage)
        return "delete"
    return _upsert_live_row(conn, rid, version, src[0], src[1], src[2], stage)


def apply_backfill_row(conn, row: tuple[int, str, int, bool, int]) -> str:
    """处理一条回填扫描行。

    ``row`` 可能来自稍早的快照。重新读取当前提交版本并加行锁：
      * 行已删除（读不到）        -> skipped_deleted，绝不复活；
      * 当前版本比快照行更新       -> stale，交给变更流（或下一块）；
      * 版本一致且行锁在手         -> 安全规范化写入。
    """
    rid, _old_code, _old_amt, _old_active, snapshot_version = row
    cur = conn.execute(
        """SELECT id, raw_code, amount_cents, active, row_version
             FROM records WHERE id=%s FOR UPDATE""",
        [rid],
    ).fetchone()
    if cur is None:
        return "skipped_deleted"
    if cur[4] != snapshot_version:
        return "stale"
    return _upsert_live_row(conn, cur[0], cur[4], cur[1], cur[2], cur[3], "backfill")


# --------------------------------------------------------------------------- #
# 变更流排空：连续水位线 + 洞位预应用
# --------------------------------------------------------------------------- #
def drain_changes(conn, gate: FaultGate | None = None) -> dict[str, int]:
    """排空所有当前可见的变更，返回本轮回填统计。

    两个扫描方向缺一不可：
      1. 洞位补扫：(watermark, applied_max] 内此前不可见、现在可能已提交的
         行（早领号晚提交的事务）。只按 change_seq > applied_max 扫描会把
         这些行永久跳过——这正是“不能只看最大编号”的关键所在。
      2. 新增扫描：change_seq > applied_max 的新变更；洞位之后的可先应用，
         但 watermark 只沿连续前缀推进。
    重复应用由版本守卫保证幂等。
    """
    stats = {"applied": 0, "deleted": 0, "errors": 0, "batches": 0}
    meta = get_meta(conn)
    applied_max = meta["applied_max"]
    watermark = meta["watermark"]

    while True:
        with conn.transaction():
            # 1) 洞位补扫：水位线之后、applied_max 之前（含），现在才可见的行
            gap_rows = conn.execute(
                """SELECT change_seq, id, op, row_version
                     FROM record_changes
                    WHERE change_seq > %s AND change_seq <= %s
                    ORDER BY change_seq""",
                [watermark, applied_max],
            ).fetchall()
            for change in gap_rows:
                outcome = apply_change(conn, change)
                stats["applied"] += 1
                if outcome == "delete":
                    stats["deleted"] += 1
                elif outcome == "error":
                    stats["errors"] += 1

            # 2) 新增扫描
            rows = conn.execute(
                """SELECT change_seq, id, op, row_version
                     FROM record_changes
                    WHERE change_seq > %s
                    ORDER BY change_seq
                    LIMIT %s""",
                [applied_max, CHANGE_BATCH],
            ).fetchall()
            if not rows and not gap_rows:
                break
            stats["batches"] += 1
            for change in rows:
                outcome = apply_change(conn, change)
                stats["applied"] += 1
                if outcome == "delete":
                    stats["deleted"] += 1
                elif outcome == "error":
                    stats["errors"] += 1

            # 只在连续前缀上推进 watermark；洞位（回滚洞或先领号后提交的
            # 未提交事务）之后的变更虽然已经应用，但不能让 watermark 跨过洞。
            # 期望编号在批次间延续：本批起点可能正好落在洞之后，不能只在
            # 本批行内重新计数，否则水位线永远停在洞前。
            expect = watermark + 1
            contiguous = 0
            for change in gap_rows + rows:
                if change[0] != expect:
                    break
                contiguous += 1
                expect += 1
            watermark += contiguous
            if rows:
                applied_max = rows[-1][0]

            conn.execute(
                """UPDATE migration_meta
                      SET watermark=%s, applied_max=%s, updated_at=now()
                    WHERE id=1""",
                [watermark, applied_max],
            )
            if gate:
                gate.hit(FAULT_DRAIN_COMMIT)
        if len(rows) < CHANGE_BATCH:
            break
    return stats


# --------------------------------------------------------------------------- #
# 历史回填：按主键分块，每块持久化进度
# --------------------------------------------------------------------------- #
def backfill_one_chunk(conn, gate: FaultGate | None = None) -> dict[str, Any] | None:
    meta = get_meta(conn)
    if meta["backfill_done"]:
        return None
    chunk_size = meta["chunk_size"]
    lo = meta["backfill_after_id"]
    hi = lo + chunk_size

    scanned = applied = errors = skipped = 0
    # 块事务：先在主键范围内取 id，再逐行 FOR UPDATE 读取当前版本。
    # READ COMMITTED 下 FOR UPDATE 会等待并发写事务提交并拿到其最新版本；
    # 块内所有工作与进度更新同一事务，进程在提交前被杀则整体回滚，
    # 重启后该块重做（全部操作幂等）。
    with conn.transaction():
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM records WHERE id > %s AND id <= %s ORDER BY id",
            [lo, hi],
        ).fetchall()]
        for rid in ids:
            scanned += 1
            row = conn.execute(
                """SELECT id, raw_code, amount_cents, active, row_version
                     FROM records WHERE id=%s FOR UPDATE""",
                [rid],
            ).fetchone()
            if row is None:
                # 拿到 id 后、加锁前被并发删除
                skipped += 1
                continue
            outcome = _upsert_live_row(conn, row[0], row[4], row[1], row[2], row[3], "backfill")
            if outcome == "ok":
                applied += 1
            elif outcome == "error":
                errors += 1

        more = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM records WHERE id > %s)", [hi]
        ).fetchone()[0]
        # 空区间快速跳跃：范围内无行且更远处仍有行时，前沿直接推到下一个
        # 现存 id 的前一位（backfill_after_id 语义为“该值以下均已处理”），
        # 下一块 id > frontier 即可包含 next_id；不会跳过任何现存行，
        # 也避免稀疏主键导致上万次空块事务。
        if not ids and more:
            next_id = conn.execute(
                "SELECT min(id) FROM records WHERE id > %s", [hi]
            ).fetchone()[0]
            frontier = next_id - 1
            id_range = f"({lo}, {hi}] 空区间，跳跃前沿至 {frontier}"
        else:
            frontier = hi
            id_range = f"({lo}, {hi}]"
        chunk_no = conn.execute(
            "SELECT COALESCE(MAX(chunk_no), 0) + 1 FROM migration_chunks"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO migration_chunks
                   (chunk_no, id_range, state, rows_scanned, rows_applied, errors)
               VALUES (%s, %s, 'DONE', %s, %s, %s)""",
            [chunk_no, id_range, scanned, applied, errors],
        )
        conn.execute(
            """UPDATE migration_meta
                  SET backfill_after_id=%s, backfill_done=%s,
                      state='RUNNING', fail_stage=NULL, fail_message=NULL,
                      updated_at=now()
                WHERE id=1""",
            [frontier, not more],
        )
        if gate:
            gate.hit(FAULT_STEP_COMMIT)

    return {"chunk_no": chunk_no, "range": (lo, frontier), "scanned": scanned,
            "applied": applied, "errors": errors, "skipped_deleted": skipped,
            "backfill_done": not more}


def has_pending_changes(conn) -> bool:
    meta = get_meta(conn)
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM record_changes WHERE change_seq > %s)",
        [meta["applied_max"]],
    ).fetchone()
    return row[0]


def step(conn, gate: FaultGate | None = None) -> dict[str, Any]:
    """单个迁移步进：一个回填块（若有）+ 一轮变更排空。"""
    acquire_lock(conn)
    meta = get_meta(conn)
    if meta["state"] == "READY":
        raise MigrationError("迁移已 READY，无需继续 step")
    chunk = backfill_one_chunk(conn, gate)
    drain = drain_changes(conn, gate)
    return {"chunk": chunk, "drain": drain}


def run(conn, gate: FaultGate | None = None) -> dict[str, int]:
    """持续推进直到历史回填完成且无待处理变更（不做 finalize 核验）。"""
    acquire_lock(conn)
    total = {"chunks": 0, "changes": 0}
    while True:
        chunk = backfill_one_chunk(conn, gate)
        if chunk:
            total["chunks"] += 1
        drain = drain_changes(conn, gate)
        total["changes"] += drain["applied"]
        meta = get_meta(conn)
        if meta["backfill_done"] and not has_pending_changes(conn):
            return total


# --------------------------------------------------------------------------- #
# 核验与 finalize
# --------------------------------------------------------------------------- #
def _snapshot_compare(conn) -> dict[str, Any]:
    """在当前事务快照内逐主键逐字段比较源表与规范表。"""
    source = {
        r[0]: r
        for r in conn.execute(
            "SELECT id, raw_code, amount_cents, active, row_version FROM records"
        ).fetchall()
    }
    target = {
        r[0]: r
        for r in conn.execute(
            "SELECT id, code, amount, active, row_version FROM canonical_records"
        ).fetchall()
    }
    mismatches: list[dict[str, Any]] = []
    for rid, (_, raw_code, amount_cents, active, s_ver) in source.items():
        try:
            exp_code = normalize_code(raw_code)
            exp_amount = normalize_amount(amount_cents)
        except NormalizeError:
            # 错误行不应出现在规范表；若出现即为不一致
            if rid in target:
                mismatches.append({"id": rid, "problem": "错误行出现在规范表"})
            continue
        tgt = target.pop(rid, None)
        if tgt is None:
            mismatches.append({"id": rid, "problem": "规范表缺失"})
            continue
        _, t_code, t_amount, t_active, t_ver = tgt
        problems = []
        if t_code != exp_code:
            problems.append(f"code: 期望 {exp_code!r} 实际 {t_code!r}")
        if Decimal(t_amount) != exp_amount:
            problems.append(f"amount: 期望 {exp_amount} 实际 {t_amount}")
        if bool(t_active) != bool(active):
            problems.append(f"active: 期望 {active} 实际 {t_active}")
        if t_ver != s_ver:
            problems.append(f"row_version: 期望 {s_ver} 实际 {t_ver}")
        if problems:
            mismatches.append({"id": rid, "problem": "; ".join(problems)})
    extras = sorted(target.keys())  # 目标中源表不存在的额外行
    err_count = conn.execute("SELECT count(*) FROM migration_errors").fetchone()[0]
    max_change = conn.execute(
        "SELECT COALESCE(MAX(change_seq), 0) FROM record_changes"
    ).fetchone()[0]
    return {
        "mismatches": mismatches,
        "extra_rows": extras,
        "error_rows": err_count,
        "max_change_seq": max_change,
        "source_rows": len(source),
        "target_rows": len(target) + (len(source) - len(extras)),
    }


def count_unapplied_changes(conn, watermark: int) -> int:
    """水位线之后是否存在“最终影响尚未应用”的已提交变更。

    回滚事务也会消耗 sequence，故水位线之后必然存在两类东西：
      * 回滚洞：record_changes 里根本没有该行（无需处理）；
      * 已提交但被洞位预应用/漏扫的变更：必须确认其*最终*影响已落到目标。

    同一 id 的中间变更允许被后续变更取代（I→D、U→D、D→I 链），因此只核对
    每个 id 的最后一条变更；规范化失败的行其处理结果是 migration_errors
    记录（由 finalize/verify 的错误检查单独把关），不算未应用。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (c.id) c.op, c.id, c.row_version AS c_ver,
               (SELECT k.row_version FROM canonical_records k WHERE k.id = c.id) AS t_ver,
               EXISTS (SELECT 1 FROM migration_errors e
                        WHERE e.id = c.id AND e.row_version = c.row_version) AS is_error
          FROM record_changes c
         WHERE c.change_seq > %s
         ORDER BY c.id, c.change_seq DESC
        """,
        [watermark],
    ).fetchall()
    bad = 0
    for op, rid, c_ver, t_ver, is_error in rows:
        if is_error:
            continue  # 处理结果是失败位置记录，由错误检查项负责
        if op in ("I", "U"):
            if t_ver is None or t_ver < c_ver:
                bad += 1
        else:  # D：最终变更为删除时目标不得有不旧于该版本的存活行
            if t_ver is not None and t_ver >= c_ver:
                bad += 1
    return bad


def verify(conn) -> dict[str, Any]:
    """只读核验，任何阶段都可运行。使用单一时点快照（REPEATABLE READ）。"""
    import psycopg

    meta = get_meta(conn)
    prev_level = conn.isolation_level
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        with conn.transaction():
            report = _snapshot_compare(conn)
            report["state"] = meta["state"]
            report["watermark"] = meta["watermark"]
            report["applied_max"] = meta["applied_max"]
            report["backfill_after_id"] = meta["backfill_after_id"]
            report["backfill_done"] = meta["backfill_done"]
            # 待处理：已提交但尚未应用的变更（> applied_max）
            report["pending_changes"] = conn.execute(
                "SELECT count(*) FROM record_changes WHERE change_seq > %s",
                [meta["applied_max"]],
            ).fetchone()[0]
            # 水位线之后的已提交变更是否都已实际生效（回滚洞不计入）
            report["unapplied_behind_watermark"] = count_unapplied_changes(
                conn, meta["watermark"]
            )
            # 回滚会留下永久 sequence 洞，watermark 不要求等于最大编号；
            # READY 的正确判据是：水位线之后不存在“影响未应用”的已提交变更。
            report["watermark_tight"] = report["unapplied_behind_watermark"] == 0
            report["ok"] = (
                not report["mismatches"]
                and not report["extra_rows"]
                and report["error_rows"] == 0
                and report["pending_changes"] == 0
                and report["watermark_tight"]
            )
            return report
    finally:
        conn.isolation_level = prev_level


def finalize(conn, gate: FaultGate | None = None) -> dict[str, Any]:
    """短暂阻止源写入，排空已提交变更，在确定边界核验通过后才 READY。

    分两个事务：
      T1 在 SHARE 锁内排空变更、补齐回填并提交——失败位置（migration_errors）
         随这批工作一起持久化，不会因随后核验失败而回滚；
      T2 在同一把 SHARE 锁边界内做只读核验：通过才提交 READY；不通过则
         持久化 FAILED 与失败原因后提交，再抛出 FinalizeBlocked。
    """
    acquire_lock(conn)
    # ---- T1：阻止源写入 + 排空 + 补齐回填，提交已完成的工作 ----------------
    with conn.transaction():
        # SHARE 锁：阻止 INSERT/UPDATE/DELETE（它们需要 ROW EXCLUSIVE），
        # 但允许其他会话读取；这是“短暂阻止源写入”的边界。
        conn.execute("LOCK TABLE records IN SHARE MODE")
        drain_stats = drain_changes(conn, gate)
        if not get_meta(conn)["backfill_done"]:
            while backfill_one_chunk(conn, gate) is not None:
                pass

    # ---- T2：确定边界上的核验，通过才 READY，否则持久化 FAILED -------------
    with conn.transaction():
        conn.execute("LOCK TABLE records IN SHARE MODE")
        report = _snapshot_compare(conn)
        meta = get_meta(conn)
        # 水位线之后是否存在“影响尚未应用”的已提交变更。
        # 回滚事务也会消耗 sequence（永久洞，record_changes 无对应行），
        # 故不能简单按 watermark 之后是否还有行来判断。
        unapplied = count_unapplied_changes(conn, meta["watermark"])
        problems = list(report["mismatches"])
        if report["extra_rows"]:
            problems.append({"id": None, "problem": f"规范表额外行: {report['extra_rows'][:20]}"})
        if unapplied:
            problems.append({"id": None,
                            "problem": f"水位线 {meta['watermark']} 之后仍有 {unapplied} 条已提交变更的影响未应用"})
        if report["error_rows"]:
            sample = conn.execute(
                "SELECT id, row_version, message FROM migration_errors LIMIT 5"
            ).fetchall()
            problems.append({"id": None,
                            "problem": f"{report['error_rows']} 行格式错误阻断完成，示例: {sample}"})

        if problems:
            _mark_failed(conn, "finalize", f"核验失败 {len(problems)} 项: {problems[:5]}")
            if gate:
                gate.hit(FAULT_FINALIZE_COMMIT)
            blocked = FinalizeBlocked(
                f"finalize 被阻断（格式错误/不一致），共 {len(problems)} 项: {problems[:5]}"
            )
        else:
            conn.execute(
                "UPDATE migration_meta SET state='READY', fail_stage=NULL,"
                " fail_message=NULL, updated_at=now() WHERE id=1"
            )
            if gate:
                gate.hit(FAULT_FINALIZE_COMMIT)
            blocked = None

    report["drain"] = drain_stats
    if blocked is not None:
        raise blocked
    report["state"] = "READY"
    return report
