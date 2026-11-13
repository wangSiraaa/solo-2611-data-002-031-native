"""一键完整演练编排。

时间线(全部以命名 restore point 为准, 不用墙钟):
  心跳写入 ─ 基础备份 b1 ─ 标记 M1 ─ rp_early ─ 基础备份 b2
          ─ 标记 M2(含更新/删除) ─ rp_late ─ 标记 M3 ─ 误操作(mishap)
成功恢复:
  R1 -> rp_early : 目标之前没有误操作, 暂停态核验
  R2 -> rp_late  : 含目标前更新/删除、排除目标后事务, 提升为新时间线核验
失败制造:
  F-gap        : 缺 WAL 段(预检拒绝 + 强制启动后真实等待/超时)
  F-identity   : 拿错数据库的基础备份(身份预检拒绝)
  F-timeline   : 错误时间线(时间线预检拒绝)
  F-truncated  : 截断归档(完整性预检拒绝 + 强制启动后数据库重放失败)
"""
from __future__ import annotations

from pathlib import Path

from . import faildemo
from .archive import build_archive_set
from .config import DrillConfig
from .instance import (DB_NAME, HeartbeatWriter, create_business_db,
                       create_restore_point, init_source, insert_witness,
                       run_mishap, start_source, take_base_backup)
from .recovery import (monitor, prepare_restore_dir,
                       preflight, promote, start_restore, stop_restore)
from .report import build_report
from .state import (PHASE_FAILED, RESULT_FAILED,
                    RESULT_TARGET_REACHED, RESULT_WAITING_ARCHIVE,
                    RestoreState, Store)
from .util import DrillError, now_iso, read_jsonl, wait_ready
from .verify import verify_restored


