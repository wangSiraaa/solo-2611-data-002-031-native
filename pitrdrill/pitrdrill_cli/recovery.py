"""隔离时间点恢复: 预检、恢复目录准备、WAL 重放监控、提升。

恢复目录与源实例完全分开(独立 PGDATA / 端口 / socket),
恢复确实由 PostgreSQL 重放 WAL 完成; 本模块不做任何 SQL 导入或数据搬运。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .archive import (archive_index_map, gap_analysis, set_segment_names,
                      timeline_check, verify_segment_bytes,
                      waldump_timeline_evidence)
from . import TASK_TAG
from .config import DrillConfig, find_free_port
from .instance import write_restore_fetch_wrapper
from .state import (PHASE_FAILED, PHASE_PROMOTED,
                    PHASE_RESTORE_REPLAYING, PHASE_RESTORE_WAITING_ARCHIVE,
                    PHASE_TARGET_REACHED, RESULT_FAILED,
                    RESULT_PROMOTED, RESULT_TARGET_REACHED,
                    RESULT_WAITING_ARCHIVE, RestoreState, Store)
from .util import (DrillError, enforce_pgdata_permissions, now_iso,
                   parse_controldata, parse_segment_name, pid_alive,
                   psql, psql_value, read_pidfile, robust_copytree,
                   robust_rmtree, robust_write_text, run, segment_range,
                   tail_text, wait_deadline, wait_ready)


def _enforce_pgdata_permissions(data_dir) -> None:
    enforce_pgdata_permissions(Path(data_dir))

# 日志中的关键证据模式
# PG16 实际措辞: "pausing at the end of recovery"(旧版本为 "recovery has paused")
PAT_PAUSE_TARGET = re.compile(
    r"pausing at the end of recovery|recovery has paused")
PAT_RESTORE_POINT = re.compile(
    r"recovery stopping at restore point \"(?P<name>[^\"]+)\"")
PAT_CONSISTENT = re.compile(r"consistent recovery state reached")
PAT_MISSING_WAL = re.compile(r"could not connect to|WAL segment.*is not|requested WAL segment.*has already been removed|not yet been supplied|could not restore")
PAT_FATAL = re.compile(r"FATAL:|PANIC:")
PAT_ARCHIVE_MISS = re.compile(r"\[pitrdrill-restore-fetch\] MISS (\S+)")
PAT_ARCHIVE_HIT = re.compile(r"\[pitrdrill-restore-fetch\] HIT (\S+)")
PAT_GATED = re.compile(r"\[pitrdrill-gated\] GATED (\S+)")
PAT_GATED_HIT = re.compile(r"\[pitrdrill-gated\] HIT (\S+)")
PAT_PROMOTED = re.compile(r"selected new timeline ID: (\d+)")


def _seg_order(name: str) -> tuple[int, int]:
    try:
        return parse_segment_name(name)
    except DrillError:
        return (-1, -1)


# ---------------------------------------------------------------- 预检
def preflight(store: Store, backup_name: str, target_name: str,
              set_name: str, *, wait_budget: int | None = None) -> dict:
    """恢复前提核验。返回结构化结果; 硬错误抛 DrillError。

    检查项:
      1. 基础备份身份(system identifier)与源库一致 —— 拿错数据库
      2. 备份自身完整性(pg_verifybackup)
      3. 目标 restore point 存在且时间线匹配 —— 错误时间线
      4. 所需 WAL 段 vs (归档集合 + 备份自带 pg_wal) —— 归档缺口, 可受限等待
      5. 归档文件大小/sha256/pg_waldump —— 截断/篡改
    """
    cfg = store.cfg
    budget = wait_budget if wait_budget is not None else cfg.archive_wait_budget
    if backup_name not in store.state.backups:
        raise DrillError(f"未知基础备份: {backup_name}")
    backup = store.state.backups[backup_name]
    rp = next((r for r in store.state.restore_points if r["name"] == target_name), None)
    if rp is None:
        raise DrillError(f"未知 restore point: {target_name}")

    result: dict = {
        "checked_at": now_iso(), "backup": backup_name,
        "target": target_name, "archive_set": set_name,
        "checks": {}, "problems": [], "hard_errors": [],
    }

    # 1. 身份检查
    src_id = store.state.source.get("system_identifier", "")
    bak_id = backup.get("system_identifier", "")
    id_ok = bool(src_id) and src_id == bak_id
    result["checks"]["system_identifier"] = {
        "source": src_id, "backup": bak_id, "match": id_ok}
    if not id_ok:
        result["hard_errors"].append(
            f"WRONG_DATABASE_IDENTITY: 备份 system identifier {bak_id} "
            f"!= 源库 {src_id}(拿错数据库的基础备份)")

    # 2. 备份完整性
    from .instance import verify_backup_integrity
    vb = verify_backup_integrity(store, backup_name)
    result["checks"]["pg_verifybackup"] = vb
    if vb["rc"] != 0:
        result["hard_errors"].append(f"BACKUP_CORRUPT: {vb['output'][:300]}")

    # 3. 时间线检查
    tli_problems = timeline_check(rp["timeline"], set_segment_names(cfg, set_name))
    result["checks"]["timeline"] = {
        "required": rp["timeline"], "problems": tli_problems}
    if tli_problems:
        result["hard_errors"].append(
            "WRONG_TIMELINE: 归档集合存在错误时间线段 -> "
            + "; ".join(tli_problems)[:300])
    if backup["backup_timeline"] != rp["timeline"]:
        result["hard_errors"].append(
            f"WRONG_TIMELINE: 备份起始时间线 {backup['backup_timeline']} "
            f"!= 目标时间线 {rp['timeline']}")

    # 4. 缺口分析(含备份自带 WAL, 允许受限等待新归档)
    start_lsn = backup["checkpoint_lsn"] or backup["start_lsn"]
    required = segment_range(start_lsn, rp["lsn"],
                             cfg.wal_seg_size_bytes, rp["timeline"])
    backup_wal = backup.get("backup_wal_segments", [])
    available = set_segment_names(cfg, set_name)
    gap = gap_analysis(required, available, backup_wal)

    if not gap["complete"]:
        # 显式等待新文件(归档集合可能在演练期间继续增长)
        deadline = wait_deadline(budget)
        while time.time() < deadline and not gap["complete"]:
            # 集合按硬链接建立, 等待时重新从归档目录补链缺失段
            _grow_set_from_archive(cfg, set_name)
            available = set_segment_names(cfg, set_name)
            gap = gap_analysis(required, available, backup_wal)
            if gap["complete"]:
                break
            time.sleep(0.5)
    result["checks"]["gap"] = gap
    result["checks"]["gap_wait_budget_seconds"] = budget
    if not gap["complete"]:
        result["hard_errors"].append(
            "ARCHIVE_GAP: 目标之前仍存在归档缺口(等待 "
            f"{budget}s 后): {', '.join(gap['missing'])}")

    # 5. 截断/篡改检查(集合内每个段)
    idx = archive_index_map(cfg)
    seg_problems: list[str] = []
    seg_details = []
    for name in set_segment_names(cfg, set_name):
        path = cfg.set_dir(set_name) / name
        expected = idx.get(name, {})
        probs = verify_segment_bytes(
            cfg, path,
            expected_size=expected.get("size", cfg.wal_seg_size_bytes),
            expected_sha256=expected.get("sha256"))
        if probs:
            seg_problems.extend(probs)
        seg_details.append({"file": name, "ok": not probs,
                            "evidence": waldump_timeline_evidence(cfg, path)[:160]})
    result["checks"]["segment_integrity"] = {
        "problems": seg_problems, "details": seg_details[:8]}
    if seg_problems:
        result["hard_errors"].append(
            "ARCHIVE_TRUNCATED_OR_CORRUPT: " + "; ".join(seg_problems))

    result["passed"] = not result["hard_errors"]
    return result


def _grow_set_from_archive(cfg: DrillConfig, set_name: str) -> None:
    """等待期间把归档目录中新出现的段补链进集合(集合可继续增长)。

    尊重集合清单的 excluded 列表与已存在文件:
    缺口/截断等故障集合不得被自动"治愈"。
    """
    from .archive import hardlink_or_copy
    sdir = cfg.set_dir(set_name)
    if not sdir.exists():
        return
    manifest = sdir / "set-manifest.json"
    excluded: set[str] = set()
    if manifest.exists():
        try:
            excluded = set(json.loads(manifest.read_text()).get("excluded", []))
        except (json.JSONDecodeError, OSError):
            excluded = set()
    for src in cfg.archive_dir.iterdir():
        if len(src.name) != 24 or src.name in excluded:
            continue
        dst = sdir / src.name
        # 已存在文件(可能被故意截断)绝不覆盖, 只补新文件
        if not dst.exists():
            try:
                hardlink_or_copy(src, dst)
            except OSError:
                pass


# ---------------------------------------------------------------- 准备恢复目录
def prepare_restore_dir(store: Store, rs: RestoreState) -> None:
    cfg = store.cfg
    backup = store.state.backups[rs.backup]
    rdir = cfg.restore_dir(rs.name)
    data = cfg.restore_data(rs.name)
    rdir.mkdir(parents=True, exist_ok=True)
    cfg.restore_socket(rs.name).mkdir(parents=True, exist_ok=True)

    if data.exists():
        # 显式重建由调用方决定; 这里不静默覆盖
        raise DrillError(f"恢复数据目录已存在, 如需显式重建请先 drop: {data}")
    robust_copytree(Path(backup["path"]), data)
    # pg_basebackup 产物目录可能为 0755, robust_copytree 已收敛为 0700
    _enforce_pgdata_permissions(data)
    # 复制来的备份不得携带运行态 pid 文件
    pidf = data / "postmaster.pid"
    if pidf.exists():
        pidf.unlink()

    robust_write_text(cfg.marker_path("restore", rs.name),
                      f"task={TASK_TAG}\nkind=restore\nname={rs.name}\n"
                      f"created_at={now_iso()}\nbackup={rs.backup}\n"
                      f"target={rs.target}\narchive_set={rs.archive_set}\n")

    # 恢复信号:
    #  - recovery.signal: 一次性归档恢复, WAL 取不到且未达目标 -> FATAL(缺口场景)
    #  - standby.signal:  备库模式, 会无限重试 restore_command 进入归档等待,
    #    到达 recovery_target 后按 recovery_target_action=pause 暂停(迟到场景)
    if rs.gated:
        robust_write_text(data / "standby.signal", "")
    else:
        robust_write_text(data / "recovery.signal", "")
    if rs.gated and rs.gate_file:
        from .instance import write_gated_restore_fetch_wrapper
        wrapper = write_gated_restore_fetch_wrapper(
            cfg, rs.name, cfg.set_dir(rs.archive_set),
            cfg.restore_log(rs.name), Path(rs.gate_file))
    else:
        wrapper = write_restore_fetch_wrapper(
            cfg, rs.name, cfg.set_dir(rs.archive_set), cfg.restore_log(rs.name))
    rp = next(r for r in store.state.restore_points if r["name"] == rs.target)
    port = find_free_port(cfg.restore_port_base)
    rs.port = port
    settings = f"""# pitrdrill 隔离恢复配置(自动生成)
