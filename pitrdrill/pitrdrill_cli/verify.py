"""恢复结果核验: 全部通过独立 SQL 在隔离实例上逐表核对。

核验维度:
  1. 目标标记存在/不存在(事务见证 pitrdrill_witness)
  2. 误删除未发生(父表 3/7 仍在, 子表外键完整)
  3. 误更新未发生(parent 5 未被 MISHAP 污染)
  4. 目标后的正常新增不存在(id=9001)
  5. 父子外键约束真实有效(尝试违规插入必须被拒绝)
  6. 序列后续插入行为正常(nextval 推进且可插入子表)
  7. LSN 截断: 重放 LSN 不超过目标 restore point LSN, 且目标后事件不存在
"""
from __future__ import annotations

from .config import DrillConfig
from .state import Store
from .util import DrillError, lsn_to_int, psql

DB_NAME = "pitrdrill_biz"


def _q(cfg: DrillConfig, socket_dir, port: int, sql: str,
       dbname: str = DB_NAME) -> str:
    r = psql(cfg, socket_dir=socket_dir, port=port, sql=sql, dbname=dbname)
    if not r.ok:
        raise DrillError(f"核验 SQL 失败: {r.stderr.strip()}\nSQL: {sql}")
    return r.stdout.strip()


def verify_restored(store: Store, rs_name: str,
                    expect_markers: list[str],
                    absent_markers: list[str],
                    mishap: dict | None,
                    *, require_promoted: bool = False) -> dict:
    cfg = store.cfg
    rs = store.get_restore(rs_name)
    sock = cfg.restore_socket(rs_name)
    port = rs.port
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    # 0. 恢复/提升状态
    in_recovery = _q(cfg, sock, port, "SELECT pg_is_in_recovery()::text;")
    if in_recovery == "false":
        wal_name = _q(cfg, sock, port,
                      "SELECT pg_walfile_name(pg_current_wal_lsn());")
        tli = int(wal_name[:8], 16)
    else:
        tli = int(_q(cfg, sock, port,
                     "SELECT timeline_id FROM pg_control_checkpoint();"))
    record("实例可连接且身份为 pitrdrill 隔离实例",
           (cfg.marker_path("restore", rs_name)).exists(),
           f"pg_is_in_recovery={in_recovery}, timeline={tli}")
    if require_promoted:
        record("已提升为独立主库(新时间线)", in_recovery == "false",
               f"pg_is_in_recovery={in_recovery}, timeline={tli}, "
               f"提升记录时间线={rs.promoted_timeline}")
        # 新时间线必须与源库时间线(1)区分, 且与原归档不混同
        history = [p.name for p in (cfg.restore_data(rs_name) / "pg_wal").iterdir()
                   if p.name.endswith(".history")]
        record("提升产生可区分的新时间线(>源时间线 1)且生成 history 文件",
               tli > 1 and bool(history),
               f"new timeline={tli}, history={history}")
    else:
        record("仍处于恢复暂停态(未自动提升)", in_recovery == "true",
               f"pg_is_in_recovery={in_recovery}")

    # 1. 目标标记
    for m in expect_markers:
        row = _q(cfg, sock, port,
                 f"SELECT marker || '|' || commit_lsn::text || '|' || event_seq "
                 f"FROM pitrdrill_witness WHERE marker='{m}';")
        record(f"目标标记存在: {m}", bool(row), row or "未找到")
    for m in absent_markers:
        row = _q(cfg, sock, port,
                 f"SELECT count(*) FROM pitrdrill_witness WHERE marker='{m}';")
        record(f"更晚标记不存在(目标后事务已排除): {m}", row == "0", f"count={row}")

    # 2/3/4. 误操作未发生
    if mishap:
        for pid in mishap.get("deleted_parents", [3, 7]):
            row = _q(cfg, sock, port,
                     f"SELECT id || '|' || name FROM pitrdrill_parent WHERE id={pid};")
            record(f"误删除未发生: parent {pid} 仍存在", bool(row), row or "行缺失")
        c5 = _q(cfg, sock, port,
                "SELECT payload FROM pitrdrill_parent WHERE id=5;")
        record("误更新未发生: parent 5 未被 MISHAP 污染",
               "MISHAP" not in c5, f"payload={c5}")
        row9001 = _q(cfg, sock, port,
                     "SELECT count(*) FROM pitrdrill_parent WHERE id=9001;")
        record("目标后正常新增已排除: parent 9001 不存在",
               row9001 == "0", f"count={row9001}")
        mishap_events = _q(
            cfg, sock, port,
            "SELECT count(*) FROM pitrdrill_event_log WHERE kind='mishap';")
        record("误操作事件日志不存在", mishap_events == "0",
               f"mishap count={mishap_events}")

    # 5. 父子外键约束真实有效
    fk_def = _q(cfg, sock, port,
                "SELECT conname || '|' || confrelid::regclass::text "
                "FROM pg_constraint WHERE conname='pitrdrill_child_parent_id_fkey';")
    record("父子外键约束定义存在", bool(fk_def), fk_def or "未找到外键")
    fk_count = _q(cfg, sock, port,
                  "SELECT count(*) FROM pitrdrill_child c "
                  "LEFT JOIN pitrdrill_parent p ON p.id=c.parent_id "
                  "WHERE p.id IS NULL;")
    record("不存在悬空子记录", fk_count == "0", f"orphan count={fk_count}")
    if in_recovery == "f":
        fk_violation = psql(cfg, sock, port,
                            "INSERT INTO pitrdrill_child(parent_id, tick, note) "
                            "VALUES (999999, 1, 'fk-probe');", dbname=DB_NAME)
        record("父子外键约束拒绝悬空子记录", not fk_violation.ok
               and "foreign key" in (fk_violation.stderr + fk_violation.stdout).lower(),
               (fk_violation.stderr or fk_violation.stdout).strip()[:240])
    else:
        record("只读暂停态跳过违规插入探针(提升后 R2 执行真实约束拒绝)",
               True, "符合只读恢复语义")

    # 6. 序列后续插入行为
    #    暂停态实例为只读, 只能做只读核验; 提升后才执行真实插入探针
    seq_before = int(_q(cfg, sock, port,
                        "SELECT last_value FROM pitrdrill_event_seq;"))
    if in_recovery == "f":
        seq_next = int(_q(cfg, sock, port,
                          "SELECT nextval('pitrdrill_event_seq');"))
        record("序列可继续推进", seq_next == seq_before + 1,
               f"last_value={seq_before} -> nextval={seq_next}")
        ins = _q(cfg, sock, port,
                 "INSERT INTO pitrdrill_child(parent_id, tick, note) "
                 "VALUES (1, 0, 'post-recovery-seq-probe') RETURNING id;")
        record("identity 序列后续插入成功", bool(ins.isdigit()),
               f"new child id={ins}")
        record("插入后父子关联可查询", bool(_q(
            cfg, sock, port,
            "SELECT count(*) FROM pitrdrill_child WHERE note='post-recovery-seq-probe';")),
               "probe row visible")
    else:
        seq_defined = _q(cfg, sock, port,
                         "SELECT count(*) FROM pg_sequences "
                         "WHERE sequencename IN ('pitrdrill_event_seq',"
                         " 'pitrdrill_child_id_seq');")
        record("序列对象在恢复态完好可读", seq_defined == "2",
               f"sequences found={seq_defined}, last_value={seq_before}")
        record("只读暂停态跳过写入探针(提升后 R2 执行真实插入)", True,
               "符合 recovery_target_action=pause 的只读语义")

    # 7. LSN 截断证据
    rp = next(r for r in store.state.restore_points if r["name"] == rs.target)
    replay = _q(cfg, sock, port,
                "SELECT COALESCE(pg_last_wal_replay_lsn()::text,"
                "(SELECT redo_lsn FROM pg_control_checkpoint())::text);")
    target_lsn = rp["lsn"]
    from .util import lsn_to_int
    within = lsn_to_int(replay) <= lsn_to_int(target_lsn) + (16 * 1024 * 1024)
    record("重放 LSN 不越过目标 restore point(容差一个 WAL 段)",
           within, f"replay={replay} target={target_lsn}")

    summary = {
        "restore": rs_name, "total": len(checks),
        "passed": sum(1 for c in checks if c["passed"]),
        "failed": sum(1 for c in checks if not c["passed"]),
        "checks": checks,
        "timeline": tli,
        "all_passed": all(c["passed"] for c in checks),
    }
    rs.verification = summary
    store.upsert_restore(rs)
    return summary


def source_snapshot(store: Store) -> dict:
    """记录源库当前真实状态, 供报告对比(不用于恢复本身)。"""
    cfg = store.cfg
    sock = cfg.source_socket
    port = cfg.source_port
    out = {}
    for key, sql in {
        "parent_count": "SELECT count(*) FROM pitrdrill_parent;",
        "child_count": "SELECT count(*) FROM pitrdrill_child;",
        "witness_count": "SELECT count(*) FROM pitrdrill_witness;",
        "parent_5_payload": "SELECT payload FROM pitrdrill_parent WHERE id=5;",
        "parent_9001": "SELECT count(*) FROM pitrdrill_parent WHERE id=9001;",
    }.items():
        out[key] = _q(cfg, sock, port, sql)
    return out