def _log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def run_full_demo(cfg: DrillConfig, *, quick: bool = False,
                  rebuild: bool = False) -> Store:
    cfg.ensure_dirs()
    # 已存在完整演练时默认拒绝覆盖; --rebuild 显式清理本演练全部资源后重建
    if (cfg.source_data / "PG_VERSION").exists():
        if not rebuild:
            raise DrillError(
                f"演练根目录已存在源实例: {cfg.source_data}。"
                "继续观察请用 status/restore-observe; 显式重建请加 --rebuild "
                "(等价于先执行 cleanup --yes --include-source --include-archive)")
        from .cleanup import cleanup
        existing = Store(cfg)
        _log("--rebuild: 清理上一轮本演练全部资源")
        cleanup(existing, include_source=True, include_archive=True, yes=True)
    store = Store(cfg)
    tick = 0.15 if quick else cfg.tick_seconds
    workload = 8.0 if quick else cfg.workload_seconds
    gap_budget = 8 if quick else cfg.gap_wait_budget
    rec_budget = 60 if quick else cfg.recovery_wait_budget

    # ---------------- 源实例 ----------------
    _log("== 阶段1: 初始化并启动隔离源实例 ==")
    if not (cfg.source_data / "PG_VERSION").exists():
        init_source(store)
    start_source(store)
    wait_ready(cfg, cfg.source_socket, cfg.source_port)
    create_business_db(store)
    store.set_phase("SOURCE_RUNNING")
    _log(f"源实例已启动 port={cfg.source_port} "
         f"sysid={store.state.source['system_identifier']}")

    event_file = cfg.tmp_dir / "heartbeat-events.jsonl"
    writer = HeartbeatWriter(cfg, event_file, tick_seconds=tick)
    writer.__enter__()
    try:
        # ---------------- 工作负载 + 见证 + 备份 ----------------
        _log("== 阶段2: 持续写入 + 事务见证 + 物理基础备份 ==")
        for _ in range(6):
            writer.beat()
        b1 = take_base_backup(store, "b1-early", "早期基础备份(心跳开始后)")
        _log(f"基础备份 b1 完成 start={b1['start_lsn']} "
             f"自带WAL={len(b1['backup_wal_segments'])}段")

        for _ in range(6):
            writer.beat()
        m1 = insert_witness(cfg, "MARKER-M1", {"phase": "before-rp-early",
                                              "meaning": "早期见证标记"},
                            cfg.source_socket, cfg.source_port)
        store.add_event("witness", **m1)
        rp_early = create_restore_point(store, "rp_early",
                                        "早期恢复点(误操作之前)")
        _log(f"restore point rp_early @ {rp_early['lsn']}")

        for _ in range(6):
            writer.beat()
        b2 = take_base_backup(store, "b2-mid", "中期基础备份(rp_early 之后)")
        _log(f"基础备份 b2 完成 start={b2['start_lsn']}")

        # 制造明确的更新与删除(目标前的正常业务操作)
        from .util import psql
        dml = psql(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
                   dbname=DB_NAME, sql="""
BEGIN;
UPDATE pitrdrill_parent SET version = 42, payload = 'pre-target-update',
       updated_at = clock_timestamp() WHERE id IN (2,4,6);
DELETE FROM pitrdrill_child WHERE parent_id = 8
  AND id IN (SELECT id FROM pitrdrill_child WHERE parent_id=8 ORDER BY id LIMIT 3);
INSERT INTO pitrdrill_event_log(event_seq, kind, marker, detail)
VALUES (nextval('pitrdrill_event_seq'), 'pre_target_dml', NULL,
        '{"update_parents":[2,4,6],"delete_children_parent8":3}'::jsonb);
COMMIT;
""")
        if not dml.ok:
            raise DrillError(f"目标前 DML 失败: {dml.stderr}")
        store.add_event("pre_target_dml",
                        updated_parents=[2, 4, 6], deleted_children_parent=8)

        for _ in range(6):
            writer.beat()
        m2 = insert_witness(cfg, "MARKER-M2",
                            {"phase": "before-rp-late",
                             "meaning": "含更新删除后的见证标记"},
                            cfg.source_socket, cfg.source_port)
        store.add_event("witness", **m2)
        rp_late = create_restore_point(store, "rp_late",
                                       "晚期恢复点(误操作之前, 含正常更新删除)")
        _log(f"restore point rp_late @ {rp_late['lsn']}")

        for _ in range(4):
            writer.beat()
        m3 = insert_witness(cfg, "MARKER-M3",
                            {"phase": "after-rp-late",
                             "meaning": "目标之后的标记(恢复应排除)"},
                            cfg.source_socket, cfg.source_port)
        store.add_event("witness", **m3)

        # 让 rp_late 之后再产生若干 WAL 段, 然后执行误操作
        writer.run_for(max(2.0, workload * 0.3))
        mishap = run_mishap(store)
        _log(f"误操作已执行并归档 LSN -> {mishap['after_lsn']}")
        # 误操作删除了 parent 3/7, 心跳写入器此后只能引用存活父表
        writer.set_valid_parents([1, 2, 4, 5, 6, 8, 9, 10])
        writer.run_for(max(1.5, workload * 0.15))
    finally:
        writer.__exit__(None, None, None)

    beats = read_jsonl(event_file)
    store.add_event("workload_summary", ticks=len(beats),
                    last_lsn=beats[-1]["lsn"] if beats else None)
    store.save()

    # 完整归档集合(成功恢复使用)
    full_set = build_archive_set(store, "set-full")
    _log(f"完整归档集合 set-full: {len(full_set['files'])} 段")

    mishap_rec = next(e for e in store.state.events if e.get("kind") == "mishap")

    # ---------------- 成功恢复 R1: rp_early, 暂停态 ----------------
    _log("== 阶段3: 成功恢复 R1 -> rp_early(误操作之前) ==")
    r1 = _do_restore(store, "R1-early", "b1-early", "rp_early",
                     "set-full", rec_budget)
    v1 = verify_restored(
        store, "R1-early",
        expect_markers=["MARKER-M1"],
        absent_markers=["MARKER-M2", "MARKER-M3"],
        mishap=mishap_rec)
    _log(f"R1 核验 {v1['passed']}/{v1['total']} 通过; "
         f"结果={store.get_restore('R1-early').result}")
    stop_restore(store, store.get_restore("R1-early"))

    # ---------------- 成功恢复 R2: rp_late, 提升 ----------------
    _log("== 阶段4: 成功恢复 R2 -> rp_late 并提升为新时间线 ==")
    r2 = _do_restore(store, "R2-late", "b2-mid", "rp_late",
                     "set-full", rec_budget)
    prom = promote(store, store.get_restore("R2-late"))
    hist = store.get_restore("R2-late").target_evidence.get(
        "promoted", {}).get("history_files", [])
    _log(f"R2 已提升, 新时间线={prom['timeline']} "
         f"(源时间线=1, history={hist}, 严格区分)")
    v2 = verify_restored(
        store, "R2-late",
        expect_markers=["MARKER-M1", "MARKER-M2"],
        absent_markers=["MARKER-M3"],
        mishap=mishap_rec, require_promoted=True)
    _log(f"R2 核验 {v2['passed']}/{v2['total']} 通过")

    # ---------------- 失败场景 ----------------
    _log("== 阶段5: 故障注入与失败确认 ==")
    _failure_gap(store, gap_budget, rec_budget)
    _failure_identity(store)
    _failure_timeline(store)
    _failure_truncated(store, rec_budget)

    # ---------------- 报告 ----------------
    _log("== 阶段6: 生成报告 ==")
    report = build_report(store)
    _log(f"报告: {report}")
    _log("完整演练结束。源实例仍在运行(未被替换), R2 提升实例保留可供核验。")
    return store