listen_addresses = '127.0.0.1'
port = {port}
unix_socket_directories = '{cfg.restore_socket(rs.name)}'
hot_standby = on
restore_command = '{wrapper} %f %p'
recovery_target_name = '{rs.target}'
recovery_target_action = 'pause'
recovery_target_timeline = '{rp['timeline']}'
archive_mode = off
log_line_prefix = '%m [%p] %q%u@%d '
log_min_messages = info
"""
    robust_write_text(data / "postgresql.auto.conf", settings)
    robust_write_text(cfg.restore_config_file(rs.name), settings)
    rs.commands.append({"ts": now_iso(), "step": "prepare_restore_dir",
                        "restore_command": str(wrapper), "port": port})


def start_restore(store: Store, rs: RestoreState) -> None:
    cfg = store.cfg
    data = cfg.restore_data(rs.name)
    log = cfg.restore_log(rs.name)
    r = run([cfg.pgbin / "pg_ctl", "-D", data, "-l", log,
             "-w", "-t", "30", "start"], check=False)
    rs.commands.append({"ts": now_iso(), "step": "pg_ctl start",
                        "argv": r.argv, "rc": r.rc,
                        "stderr_tail": r.stderr[-400:]})
    # pg_ctl -w 在恢复模式下等到库可连接即返回; 截断等场景 startup 可能在此期间 FATAL
    if r.rc != 0 and not pid_alive(read_pidfile(data / "postmaster.pid")):
        # 不抛异常: 交给 monitor 依据进程死亡与 FATAL 日志归类为 FAILED
        rs.phase = PHASE_FAILED
        rs.result = RESULT_FAILED
        fatal = [ln for ln in tail_text(log).splitlines() if PAT_FATAL.search(ln)]
        rs.failure_reasons.append(
            "恢复实例启动阶段即退出: "
            + (fatal[-1][:260] if fatal else r.stderr.strip()[:260]))
        rs.pid = read_pidfile(data / "postmaster.pid")
        store.upsert_restore(rs)
        return
    rs.pid = read_pidfile(data / "postmaster.pid")
    rs.phase = PHASE_RESTORE_REPLAYING
    store.upsert_restore(rs)


# ---------------------------------------------------------------- 状态监控
def monitor(store: Store, rs: RestoreState, *, budget: int | None = None,
            poll: float = 1.0) -> RestoreState:
    """轮询直到: 到达目标 / 归档缺失等待(受限) / 失败。

    绝不把"进程仍在运行"或"端口已监听"当作恢复成功。
    """
    cfg = store.cfg
    data = cfg.restore_data(rs.name)
    log = cfg.restore_log(rs.name)
    budget = budget if budget is not None else cfg.recovery_wait_budget
    deadline = time.time() + budget
    miss_since: float | None = None
    last_phase = ""

    while time.time() < deadline:
        rs = store.get_restore(rs.name)
        pid = read_pidfile(data / "postmaster.pid")
        log_text = tail_text(log)

        # 启动阶段已归类失败(start_restore 检测到 startup 即退出): 直接返回
        if rs.phase == PHASE_FAILED and rs.result == RESULT_FAILED:
            if not pid_alive(pid):
                return rs

        # 明确失败: 进程死亡且未到达目标
        if not pid_alive(pid) and rs.phase != PHASE_TARGET_REACHED:
            fatal = [ln for ln in log_text.splitlines() if PAT_FATAL.search(ln)]
            rs.phase = PHASE_FAILED
            rs.result = RESULT_FAILED
            reason = "恢复进程已退出"
            if fatal:
                reason += ": " + fatal[-1][:260]
            if not any(reason in x for x in rs.failure_reasons):
                rs.failure_reasons.append(reason)
            store.upsert_restore(rs)
            return rs

        # 到达目标: 日志确认 restore point + recovery paused
        m_rp = None
        for m in PAT_RESTORE_POINT.finditer(log_text):
            m_rp = m
        paused = bool(PAT_PAUSE_TARGET.search(log_text))
        if m_rp and paused and m_rp.group("name") == rs.target:
            evidence = _collect_target_evidence(store, rs, m_rp.group(0))
            rs.phase = PHASE_TARGET_REACHED
            rs.result = RESULT_TARGET_REACHED
            rs.target_evidence = evidence
            store.upsert_restore(rs)
            return rs

        # 归档缺失等待: restore_command 连续 MISS/GATED 且无新的成功重放
        misses = PAT_ARCHIVE_MISS.findall(log_text)
        hits = PAT_ARCHIVE_HIT.findall(log_text)
        gated = PAT_GATED.findall(log_text)
        gated_hits = PAT_GATED_HIT.findall(log_text)
        all_miss = misses + gated
        all_hit = hits + gated_hits
        waiting = bool(all_miss) and (
            not all_hit or _seg_order(all_miss[-1]) >= _seg_order(all_hit[-1]))
        if waiting and pid_alive(pid):
            miss_since = miss_since or time.time()
            if rs.phase != PHASE_RESTORE_WAITING_ARCHIVE:
                rs.phase = PHASE_RESTORE_WAITING_ARCHIVE
                rs.result = RESULT_WAITING_ARCHIVE
                rs.failure_reasons.append(
                    f"归档尚未到位, 恢复在等待 WAL: {all_miss[-1]}"
                    + ("(门控/归档迟到)" if gated else ""))
                store.upsert_restore(rs)
        else:
            miss_since = None
            if rs.phase == PHASE_RESTORE_WAITING_ARCHIVE:
                rs.phase = PHASE_RESTORE_REPLAYING
                store.upsert_restore(rs)

        time.sleep(poll)

    # 超时: 依据当前真实状态分类, 不谎报完成
    rs = store.get_restore(rs.name)
    if rs.phase == PHASE_RESTORE_WAITING_ARCHIVE:
        rs.failure_reasons.append(
            f"ARCHIVE_WAIT_TIMEOUT: 等待缺失 WAL 超过预算 {budget}s, "
            "保留诊断, 未静默恢复到更早位置")
        rs.result = RESULT_WAITING_ARCHIVE
    elif rs.phase != PHASE_TARGET_REACHED:
        rs.phase = PHASE_FAILED
        rs.result = RESULT_FAILED
        rs.failure_reasons.append(
            f"RECOVERY_TIMEOUT: {budget}s 内未到达目标(阶段={rs.phase})")
    store.upsert_restore(rs)
    return rs


def _collect_target_evidence(store: Store, rs: RestoreState,
                             log_line: str) -> dict:
    cfg = store.cfg
    evidence = {"log_line": log_line.strip(), "log_path": str(cfg.restore_log(rs.name))}
    try:
        # PG16 无 pg_stat_recovery_pause()(PG17+ 才有);
        # 暂停事实以日志 "pausing at the end of recovery" 为准
        row = psql_value(
            cfg, socket_dir=cfg.restore_socket(rs.name), port=rs.port,
            sql="SELECT pg_is_in_recovery()::text || '|' || "
                "COALESCE(pg_last_wal_replay_lsn()::text,'0/0');")
        in_rec, lsn = row.split("|")
        evidence.update({"pg_is_in_recovery": in_rec,
                         "last_wal_replay_lsn": lsn,
                         "recovery_pause": "confirmed-by-log"})
    except DrillError as e:
        evidence["sql_evidence_error"] = str(e)[:200]
    # pg_controldata 侧证
    ctrl = parse_controldata(run(
        [cfg.pgbin / "pg_controldata", cfg.restore_data(rs.name)]).stdout)
    evidence["controldata_state"] = ctrl.get("Database cluster state", "")
    evidence["controldata_timeline"] = ctrl.get("Latest checkpoint's TimeLineID", "")
    return evidence


# ---------------------------------------------------------------- 提升
def promote(store: Store, rs: RestoreState) -> dict:
    """只允许对隔离恢复实例提升; 提升后产生可区分的新时间线。"""
    cfg = store.cfg
    if rs.phase != PHASE_TARGET_REACHED:
        raise DrillError(
            f"仅 TARGET_REACHED 状态可提升, 当前 {rs.phase}/{rs.result}")
    data = cfg.restore_data(rs.name)
    r = run([cfg.pgbin / "pg_ctl", "-D", data, "-w", "-t", "60", "promote"],
            check=False)
    rs.commands.append({"ts": now_iso(), "step": "promote",
                        "argv": r.argv, "rc": r.rc,
                        "stderr_tail": r.stderr[-300:]})
    if r.rc != 0:
        raise DrillError(f"提升失败: {r.stderr.strip()[:300]}")
    wait_ready(cfg, cfg.restore_socket(rs.name), rs.port, timeout=60)
    # 提升后的新时间线: 以当前 WAL 文件名前缀为准(timeline_id 视图可能滞后),
    # 并以 pg_wal 下生成的 .history 文件交叉验证
    wal_name = psql_value(
        cfg, socket_dir=cfg.restore_socket(rs.name), port=rs.port,
        sql="SELECT pg_walfile_name(pg_current_wal_lsn());")
    tli = int(wal_name[:8], 16)
    history = sorted(p.name for p in (data / "pg_wal").iterdir()
                     if p.name.endswith(".history"))
    log_text = tail_text(cfg.restore_log(rs.name), 4000)
    m = PAT_PROMOTED.search(log_text)
    log_tli = int(m.group(1)) if m else None
    in_rec = psql_value(
        cfg, socket_dir=cfg.restore_socket(rs.name), port=rs.port,
        sql="SELECT pg_is_in_recovery()::text;")
    rs.phase = PHASE_PROMOTED
    rs.result = RESULT_PROMOTED
    rs.promoted_timeline = tli
    rs.target_evidence["promoted"] = {
        "timeline": tli, "log_selected_timeline": log_tli,
        "history_files": history, "current_wal": wal_name,
        "pg_is_in_recovery": in_rec,
        "log": log_text[-3000:]}
    store.upsert_restore(rs)
    return {"timeline": tli, "pg_is_in_recovery": in_rec}


def stop_restore(store: Store, rs: RestoreState) -> None:
    cfg = store.cfg
    data = cfg.restore_data(rs.name)
    pid = read_pidfile(data / "postmaster.pid")
    if pid > 0:
        r = run([cfg.pgbin / "pg_ctl", "-D", data, "-m", "fast",
                 "-w", "-t", "60", "stop"], check=False)
        rs.commands.append({"ts": now_iso(), "step": "stop",
                            "argv": r.argv, "rc": r.rc})
        store.upsert_restore(rs)


def drop_restore(store: Store, name: str) -> None:
    """删除隔离恢复目录; 拒绝触碰源库/归档/其他演练。"""
    cfg = store.cfg
    rs = store.get_restore(name)
    marker = cfg.marker_path("restore", name)
    if not marker.exists():
        raise DrillError(f"缺少 pitrdrill 恢复标记, 拒绝删除: {name}")
    stop_restore(store, rs)
    robust_rmtree(cfg.restore_dir(name))
    del store.state.restores[name]
    store.save()
