"""命令行入口：启动、停止、状态查询与人工恢复。

用法总览：
  python -m collector.cli start   --url ... --db ... [--pidfile ...] [--log ...] [退避/停止参数]
  python -m collector.cli stop    --pidfile ...
  python -m collector.cli status  --db ... [--json]
  python -m collector.cli readings --db ...
  python -m collector.cli requests --db ...
  python -m collector.cli incidents --db ...
  python -m collector.cli run-foreground --url ... --db ...      # 前台运行（演示/调试）
  python -m collector.cli recover gap     --db ... --incident ID (--accept-restart | --abandon) [--note ...]
  python -m collector.cli recover rotate  --db ... --incident ID [--note ...]
  python -m collector.cli recover conflict --db ... --incident ID [--note ...]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .client import Collector, CollectorConfig
from .storage import Storage


def _build_config(ns: argparse.Namespace) -> CollectorConfig:
    return CollectorConfig.from_env(
        url=ns.url,
        db_path=ns.db,
        initial_backoff=ns.initial_backoff,
        max_backoff=ns.max_backoff,
        backoff_factor=ns.backoff_factor,
        max_attempts=ns.max_attempts,
        max_run_seconds=ns.max_run_seconds,
        stop_file=ns.stop_file,
        read_size=ns.read_size,
    )


def _print_status(db_path: str, as_json: bool) -> int:
    storage = Storage(db_path)
    try:
        status = storage.status()
    finally:
        storage.close()
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    print(f"数据库: {status['db_path']}  (生成于 {status['generated_at']})")
    c = status["counts"]
    print(
        f"记录数: {c['readings']}  事件总数(incidents): {c['incidents']} "
        f"未处置: {c['open_incidents']}  请求次数: {c['requests']}"
    )
    if not status["streams"]:
        print("（尚无任何流）")
    for s in status["streams"]:
        print(
            f"- 流标识: {s['stream_id']}\n"
            f"  状态: {s['state']}\n"
            f"  可信断点(Last-Event-ID): {s['last_event_id']}\n"
            f"  已提交最大序号: {s['last_sequence']}\n"
            f"  重复数: {s['duplicate_count']}\n"
            f"  更新时间: {s['updated_at']}"
        )
        if s["reason"]:
            print(f"  阻塞原因: {s['reason']}")
    if status["open_incidents"]:
        print("\n待人工处置:")
        for inc in status["open_incidents"]:
            print(f"  [# {inc['id']}] {inc['kind']}: {inc['detail']}")
    # 退出码：存在未处置事件时返回 2，便于脚本判断。
    return 2 if status["open_incidents"] else 0


def cmd_start(ns: argparse.Namespace) -> int:
    pidfile = Path(ns.pidfile)
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            print(f"采集进程已在运行 (pid={pid})", file=sys.stderr)
            return 1
        except (ProcessLookupError, ValueError):
            print("发现过期 pid 文件，覆盖。", file=sys.stderr)
    log_f = open(ns.log, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "collector.cli", "run-foreground",
            "--url", ns.url,
            "--db", ns.db,
            "--initial-backoff", str(ns.initial_backoff),
            "--max-backoff", str(ns.max_backoff),
            "--backoff-factor", str(ns.backoff_factor),
            "--read-size", str(ns.read_size),
            *(("--max-attempts", str(ns.max_attempts)) if ns.max_attempts else ()),
            *(("--max-run-seconds", str(ns.max_run_seconds)) if ns.max_run_seconds else ()),
            *(("--stop-file", ns.stop_file) if ns.stop_file else ()),
        ],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))
    print(f"采集进程已启动 pid={proc.pid}，日志: {ns.log}，pidfile: {ns.pidfile}")
    return 0


def cmd_stop(ns: argparse.Namespace) -> int:
    pidfile = Path(ns.pidfile)
    if not pidfile.exists():
        print("pid 文件不存在，采集进程可能未运行。", file=sys.stderr)
        return 1
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"进程 {pid} 已不存在（可能已自然退出），清理 pid 文件。", file=sys.stderr)
        pidfile.unlink(missing_ok=True)
        return 0
    # 等待优雅退出（最多 10 秒）。
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        print("超时未退出，发送 SIGKILL。", file=sys.stderr)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pidfile.unlink(missing_ok=True)
    print(f"已停止采集进程 pid={pid}")
    return 0


def cmd_run_foreground(ns: argparse.Namespace) -> int:
    config = _build_config(ns)
    storage = Storage(config.db_path)
    collector = Collector(config, storage=storage)
    print(
        f"[run-foreground] 启动采集 url={config.url} db={config.db_path} pid={os.getpid()}",
        flush=True,
    )
    summary = collector.run()
    storage.close()
    print(
        f"[run-foreground] 结束 reason={summary.stop_reason} "
        f"attempts={summary.attempts} inserted={summary.events_inserted} "
        f"duplicates={summary.duplicates} "
        f"last_error={summary.last_error}",
        flush=True,
    )
    return 0 if not summary.blocked else 3


def cmd_readings(ns: argparse.Namespace) -> int:
    storage = Storage(ns.db)
    try:
        rows = storage.readings(limit=ns.limit)
        if ns.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"{r['event_id']:<12} type={r['event_type']}  {r['raw_payload']}")
        print(f"-- 共 {len(rows)} 行（limit={ns.limit}）")
    finally:
        storage.close()
    return 0


def cmd_requests(ns: argparse.Namespace) -> int:
    storage = Storage(ns.db)
    try:
        rows = storage.conn.execute(
            "SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (ns.limit,)
        ).fetchall()
        if ns.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(
                    f"[#{r['id']}] {r['started_at']} -> {r['status_code']} "
                    f"{r['reason']} (saved={r['events_saved']}, dupes={r['dupes_saved']})"
                )
                print(f"    URL: {r['url']}")
                print("    请求头:")
                for line in r["request_headers"].splitlines():
                    print("      " + line)
    finally:
        storage.close()
    return 0


def cmd_incidents(ns: argparse.Namespace) -> int:
    storage = Storage(ns.db)
    try:
        rows = storage.conn.execute(
            "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (ns.limit,)
        ).fetchall()
        if ns.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        else:
            for r in rows:
                state = "已处置" if r["resolved_at"] else "未处置"
                print(f"[#{r['id']}] {r['kind']} ({state})")
                print(f"    {r['detail']}")
                if r["incoming_payload"]:
                    print(f"    新到内容: {r['incoming_payload']}")
                if r["resolution"]:
                    print(f"    裁决: {r['resolution']} @ {r['resolved_at']}")
    finally:
        storage.close()
    return 0


def cmd_recover(ns: argparse.Namespace) -> int:
    storage = Storage(ns.db)
    try:
        if ns.kind == "gap":
            storage.recover_gap(
                ns.incident,
                accept_restart=ns.accept_restart,
                abandon=ns.abandon,
                note=ns.note or "",
            )
            print(
                f"已处置缺口 incident=#{ns.incident}: "
                + ("接受缺口并从上游可用位置重拉（清空本流可信断点）"
                   if ns.accept_restart else "保持停止（人工放弃）")
            )
        elif ns.kind == "rotate":
            new_stream = storage.adopt_stream(ns.incident, ns.note or "")
            print(f"已确认换流 incident=#{ns.incident}，新流 {new_stream} 已启用（序号从 1 开始）")
        elif ns.kind == "conflict":
            storage.resolve_conflict(ns.incident, ns.note or "")
            print(f"已确认冲突 incident=#{ns.incident}：保留原始记录，解除该流阻塞")
    finally:
        storage.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="collector", description="单路 SSE 可靠采集客户端")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_conn(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--url", required=True, help="SSE 上游 URL")
        sp.add_argument("--db", required=True, help="SQLite 数据库路径")
        sp.add_argument("--initial-backoff", type=float, default=1.0)
        sp.add_argument("--max-backoff", type=float, default=30.0)
        sp.add_argument("--backoff-factor", type=float, default=2.0)
        sp.add_argument("--max-attempts", type=int, default=None,
                        help="连接尝试次数上限；默认不限（配合 max-run-seconds/停止文件）")
        sp.add_argument("--max-run-seconds", type=float, default=None,
                        help="总运行时长上限，到时优雅停止")
        sp.add_argument("--stop-file", default=None, help="该文件出现即优雅停止")
        sp.add_argument("--read-size", type=int, default=4096,
                        help="单次 socket 读取字节数（调小可强制制造分块）")

    sp = sub.add_parser("start", help="后台启动采集守护进程")
    add_common_conn(sp)
    sp.add_argument("--pidfile", default=".runtime/collector.pid")
    sp.add_argument("--log", default=".runtime/collector.log")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="停止采集守护进程")
    sp.add_argument("--pidfile", default=".runtime/collector.pid")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", help="查询采集状态")
    sp.add_argument("--db", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=lambda ns: _print_status(ns.db, ns.json))

    sp = sub.add_parser("readings", help="列出已落库业务记录")
    sp.add_argument("--db", required=True)
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_readings)

    sp = sub.add_parser("requests", help="列出请求日志（含请求头核对材料）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_requests)

    sp = sub.add_parser("incidents", help="列出冲突/缺口/换流事件")
    sp.add_argument("--db", required=True)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_incidents)

    sp = sub.add_parser("run-foreground", help="前台运行（演示/调试/被守护进程拉起）")
    add_common_conn(sp)
    sp.set_defaults(func=cmd_run_foreground)

    sp = sub.add_parser("recover", help="人工恢复入口")
    rsub = sp.add_subparsers(dest="kind", required=True)

    g = rsub.add_parser("gap", help="处置 410 保留窗口缺口/序号缺口")
    g.add_argument("--db", required=True)
    g.add_argument("--incident", type=int, required=True)
    grp = g.add_mutually_exclusive_group(required=True)
    grp.add_argument("--accept-restart", action="store_true",
                     help="接受缺口：清空本流断点，从上游当前可用位置重拉（不宣称连续）")
    grp.add_argument("--abandon", action="store_true", help="保持停止，记录放弃裁决")
    g.add_argument("--note", default="")
    g.set_defaults(func=cmd_recover)

    r = rsub.add_parser("rotate", help="确认换流：启用新流，旧流序号不沿用")
    r.add_argument("--db", required=True)
    r.add_argument("--incident", type=int, required=True)
    r.add_argument("--note", default="")
    r.set_defaults(func=cmd_recover)

    c = rsub.add_parser("conflict", help="确认冲突：保留原始记录并解除阻塞")
    c.add_argument("--db", required=True)
    c.add_argument("--incident", type=int, required=True)
    c.add_argument("--note", default="")
    c.set_defaults(func=cmd_recover)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