# ---------------------------------------------------------------- 恢复助手
def _do_restore(store: Store, name: str, backup: str, target: str,
                set_name: str, rec_budget: int, *,
                force_start: bool = False,
                preflight_budget: int | None = None) -> RestoreState:
    cfg = store.cfg
    rs = RestoreState(
        name=name, backup=backup, target=target, archive_set=set_name,
        data_dir=str(cfg.restore_data(name)),
        socket_dir=str(cfg.restore_socket(name)),
        log_path=str(cfg.restore_log(name)), port=0,
        created_at=now_iso())
    store.upsert_restore(rs)
    pf = preflight(store, backup, target, set_name,
                   wait_budget=preflight_budget)
    rs = store.get_restore(name)
    rs.preflight = pf
    store.upsert_restore(rs)
    if not pf["passed"] and not force_start:
        rs.phase = PHASE_FAILED
        rs.result = RESULT_FAILED
        rs.failure_reasons.extend(pf["hard_errors"])
        store.upsert_restore(rs)
        return rs
    prepare_restore_dir(store, rs)
    start_restore(store, rs)
    return monitor(store, rs, budget=rec_budget)


# ---------------------------------------------------------------- 各失败场景
def _failure_gap(store: Store, gap_budget: int, rec_budget: int) -> None:
    cfg = store.cfg
    _log("  [F-gap] 缺 WAL 段")
    rec = faildemo.make_gap_set(store, "set-gap", "b1-early", "rp_early")
    _log(f"  缺口段: {rec['removed_segment']}")
    # 5a. 预检层面(短预算)应明确报 ARCHIVE_GAP
    rs_pf = _do_restore(store, "F-gap-preflight", "b1-early", "rp_early",
                        "set-gap", rec_budget, preflight_budget=5)
    assert rs_pf.result == RESULT_FAILED and \
        any("ARCHIVE_GAP" in x for x in rs_pf.failure_reasons), \
        "缺口预检未按预期失败"
    _log("  预检已识别 ARCHIVE_GAP 并拒绝恢复")

    # 5b. 强制启动: 数据库真实重放, 缺段处 PG 判定无法到达目标而 FATAL
    rs = RestoreState(
        name="F-gap-running", backup="b1-early", target="rp_early",
        archive_set="set-gap",
        data_dir=str(cfg.restore_data("F-gap-running")),
        socket_dir=str(cfg.restore_socket("F-gap-running")),
        log_path=str(cfg.restore_log("F-gap-running")), port=0,
        created_at=now_iso())
    store.upsert_restore(rs)
    prepare_restore_dir(store, rs)
    start_restore(store, rs)
    rs = monitor(store, store.get_restore("F-gap-running"), budget=gap_budget)
    store.state.failure_demos["set-gap"]["forced_start_result"] = {
        "result": rs.result, "reasons": rs.failure_reasons,
        "log_tail": (Path(rs.log_path).read_text().splitlines()[-6:]
                     if Path(rs.log_path).exists() else [])}
    _log(f"  缺段强制启动结果={rs.result} (FATAL: 未到达配置目标, 区别于无限等待)")
    # 只停止不删除: 保留 FAILED 状态与 restore.log 作为报告证据
    try:
        stop_restore(store, store.get_restore("F-gap-running"))
    except DrillError:
        pass

    # 5c. 归档迟到(门控): 先观察到受限等待超时, 再补齐归档后真实到达
    _failure_gated_wait(store, gap_budget, rec_budget, rec["removed_segment"])


