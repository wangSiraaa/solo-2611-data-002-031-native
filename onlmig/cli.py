"""命令行入口。

迁移命令:
  setup              安装表结构与变更捕获触发器
  start [--chunk N]  建立迁移（持久化初始进度并获取互斥锁）
  step               推进一个回填块并排空一轮变更
  resume             从持久化进度持续推进到回填完成、无待处理变更
  status             查看进度、待处理变更、失败位置
  finalize           短暂阻止源写入，排空并核验，通过才标记 READY
  verify             只读逐主键逐字段核验
  reset              仅演示用：清空全部表与迁移进度，回到初始状态

写入命令（见 writer --help）:
  writer insert|update|delete ...
  writer run ...     并发写入器
"""
from __future__ import annotations

import argparse
import json

from . import engine, schema, writer
from .db import connect
from .engine import FaultGate, FinalizeBlocked, MigrationError


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_setup(args) -> None:
    conn = connect()
    schema.install(conn)
    print("已安装：records(含 row_version)、record_changes 触发器、canonical_records、迁移进度表")


def cmd_start(args) -> None:
    conn = connect()
    created = engine.start(conn, args.chunk)
    if created:
        print(f"迁移已建立，chunk_size={args.chunk}，状态 RUNNING，互斥咨询锁键={engine.MIGRATION_LOCK_KEY:#x}")
    else:
        print("迁移已存在，start 幂等返回；当前状态:", engine.get_meta(conn)["state"])
    engine.release_lock(conn)


def cmd_step(args) -> None:
    conn = connect()
    try:
        result = engine.step(conn, FaultGate.from_env())
        _print_json(result)
    finally:
        engine.release_lock(conn)


def cmd_resume(args) -> None:
    conn = connect()
    try:
        totals = engine.run(conn, FaultGate.from_env())
        meta = engine.get_meta(conn)
        _print_json({"resume": totals, "meta": meta})
    finally:
        engine.release_lock(conn)


def cmd_status(args) -> None:
    conn = connect()
    meta = engine.get_meta(conn)
    with conn.transaction():
        pending = conn.execute(
            "SELECT count(*) FROM record_changes WHERE change_seq > %s",
            [meta["applied_max"]],
        ).fetchone()[0]
        behind_watermark = engine.count_unapplied_changes(conn, meta["watermark"])
        max_seq = conn.execute(
            "SELECT COALESCE(MAX(change_seq), 0) FROM record_changes"
        ).fetchone()[0]
        chunks = conn.execute(
            "SELECT chunk_no, id_range, rows_scanned, rows_applied, errors, finished_at"
            " FROM migration_chunks ORDER BY chunk_no"
        ).fetchall()
        errors = conn.execute(
            "SELECT id, row_version, stage, message FROM migration_errors ORDER BY id"
        ).fetchall()
        src_count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        tgt_count = conn.execute("SELECT count(*) FROM canonical_records").fetchone()[0]
    locked = engine.lock_held_by_other(conn)
    _print_json({
        "state": meta["state"],
        "advisory_lock_held": locked,
        "backfill_after_id": meta["backfill_after_id"],
        "backfill_done": meta["backfill_done"],
        "watermark": meta["watermark"],
        "applied_max": meta["applied_max"],
        "max_change_seq": max_seq,
        "committed_changes_behind_watermark": behind_watermark,
        "pending_changes": pending,
        "chunk_size": meta["chunk_size"],
        "chunks_done": len(chunks),
        "last_chunk": chunks[-1] if chunks else None,
        "source_rows": src_count,
        "target_rows": tgt_count,
        "fail_stage": meta["fail_stage"],
        "fail_message": meta["fail_message"],
        "error_locations": [
            {"id": e[0], "row_version": e[1], "stage": e[2], "message": e[3]} for e in errors
        ],
    })


def cmd_finalize(args) -> None:
    conn = connect()
    try:
        report = engine.finalize(conn, FaultGate.from_env())
        _print_json(report)
        print("FINALIZE 成功：迁移状态 READY")
    except FinalizeBlocked as exc:
        print(str(exc))
        raise SystemExit(3)
    finally:
        engine.release_lock(conn)


def cmd_verify(args) -> None:
    conn = connect()
    report = engine.verify(conn)
    _print_json(report)
    if not report["ok"]:
        raise SystemExit(4)


def cmd_reset(args) -> None:
    conn = connect()
    engine.reset(conn)
    print("已清空全部表与迁移进度，回到初始状态")


# ------------------------------- writer CLI -------------------------------- #
def cmd_writer_insert(args) -> None:
    conn = connect()
    writer.do_insert(conn, args.id, args.code, args.amount, args.active)
    print(f"INSERT id={args.id} 已提交")


def cmd_writer_update(args) -> None:
    conn = connect()
    writer.do_update(conn, args.id, args.code, args.amount, args.active)
    print(f"UPDATE id={args.id} 已提交")


def cmd_writer_delete(args) -> None:
    conn = connect()
    writer.do_delete(conn, args.id)
    print(f"DELETE id={args.id} 已提交")


def cmd_writer_run(args) -> None:
    stats = writer.run_concurrent(
        args.seconds, args.threads, args.rate, args.rollback_pct,
        args.id_start, args.seed,
    )
    _print_json(stats)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="onlmig", description="本地在线回填演示 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup").set_defaults(func=cmd_setup)

    sp = sub.add_parser("start")
    sp.add_argument("--chunk", type=int, default=500)
    sp.set_defaults(func=cmd_start)

    sub.add_parser("step").set_defaults(func=cmd_step)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("finalize").set_defaults(func=cmd_finalize)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    sub.add_parser("reset").set_defaults(func=cmd_reset)

    wp = sub.add_parser("writer", help="源表写入器").add_subparsers(dest="wcmd", required=True)

    def add_row_args(q):
        q.add_argument("id", type=int)
        q.add_argument("code")
        q.add_argument("amount", type=int)
        q.add_argument("active", choices=["true", "false"])

    wi = wp.add_parser("insert"); add_row_args(wi); wi.set_defaults(func=cmd_writer_insert)
    wu = wp.add_parser("update"); add_row_args(wu); wu.set_defaults(func=cmd_writer_update)
    wd = wp.add_parser("delete"); wd.add_argument("id", type=int); wd.set_defaults(func=cmd_writer_delete)

    wr = wp.add_parser("run", help="并发写入器")
    wr.add_argument("--seconds", type=float, default=10)
    wr.add_argument("--threads", type=int, default=3)
    wr.add_argument("--rate", type=float, default=20.0, help="每线程每秒事务上限")
    wr.add_argument("--rollback-pct", type=float, default=5.0)
    wr.add_argument("--id-start", type=int, default=10_000)
    wr.add_argument("--seed", type=int, default=42)
    wr.set_defaults(func=cmd_writer_run)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except MigrationError as exc:
        print(f"迁移错误: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
