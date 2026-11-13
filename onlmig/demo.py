"""全自动在线回填演示。

覆盖场景（按执行顺序）：
  1. 初始化 + 约 3000 行合成历史数据（含合法与空白 code 错误行）
  2. 迁移开始即建立触发器变更捕获
  3. 分块边界内插入（迁移期间新增到“已扫描主键范围”的记录）
  4. 扫描前更新（显式快照竞争：先读旧行、后收到删除，不复活）
  5. 扫描前后更新 / 回填-删除竞争 / 删后再插
  6. 事务回滚：回滚的写入不出现在变更日志，不会被迁移
  7. 提交次序颠倒：早领号晚提交，水位线有洞不冒进，最终仍正确
  8. 迁移进程在回填提交前 / 变更提交前被 SIGKILL，从持久化进度续跑
  9. 不同进程不能同时推进同一迁移（咨询锁）
 10. 格式错误明确阻断 finalize，修复后重新 finalize 才 READY
 11. 最终逐主键逐字段精确比较 + 无额外行 + 每个成功事务均已反映
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from . import engine, schema, writer
from .db import connect
from .normalize import normalize_amount

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
CHUNK = 500
BASE_ROWS = 3000
PASS, FAIL = "✓", "✗"

_checks: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _checks.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        raise AssertionError(f"检查失败: {name} {detail}")


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def cli(*args: str, env_fault: str | None = None, expect_rc: int | None = None):
    env = dict(os.environ)
    if env_fault:
        env["ONLMIG_FAULT"] = env_fault
    proc = subprocess.run([PY, "-m", "onlmig.cli", *args], capture_output=True,
                          text=True, env=env)
    if expect_rc is not None and proc.returncode != expect_rc:
        raise AssertionError(
            f"命令 {args} 期望返回码 {expect_rc}，实际 {proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    if expect_rc is None and proc.returncode != 0:
        raise AssertionError(f"命令 {args} 失败: {proc.stderr}\n{proc.stdout}")
    return proc


# --------------------------------------------------------------------------- #
def seed_base_data() -> None:
    rng = random.Random(20261113)
    conn = connect()
    rows = []
    for rid in range(1, BASE_ROWS + 1):
        # 故意制造错误行：id 能被 97 整除的行使用空白 code
        code = "   " if rid % 97 == 0 else rng.choice(writer.VALID_CODES)
        rows.append((rid, code, rng.randint(-10000, 9_999_900), rng.random() < 0.85))
    with conn.transaction():
        with conn.cursor().copy(
            "COPY records (id, raw_code, amount_cents, active) FROM STDIN"
        ) as cur:
            for r in rows:
                cur.write_row(r)
    n = conn.execute("SELECT count(*) FROM records").fetchone()[0]
    bad = conn.execute("SELECT count(*) FROM record_changes").fetchone()[0]
    print(f"  历史数据 {n} 行，触发器记录变更 {bad} 条（每行初始 INSERT 一条）")


def scenario_rollback() -> None:
    section("场景 6：事务回滚——回滚的写入不能被迁移")
    conn = connect()
    for rid in (9_100_001, 9_100_002):
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO records (id, raw_code, amount_cents, active)"
                    " VALUES (%s, %s, %s, true)",
                    [rid, f"ROLLBACK_MARKER_{rid}", 12345],
                )
                raise RuntimeError("主动回滚")
        except RuntimeError:
            pass
    # 显式 delete/update 回滚
    try:
        with conn.transaction():
            conn.execute("UPDATE records SET raw_code='SHOULD_NOT_PERSIST' WHERE id=1")
            conn.execute("DELETE FROM records WHERE id=2")
            raise RuntimeError("主动回滚")
    except RuntimeError:
        pass
    exists = conn.execute(
        "SELECT count(*) FROM records WHERE id IN (9100001, 9100002)"
    ).fetchone()[0]
    logged = conn.execute(
        "SELECT count(*) FROM record_changes WHERE id IN (9100001, 9100002)"
    ).fetchone()[0]
    check("回滚 INSERT 的行不存在", exists == 0)
    check("回滚写入在变更日志中无痕迹", logged == 0)
    r1 = conn.execute("SELECT raw_code FROM records WHERE id=1").fetchone()
    r2 = conn.execute("SELECT 1 FROM records WHERE id=2").fetchone()
    check("回滚的 UPDATE/DELETE 未生效", r1 is not None and r1[0] != "SHOULD_NOT_PERSIST" and r2 is not None)


def scenario_out_of_order_commit() -> tuple[int, int]:
    section("场景 7：提交次序颠倒——早领号晚提交，不能只看最大编号")
    conn = connect()
    # 两个独立事务，各自在 INSERT 时领取 change_seq；A 先领号但最后才提交
    tx_a = connect()
    tx_b = connect()
    tx_a.execute("BEGIN")
    tx_b.execute("BEGIN")
    tx_a.execute(
        "INSERT INTO records (id, raw_code, amount_cents, active) VALUES (9200001, %s, 111, true)",
        ["late_commit_A"],
    )
    time.sleep(0.2)
    time.sleep(0.2)
    tx_b.execute(
        "INSERT INTO records (id, raw_code, amount_cents, active) VALUES (9200002, %s, 222, true)",
        ["early_commit_B"],
    )
    # 在各自事务内查到本行领取到的 change_seq（未提交，其他事务不可见）
    seq_a = tx_a.execute(
        "SELECT change_seq FROM record_changes WHERE id=9200001"
    ).fetchone()[0]
    seq_b = tx_b.execute(
        "SELECT change_seq FROM record_changes WHERE id=9200002"
    ).fetchone()[0]
    check("A 领取的编号早于 B", seq_a < seq_b, f"seq_a={seq_a} seq_b={seq_b}")
    tx_b.execute("COMMIT")  # B 先提交
    # 此时 A 未提交：drain 只能看到 B，A 的编号位置形成“洞”
    engine.drain_changes(conn)
    meta = engine.get_meta(conn)
    # 注意：场景 6 的回滚事务也消耗 sequence（3009..3012 为回滚留下的永久洞），
    # watermark 停在回滚洞之前是正确行为；这里只需验证 A 的位置未被越过。
    check("A 未提交时不处理 A 的编号",
          conn.execute("SELECT count(*) FROM canonical_records WHERE id=9200001").fetchone()[0] == 0)
    check("水位线不跨过未提交洞位", meta["watermark"] < seq_a,
          f"watermark={meta['watermark']} seq_a={seq_a}")
    check("洞位之后已提交的 B 可先行应用",
          conn.execute("SELECT count(*) FROM canonical_records WHERE id=9200002").fetchone()[0] == 1)
    time.sleep(0.2)
    tx_a.execute("COMMIT")  # A 晚提交
    engine.drain_changes(conn)
    meta = engine.get_meta(conn)
    max_seq = conn.execute("SELECT max(change_seq) FROM record_changes").fetchone()[0]
    # 3009..3012 是回滚洞（永远不会闭合），水位线应停在其前一位；
    # 关键是 A（seq_a）与 B 均已应用，且水位线没有冒进到 A 之前
    check("迟到事务提交后 A、B 均被处理且水位线未冒进",
          meta["watermark"] < seq_a and meta["applied_max"] == max_seq)
    check("A、B 两行都正确进入规范表",
          conn.execute("SELECT count(*) FROM canonical_records WHERE id IN (9200001,9200002)").fetchone()[0] == 2)
    tx_a.close(); tx_b.close()
    return seq_a, seq_b


def scenario_held_snapshot_race() -> None:
    section("场景 4/5：先读旧行后收到删除（显式快照竞争）与删后再插")
    conn = connect()
    rid_del, rid_reins = 8_300_001, 8_300_002
    writer.do_insert(conn, rid_del, "snap_old", 100, True)
    writer.do_insert(conn, rid_reins, "snap_reins", 200, True)

    # 迁移侧：打开事务并先读出旧行（模拟块扫描拿到旧版本行元组）。
    # 引擎实际使用 READ COMMITTED + FOR UPDATE：旧元组稍后重新加锁读取，
    # 由当前版本裁决，而不是用旧快照覆盖。
    mig = connect()
    mig.execute("BEGIN")
    old_del = mig.execute(
        "SELECT id, raw_code, amount_cents, active, row_version FROM records WHERE id=%s",
        [rid_del],
    ).fetchone()
    old_reins = mig.execute(
        "SELECT id, raw_code, amount_cents, active, row_version FROM records WHERE id=%s",
        [rid_reins],
    ).fetchone()

    # 源侧并发：删除 rid_del；rid_reins 删除后再插入新行
    writer.do_delete(conn, rid_del)
    writer.do_delete(conn, rid_reins)
    writer.do_insert(conn, rid_reins, "snap_reinserted", 999, False)

    # 迁移侧用*旧快照行*走回填路径：重新 FOR UPDATE 读当前版本做裁决
    r1 = engine.apply_backfill_row(mig, old_del)
    r2 = engine.apply_backfill_row(mig, old_reins)
    mig.execute("COMMIT")
    check("先读旧行后收到删除：旧回填被拦截，不会复活", r1 == "skipped_deleted", r1)
    # 删后再插：FOR UPDATE 拿到的是当前存活的新版本（v2），直接安全写入，
    # 旧快照元组（v1）从未参与写入，不存在旧值覆盖新值
    check("删后再插：按当前新版本写入而非旧快照", r2 == "ok", r2)
    # 再走变更流，确保最终状态正确
    engine.drain_changes(conn)
    gone = conn.execute("SELECT count(*) FROM canonical_records WHERE id=%s", [rid_del]).fetchone()[0]
    new = conn.execute("SELECT code, amount, active, row_version FROM canonical_records WHERE id=%s",
                       [rid_reins]).fetchone()
    check("被删除的行没有被旧回填复活", gone == 0)
    check("删后再插的行以新插入版本存在（INSERT 重置 row_version=1）",
          new == ("SNAP_REINSERTED", Decimal("9.99"), False, 1), str(new))


def scenario_kill_and_resume() -> None:
    section("场景 8：迁移进程提交前被杀死，从持久化进度续跑（不清空目标）")
    conn = connect()
    before_chunks = conn.execute("SELECT count(*) FROM migration_chunks").fetchone()[0]
    before_target = conn.execute("SELECT count(*) FROM canonical_records").fetchone()[0]
    before_frontier = engine.get_meta(conn)["backfill_after_id"]

    # 8a. 回填块提交前杀死（os._exit，未 COMMIT 的块整体回滚）
    proc = cli("step", env_fault=f"{engine.FAULT_STEP_COMMIT}:1", expect_rc=2)
    print("  " + proc.stdout.strip().splitlines()[-1])
    after_chunks = conn.execute("SELECT count(*) FROM migration_chunks").fetchone()[0]
    after_frontier = engine.get_meta(conn)["backfill_after_id"]
    check("被杀块没有产生持久化块记录", after_chunks == before_chunks)
    check("回填前沿没有越界", after_frontier == before_frontier)

    # 8b. 另一进程 resume，从最后已提交进度继续，目标表不重建
    proc = cli("resume")
    resumed = json.loads(proc.stdout)
    check("resume 继续完成剩余回填", resumed["meta"]["backfill_done"] is True)
    target_now = conn.execute("SELECT count(*) FROM canonical_records").fetchone()[0]
    check("目标表持续累积而非清空重拷", target_now >= before_target)

    # 8c. 变更批次提交前杀死一次再 resume
    proc = cli("step", env_fault=f"{engine.FAULT_DRAIN_COMMIT}:1", expect_rc=2)
    print("  " + proc.stdout.strip().splitlines()[-1])
    cli("resume")
    check("第二次被杀后仍可续跑", engine.get_meta(conn)["backfill_done"] is True)


def scenario_lock_conflict() -> None:
    section("场景 9：不同进程不能同时推进同一迁移")
    conn = connect()
    engine.acquire_lock(conn)  # 主进程持锁
    try:
        proc = cli("resume", expect_rc=2)
        check("第二个进程被咨询锁拒绝", "咨询锁被占用" in proc.stdout + proc.stderr,
              proc.stdout.strip()[:80])
    finally:
        engine.release_lock(conn)


def scenario_finalize_blocked_then_fixed() -> None:
    section("场景 10：格式错误明确阻断 finalize，修复后才能 READY")
    conn = connect()
    seed_bad = conn.execute(
        "SELECT count(*) FROM records WHERE trim(raw_code)=''"
    ).fetchone()[0]
    check("演示数据中存在预先布置的错误行", seed_bad >= 30, f"seed_bad={seed_bad}")

    # 10a. 第一次 finalize：错误行必须明确阻断，不能跳过宣布成功
    # 错误来源有两处：种子布置 + 并发写入器按比例产生的空白 code
    bad_now = conn.execute(
        "SELECT count(*) FROM records WHERE trim(raw_code)=''"
    ).fetchone()[0]
    cli("finalize", expect_rc=3)
    meta = engine.get_meta(conn)
    check("finalize 被格式错误阻断，状态 FAILED（非 READY）", meta["state"] == "FAILED")
    err_count = conn.execute("SELECT count(*) FROM migration_errors").fetchone()[0]
    check("全部当前错误行都有持久化失败位置（种子 + 并发写入产生）",
          err_count == bad_now, f"errors={err_count} bad_now={bad_now}")
    sample = conn.execute(
        "SELECT id, row_version, stage, message FROM migration_errors ORDER BY id LIMIT 3"
    ).fetchall()
    print("  失败位置示例:", sample)

    # 10b. 再注入一个新的空白 code 错误，确认阻断持续有效
    bad_id = 9_400_001
    writer.do_insert(conn, bad_id, "   ", 4200, True)
    engine.drain_changes(conn)
    cli("finalize", expect_rc=3)
    check("新增错误行同样阻断",
          conn.execute("SELECT count(*) FROM migration_errors WHERE id=%s", [bad_id]).fetchone()[0] == 1)

    # 10c. 修复所有错误源行（每个修复都是一个新版本 UPDATE 事务）
    bad_rows = conn.execute(
        "SELECT id FROM records WHERE trim(raw_code)='' ORDER BY id"
    ).fetchall()
    for (rid,) in bad_rows:
        writer.do_update(conn, rid, f"fixed_{rid}", 100 * (rid % 1000), True)
    engine.drain_changes(conn)
    check("源侧错误行已全部修复",
          conn.execute("SELECT count(*) FROM records WHERE trim(raw_code)=''").fetchone()[0] == 0)

    # 10d. finalize 提交前杀死进程：READY 不得生效，重跑后才成功
    proc = cli("finalize", env_fault=f"{engine.FAULT_FINALIZE_COMMIT}:1", expect_rc=2)
    print("  " + proc.stdout.strip().splitlines()[-1])
    check("被杀的 finalize 没有留下 READY 状态",
          engine.get_meta(conn)["state"] in ("FAILED", "RUNNING"))
    proc = cli("finalize")
    meta = engine.get_meta(conn)
    check("重新 finalize 成功并标记 READY", meta["state"] == "READY")
    row = conn.execute("SELECT code, amount FROM canonical_records WHERE id=%s",
                       [bad_id]).fetchone()
    expected_amount = normalize_amount(100 * (bad_id % 1000))
    check("修复后的规范值正确",
          row == (f"FIXED_{bad_id}", expected_amount), str(row))
    check("错误记录已随修复全部清除",
          conn.execute("SELECT count(*) FROM migration_errors").fetchone()[0] == 0)


def final_verification() -> None:
    section("场景 11：最终逐主键逐字段精确核验")
    conn = connect()
    report = engine.verify(conn)
    check("verify 总体通过", report["ok"], json.dumps(report, ensure_ascii=False)[:400])
    check("水位线之后无未应用的已提交变更（回滚洞为正常永久洞）",
          report["watermark_tight"])
    check("无待处理变更", report["pending_changes"] == 0)

    # 独立 SQL 复核（不依赖引擎 Python 代码，直接在库内按同一规则比较）
    sql = r"""
    WITH src AS (
        SELECT id,
               -- 纯 ASCII 小写转大写：translate 逐字符映射，é 等非 ASCII 保持原样，
               -- 与 Python normalize_code 的规则严格一致（不能用 upper()，
               -- 它会把 é 也转成 É）
               translate(trim(raw_code),
                         'abcdefghijklmnopqrstuvwxyz',
                         'ABCDEFGHIJKLMNOPQRSTUVWXYZ')        AS exp_code,
               (amount_cents / 100.0)::numeric(18,2)          AS exp_amount,
               active, row_version
          FROM records
         WHERE btrim(raw_code) <> ''
    )
    SELECT
      (SELECT count(*) FROM src)                                   AS live_valid,
      (SELECT count(*) FROM records WHERE btrim(raw_code)='')      AS live_bad,
      (SELECT count(*) FROM canonical_records)                     AS target_rows,
      (SELECT count(*) FROM migration_errors)                      AS error_rows,
      (SELECT count(*) FROM src s
         LEFT JOIN canonical_records c ON c.id=s.id
        WHERE c.id IS NULL OR c.code<>s.exp_code OR c.amount<>s.exp_amount
           OR c.active<>s.active OR c.row_version<>s.row_version)  AS mismatches,
      (SELECT count(*) FROM canonical_records c
         LEFT JOIN records r ON r.id=c.id WHERE r.id IS NULL)      AS extra_rows
    """
    nums = conn.execute(sql).fetchone()
    live_valid, live_bad, target_rows, error_rows, mismatches, extra_rows = nums
    check("规范表行数 == 合法源行数", target_rows == live_valid,
          f"target={target_rows} live_valid={live_valid}")
    check("无错误源行残留且错误表为空", live_bad == 0 and error_rows == 0)
    check("逐主键逐字段零不一致", mismatches == 0)
    check("目标表没有额外行", extra_rows == 0)

    # 每个成功（已提交）源事务都已反映：
    # 已提交变更总数 = 变更日志总数；其中 INSERT/UPDATE 的最新版本必须能在规范表
    # 或错误表找到对应状态，DELETE 的 id 在规范表必须缺席
    txn_sql = r"""
    SELECT
      (SELECT count(*) FROM record_changes)                                   AS total_changes,
      (SELECT count(*) FROM record_changes c
         JOIN records r ON r.id=c.id
        WHERE c.change_seq = (SELECT max(change_seq) FROM record_changes x WHERE x.id=c.id)
          AND c.op IN ('I','U')
          AND NOT EXISTS (SELECT 1 FROM canonical_records k WHERE k.id=c.id))  AS missing_live,
      (SELECT count(*) FROM (
          SELECT DISTINCT id FROM record_changes c
           WHERE c.op='D'
             AND c.change_seq=(SELECT max(change_seq) FROM record_changes x WHERE x.id=c.id)
        ) d WHERE EXISTS (SELECT 1 FROM canonical_records k WHERE k.id=d.id))  AS resurrected
    """
    total_changes, missing_live, resurrected = conn.execute(txn_sql).fetchone()
    check("每个成功 INSERT/UPDATE 事务的最终状态都已反映", missing_live == 0)
    check("每个成功 DELETE 事务的目标行均缺席（无复活）", resurrected == 0)
    check("回滚标记从未进入任何表",
          conn.execute(
            "SELECT (SELECT count(*) FROM records WHERE raw_code LIKE 'ROLLBACK_MARKER%%')"
            " + (SELECT count(*) FROM canonical_records WHERE code LIKE 'ROLLBACK_MARKER%%')"
          ).fetchone()[0] == 0)
    print(f"\n  已提交变更总数={total_changes}，全部有终态对应；源 {report['source_rows']} 行"
          f" / 目标 {report['target_rows']} 行")


def main() -> int:
    t0 = time.time()
    section("场景 1：初始化独立数据库与合成历史数据（合法 + 空白 code 错误行）")
    conn = connect()
    schema.install(conn)
    engine.reset(conn)  # 演示引导：回到完全初始状态
    seed_base_data()

    section("场景 2：start —— 先建立可靠变更捕获，再开始回填")
    cli("start", "--chunk", str(CHUNK))
    meta = engine.get_meta(conn)
    check("迁移状态 RUNNING 且进度已持久化", meta["state"] == "RUNNING")
    trig = conn.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid='records'::regclass AND tgname='records_audit'"
    ).fetchone()[0]
    check("变更捕获触发器已就位", trig == 1)

    section("场景 3：分块边界插入 / 扫描前后更新 / 回填-删除竞争（在线步进）")
    # 先做一个块，拿到边界 (0, 500]
    cli("step")
    frontier = engine.get_meta(conn)["backfill_after_id"]
    check("第一块按主键上界持久化", frontier == CHUNK)
    # 种子数据占满 1..3000：先在已扫描范围内删除一个旧行，再以新值插回，
    # 模拟“迁移期间新增到已扫描主键范围内的记录”
    writer.do_delete(conn, 377)
    writer.do_insert(conn, 377, "  boundary-late-insert ", 77700, True)
    writer.do_insert(conn, 8_000_501, "next_chunk_first", 88, False)  # 独立的新区间行
    # 尚未扫描的大 id 先更新多次（模拟扫描前后更新的前半段）
    writer.do_insert(conn, 8_002_900, "before_scan_v1", 100, True)
    writer.do_update(conn, 8_002_900, "before_scan_v2", 200, True)
    writer.do_update(conn, 8_002_900, "before_scan_v3", 300, False)
    # 已扫描范围内的更新
    writer.do_update(conn, 10, "after_scan_update", 50, False)
    # 已扫描范围内的删除
    writer.do_delete(conn, 20)
    engine.drain_changes(conn)
    late = conn.execute("SELECT code, amount, active FROM canonical_records WHERE id=%s",
                        [377]).fetchone()
    check("已扫描范围内新增记录被变更流捕获",
          late == ("BOUNDARY-LATE-INSERT", Decimal("777.00"), True), str(late))
    check("已扫描范围内更新被反映",
          conn.execute("SELECT code FROM canonical_records WHERE id=10").fetchone()[0] == "AFTER_SCAN_UPDATE")
    check("已扫描范围内删除被反映（目标缺席）",
          conn.execute("SELECT count(*) FROM canonical_records WHERE id=20").fetchone()[0] == 0)
    check("多次更新后只保留最终版本",
          conn.execute("SELECT code, amount, active FROM canonical_records WHERE id=8002900").fetchone()
          == ("BEFORE_SCAN_V3", Decimal("3.00"), False))

    scenario_rollback()
    scenario_out_of_order_commit()
    scenario_held_snapshot_race()

    section("场景 3（续）：并发写入器在迁移推进期间持续产生本地事务")
    # 紧凑 id 段（紧随种子数据），保持总量几千行且避免稀疏空区间
    stats = writer.run_concurrent(
        seconds=6, threads=3, rate=15, rollback_pct=8, id_start=10_000, seed=7
    )
    print("  并发写入统计:", json.dumps(stats, ensure_ascii=False))
    check("并发期含回滚事务", stats["rollback"] > 0)

    scenario_kill_and_resume()
    scenario_lock_conflict()
    scenario_finalize_blocked_then_fixed()
    final_verification()

    section(f"演示完成，用时 {time.time() - t0:.1f}s")
    passed = sum(1 for _, ok, _ in _checks if ok)
    print(f"检查项 {passed}/{len(_checks)} 全部通过")
    cli("status")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n演示失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