def _failure_gated_wait(store: Store, gap_budget: int, rec_budget: int,
                        missing_segment: str) -> None:
    """门控 restore_command 演示 WAITING_ARCHIVE 与超时诊断, 再放行恢复到目标。"""
    import os
    import shutil as _sh
    cfg = store.cfg
    set_name = "set-waiting"
    # 与缺口集合相同: 先缺目标之前的一段
    faildemo.make_gap_set(store, set_name, "b1-early", "rp_early")
    staging = cfg.tmp_dir / "waiting-staging"
    staging.mkdir(parents=True, exist_ok=True)
    _sh.copy2(cfg.archive_dir / missing_segment, staging / missing_segment)
    gate = cfg.tmp_dir / f"{set_name}.gate"
    if gate.exists():
        gate.unlink()

    def make_rs(suffix: str) -> RestoreState:
        name = f"F-wait-{suffix}"
        r = RestoreState(
            name=name, backup="b1-early", target="rp_early",
            archive_set=set_name, gated=True, gate_file=str(gate),
            data_dir=str(cfg.restore_data(name)),
            socket_dir=str(cfg.restore_socket(name)),
            log_path=str(cfg.restore_log(name)), port=0,
            created_at=now_iso())
        store.upsert_restore(r)
        return r

    # 5c-i: 门关闭 -> 预算内始终等待, 超时保留诊断(不谎报成功)
    r1 = make_rs("timeout")
    prepare_restore_dir(store, r1)
    start_restore(store, r1)
    r1 = monitor(store, store.get_restore(r1.name), budget=gap_budget)
    assert r1.result == RESULT_WAITING_ARCHIVE, \
        f"门控场景应处于等待态, 实际 {r1.result}"
    _log(f"  归档迟到: 受限等待 {gap_budget}s 后保留诊断, 状态=WAITING_ARCHIVE")
    # 只停止不删除: 保留 WAITING_ARCHIVE 状态与 restore.log 作为报告证据
    try:
        stop_restore(store, store.get_restore(r1.name))
    except DrillError:
        pass
    store.state.failure_demos[set_name] = {
        "demo": "gated_archive_wait", "gated_segment": missing_segment,
        "timeout_restore": r1.name,
        "timeout_budget_seconds": gap_budget,
        "timeout_result": r1.result, "timeout_reasons": r1.failure_reasons}

    # 5c-ii: 明确补齐归档并开门 -> 同一归档集合可继续增长, 恢复真实到达目标
    _sh.copy2(staging / missing_segment, cfg.set_dir(set_name) / missing_segment)
    from .util import robust_write_text
    robust_write_text(gate, "released\n")
    r2 = make_rs("released")
    prepare_restore_dir(store, r2)
    start_restore(store, r2)
    r2 = monitor(store, store.get_restore(r2.name), budget=rec_budget)
    assert r2.result == RESULT_TARGET_REACHED, \
        f"放行后应到达目标, 实际 {r2.result}/{r2.failure_reasons}"
    _log("  归档补齐并放行后, WAL 重放真实到达 rp_early: TARGET_REACHED")
    store.state.failure_demos[set_name]["released_result"] = r2.result
    store.save()


def _failure_identity(store: Store) -> None:
    _log("  [F-identity] 错备份身份(外来 pg_control)")
    faildemo.make_wrong_identity_backup(store, "b1-early", "b-foreign-identity")
    build_archive_set(store, "set-full-copy",
                      include=store.state.archive_sets["set-full"]["files"])
    rs = _do_restore(store, "F-identity", "b-foreign-identity",
                     "rp_early", "set-full-copy", 30, preflight_budget=2)
    assert rs.result == RESULT_FAILED and \
        any("WRONG_DATABASE_IDENTITY" in x for x in rs.failure_reasons)
    _log("  预检拒绝: WRONG_DATABASE_IDENTITY(拿错数据库的基础备份)")


def _failure_timeline(store: Store) -> None:
    _log("  [F-timeline] 错误时间线")
    faildemo.make_wrong_timeline_set(store, "set-tli2")
    rs = _do_restore(store, "F-timeline", "b1-early", "rp_early",
                     "set-tli2", 30, preflight_budget=2)
    assert rs.result == RESULT_FAILED and (
        any("WRONG_TIMELINE" in x for x in rs.failure_reasons)
        or any("时间线" in x for x in rs.failure_reasons))
    _log("  预检拒绝: WRONG_TIMELINE(错误时间线)")


def _failure_truncated(store: Store, rec_budget: int) -> None:
    _log("  [F-truncated] 截断归档")
    rec = faildemo.make_truncated_set(store, "set-trunc", "b1-early", "rp_early")
    _log(f"  截断段 {rec['segment']}: {rec['size_before']} -> {rec['size_after']}")
    # 预检层面
    rs_pf = _do_restore(store, "F-trunc-preflight", "b1-early", "rp_early",
                        "set-trunc", rec_budget, preflight_budget=2)
    assert rs_pf.result == RESULT_FAILED and \
        any("TRUNCATED" in x or "sha256" in x or "大小异常" in x
            for x in rs_pf.failure_reasons)
    _log("  预检拒绝: ARCHIVE_TRUNCATED_OR_CORRUPT(大小/sha256/pg_waldump)")

    # 强制启动: 数据库重放遇到截断段真实失败
    rs = _do_restore(store, "F-trunc-running", "b1-early", "rp_early",
                     "set-trunc", rec_budget, force_start=True,
                     preflight_budget=2)
    if rs.result == RESULT_FAILED:
        _log(f"  数据库重放被截断段阻断: {rs.failure_reasons[-1][:160]}")
    else:
        _log(f"  截断场景结果={rs.result}")
    # 只停止不删除: 保留 FAILED 状态与 restore.log 作为报告证据
    try:
        stop_restore(store, store.get_restore("F-trunc-running"))
    except DrillError:
        pass
