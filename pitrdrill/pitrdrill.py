#!/usr/bin/env python3
"""pitrdrill — PostgreSQL 16 时间点恢复(PITR)演练助手。

仅管理本工具创建并带 pitrdrill 标识的本地实例, 不连接/停止用户已有数据库。

一条命令启动完整本地演练:
    python3 pitrdrill.py demo
查看:
    python3 pitrdrill.py status
    python3 pitrdrill.py candidates
    python3 pitrdrill.py preflight --backup b1-early --target rp_early --set set-full
清理(仅本演练资源):
    python3 pitrdrill.py cleanup --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许 `python3 /path/pitrdrill.py` 直接运行(包在同名子目录)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pitrdrill_cli.archive import archive_push, build_archive_set  # noqa: E402
from pitrdrill_cli.cleanup import cleanup, list_resources  # noqa: E402
from pitrdrill_cli.config import DrillConfig  # noqa: E402
from pitrdrill_cli.demo import run_full_demo  # noqa: E402
from pitrdrill_cli.instance import (  # noqa: E402
    create_business_db, create_restore_point, init_source,
    insert_witness, start_source, stop_source, take_base_backup)
from pitrdrill_cli.recovery import (  # noqa: E402
    drop_restore, monitor, prepare_restore_dir, preflight, promote,
    start_restore, stop_restore)
from pitrdrill_cli.report import RESULT_CN, build_report  # noqa: E402
from pitrdrill_cli.state import RestoreState, Store  # noqa: E402
from pitrdrill_cli.util import (  # noqa: E402
    DrillError, now_iso, read_pidfile, pid_alive, tail_text)
from pitrdrill_cli.verify import verify_restored  # noqa: E402


def _store(args) -> Store:
    cfg = DrillConfig(root=Path(args.root))
    return Store(cfg)


def cmd_archive_push(args) -> int:
    cfg = DrillConfig(root=Path(args.root))
    return archive_push(cfg, args.wal_path, args.wal_name)


def cmd_init(args) -> int:
    store = _store(args)
    init_source(store)
    start_source(store)
    create_business_db(store)
    print(json.dumps(store.state.source, ensure_ascii=False, indent=2))
    return 0


def cmd_stop_source(args) -> int:
    stop_source(_store(args))
    print("源实例已停止(数据与归档保留)")
    return 0


def cmd_backup(args) -> int:
    store = _store(args)
    rec = take_base_backup(store, args.name, args.note or args.name)
    print(json.dumps({k: rec[k] for k in (
        "name", "start_lsn", "checkpoint_lsn", "backup_timeline",
        "system_identifier", "verifybackup_rc", "backup_wal_segments")},
        ensure_ascii=False, indent=2))
    return 0


def cmd_restore_point(args) -> int:
    store = _store(args)
    rec = create_restore_point(store, args.name, args.note or args.name)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_witness(args) -> int:
    store = _store(args)
    cfg = store.cfg
    rec = insert_witness(cfg, args.marker,
                         json.loads(args.detail or "{}"),
                         cfg.source_socket, cfg.source_port)
    store.add_event("witness", **rec)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_mkset(args) -> int:
    store = _store(args)
    include = args.include.split(",") if args.include else None
    exclude = args.exclude.split(",") if args.exclude else None
    manifest = build_archive_set(store, args.name,
                                 include=include, exclude=exclude)
    print(f"集合 {args.name}: {len(manifest['files'])} 个文件 -> "
          f"{store.cfg.set_dir(args.name)}")
    return 0


def cmd_candidates(args) -> int:
    store = _store(args)
    st = store.state
    print("== 基础备份候选 ==")
    for name, b in st.backups.items():
        print(f"  {name:24s} lsn={b.get('start_lsn',''):18s} "
              f"tli={b.get('backup_timeline')} sysid={b.get('system_identifier')}"
              f"{' [CORRUPT]' if b.get('corrupt') else ''}")
        print(f"    路径: {b.get('path')}")
        print(f"    自带WAL: {len(b.get('backup_wal_segments', []))} 段, "
              f"pg_verifybackup rc={b.get('verifybackup_rc')}")
    print("\n== restore point 候选 ==")
    for r in st.restore_points:
        print(f"  {r['name']:24s} lsn={r['lsn']:18s} tli={r['timeline']}  {r['note']}")
    print("\n== 归档集合候选 ==")
    for name, d in st.archive_sets.items():
        print(f"  {name:24s} 文件={len(d.get('files', []))} 排除={d.get('excluded', [])}")
    print("\n== 恢复前提提示 ==")
    if st.backups and st.restore_points and st.archive_sets:
        b = next(iter(st.backups))
        rp = st.restore_points[-1]["name"]
        s = next(iter(st.archive_sets))
        print(f"  示例: preflight --backup {b} --target {rp} --set {s}")
    return 0


def cmd_preflight(args) -> int:
    store = _store(args)
    pf = preflight(store, args.backup, args.target, args.set,
                   wait_budget=args.wait_budget)
    print(json.dumps(pf, ensure_ascii=False, indent=2))
    return 0 if pf["passed"] else 2


def _new_restore(store: Store, args) -> RestoreState:
    cfg = store.cfg
    return RestoreState(
        name=args.name, backup=args.backup, target=args.target,
        archive_set=args.set,
        data_dir=str(cfg.restore_data(args.name)),
        socket_dir=str(cfg.restore_socket(args.name)),
        log_path=str(cfg.restore_log(args.name)), port=0,
        created_at=now_iso())


def cmd_restore_start(args) -> int:
    store = _store(args)
    rs = _new_restore(store, args)
    store.upsert_restore(rs)
    pf = preflight(store, args.backup, args.target, args.set,
                   wait_budget=args.wait_budget)
    rs = store.get_restore(args.name)
    rs.preflight = pf
    store.upsert_restore(rs)
    if not pf["passed"]:
        if not args.force:
            rs.failure_reasons.extend(pf["hard_errors"])
            rs.result = "FAILED"
            store.upsert_restore(rs)
            print("预检未通过, 拒绝启动恢复(可用 --force 仅用于故障观察):",
                  file=sys.stderr)
            for e in pf["hard_errors"]:
                print(f"  - {e}", file=sys.stderr)
            return 2
    prepare_restore_dir(store, rs)
    start_restore(store, rs)
    rs = monitor(store, rs, budget=args.recovery_budget)
    print(f"恢复实例 {args.name}: {rs.phase}/{rs.result}")
    for r in rs.failure_reasons:
        print(f"  诊断: {r}")
    return 0 if rs.result in ("TARGET_REACHED", "PROMOTED") else 1


def cmd_restore_observe(args) -> int:
    store = _store(args)
    rs = store.get_restore(args.name)
    pid = read_pidfile(Path(rs.data_dir) / "postmaster.pid")
    print(f"实例 {args.name}: phase={rs.phase} result={rs.result} "
          f"pid={pid} alive={pid_alive(pid)} port={rs.port}")
    print(f"日志: {rs.log_path}")
    if args.follow:
        rs = monitor(store, rs, budget=args.timeout)
        print(f"监控结束: {rs.phase}/{rs.result}")
    else:
        print(tail_text(Path(rs.log_path), 4000))
    return 0


def cmd_promote(args) -> int:
    store = _store(args)
    rs = store.get_restore(args.name)
    out = promote(store, rs)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    mishap = next((e for e in store.state.events if e.get("kind") == "mishap"), None)
    summary = verify_restored(
        store, args.name,
        expect_markers=args.expect.split(","),
        absent_markers=(args.absent.split(",") if args.absent else []),
        mishap=None if args.allow_mishap_absent else mishap,
        require_promoted=args.require_promoted)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 3


def cmd_restore_stop(args) -> int:
    store = _store(args)
    stop_restore(store, store.get_restore(args.name))
    print(f"恢复实例 {args.name} 已停止(目录保留)")
    return 0


def cmd_restore_drop(args) -> int:
    drop_restore(_store(args), args.name)
    print(f"已删除隔离恢复目录: {args.name}")
    return 0


def cmd_status(args) -> int:
    store = _store(args)
    st = store.state
    print(f"任务: {st.task_tag}  阶段: {st.phase}  根目录: {store.cfg.root}")
    src = st.source
    if src:
        pid = read_pidfile(Path(src["data_dir"]) / "postmaster.pid")
        print(f"源实例: port={src.get('port')} pid={pid} "
              f"alive={pid_alive(pid)} sysid={src.get('system_identifier')}")
    print(f"备份: {list(st.backups)}")
    print(f"恢复点: {[r['name'] for r in st.restore_points]}")
    print(f"归档集合: {list(st.archive_sets)}")
    print("恢复实例:")
    for name, rs in st.restores.items():
        print(f"  {name:20s} {rs.phase:18s} {RESULT_CN.get(rs.result, rs.result)}"
              f"  tli={rs.promoted_timeline or '-'}")
    print(json.dumps(list_resources(store), ensure_ascii=False, indent=2))
    return 0


def cmd_report(args) -> int:
    path = build_report(_store(args))
    print(f"报告已生成: {path}")
    return 0


def cmd_cleanup(args) -> int:
    store = _store(args)
    if not args.yes:
        print("需要 --yes 确认; 该命令仅清理本演练根目录内带 pitrdrill 标记的资源")
        return 1
    out = cleanup(store, include_source=args.include_source,
                  include_archive=args.include_archive, yes=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_demo(args) -> int:
    cfg = DrillConfig(root=Path(args.root))
    run_full_demo(cfg, quick=args.quick, rebuild=args.rebuild)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pitrdrill",
        description="PostgreSQL 16 PITR 演练助手(仅操作自建隔离实例)")
    p.add_argument("--root", default="/workspace/pitrdrill-work",
                   help="演练根目录(清理边界), 默认 /workspace/pitrdrill-work")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("archive-push", help="archive_command 内部入口")
    sp.add_argument("wal_path")
    sp.add_argument("wal_name")
    sp.set_defaults(func=cmd_archive_push)

    sub.add_parser("init", help="initdb 并启动源实例+业务库").set_defaults(func=cmd_init)
    sub.add_parser("stop-source", help="停止源实例(保留数据)").set_defaults(func=cmd_stop_source)

    sp = sub.add_parser("backup", help="取得真实物理基础备份")
    sp.add_argument("name")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore-point", help="创建命名恢复点并归档其 WAL")
    sp.add_argument("name")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_restore_point)

    sp = sub.add_parser("witness", help="写入事务见证标记")
    sp.add_argument("marker")
    sp.add_argument("--detail", default="{}")
    sp.set_defaults(func=cmd_witness)

    sp = sub.add_parser("mkset", help="建立归档集合快照")
    sp.add_argument("name")
    sp.add_argument("--include", default=None)
    sp.add_argument("--exclude", default=None)
    sp.set_defaults(func=cmd_mkset)

    sub.add_parser("candidates", help="查看备份/恢复点/归档集合候选与恢复前提"
                   ).set_defaults(func=cmd_candidates)

    sp = sub.add_parser("preflight", help="恢复前提与完整性核验")
    sp.add_argument("--backup", required=True)
    sp.add_argument("--target", required=True)
    sp.add_argument("--set", required=True)
    sp.add_argument("--wait-budget", type=int, default=None)
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("restore-start", help="启动隔离恢复并监控到目标/失败")
    sp.add_argument("name")
    sp.add_argument("--backup", required=True)
    sp.add_argument("--target", required=True)
    sp.add_argument("--set", required=True)
    sp.add_argument("--wait-budget", type=int, default=None)
    sp.add_argument("--recovery-budget", type=int, default=180)
    sp.add_argument("--force", action="store_true", help="预检失败仍强制启动(仅故障观察)")
    sp.set_defaults(func=cmd_restore_start)

    sp = sub.add_parser("restore-observe", help="继续观察正在恢复的实例")
    sp.add_argument("name")
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("--timeout", type=int, default=120)
    sp.set_defaults(func=cmd_restore_observe)

    sp = sub.add_parser("promote", help="对 TARGET_REACHED 的隔离实例提升")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("verify", help="独立 SQL 逐表核验")
    sp.add_argument("name")
    sp.add_argument("--expect", required=True, help="应存在的标记, 逗号分隔")
    sp.add_argument("--absent", default="", help="应不存在的标记, 逗号分隔")
    sp.add_argument("--require-promoted", action="store_true")
    sp.add_argument("--allow-mishap-absent", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("restore-stop", help="停止隔离恢复实例")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_restore_stop)

    sp = sub.add_parser("restore-drop", help="显式重建: 删除隔离恢复目录")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_restore_drop)

    sub.add_parser("status", help="查看整体状态与资源").set_defaults(func=cmd_status)
    sub.add_parser("report", help="生成 Markdown/JSON 报告").set_defaults(func=cmd_report)

    sp = sub.add_parser("cleanup", help="只清理本次演练资源")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--include-source", action="store_true",
                    help="同时停止并删除源实例")
    sp.add_argument("--include-archive", action="store_true",
                    help="同时删除 WAL 归档目录")
    sp.set_defaults(func=cmd_cleanup)

    sp = sub.add_parser("demo", help="一条命令执行完整本地演练")
    sp.add_argument("--quick", action="store_true", help="缩短时长的冒烟模式")
    sp.add_argument("--rebuild", action="store_true",
                    help="显式清理上一轮本演练资源后重建(默认拒绝覆盖)")
    sp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DrillError as e:
        print(f"[pitrdrill 错误] {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\n[pitrdrill] 被中断; 实例与状态已留痕, 可用 status 查看、cleanup 清理",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
