#!/usr/bin/env python3
"""端到端演练：用给定 fixture 展示三个关键场景并留存核对材料。

场景：
  1) partial  —— 半条消息断开后的恢复（断在半个多字节字符、无空行）
  2) 完整消息重发 —— 同流同序号内容一致，去重不重复入账
  3) 进程重启 —— 用已提交 Last-Event-ID 续接（含提交前/提交后两个崩溃时点）

附加场景（也一并演示，输出到同一目录）：
  4) conflict —— 内容不同的重发：停止流、保留原始记录、人工恢复入口
  5) expired  —— 410 保留窗口缺口：显式缺口状态与人工选择
  6) rotate   —— 流标识改变：不沿用旧序号，人工确认后启用新流

所有产物（数据库、CLI 输出、请求头、落库记录）写入 .runtime/demo/。
用法：python3 scripts/demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / ".runtime" / "demo"
FIXTURE_PORT = 8106
BASE = f"http://127.0.0.1:{FIXTURE_PORT}/events"


def run(cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    p = subprocess.run(cmd, cwd=ROOT, env=merged_env, capture_output=True,
                       text=True, timeout=120)
    if p.stdout:
        print(p.stdout)
    if p.stderr:
        print(p.stderr, file=sys.stderr)
    if check and p.returncode not in (0, 2, 3):
        raise SystemExit(f"命令失败({p.returncode}): {' '.join(cmd)}")
    return p


def cli(*args: str) -> subprocess.CompletedProcess:
    return run([sys.executable, "-m", "collector.cli", *args])


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def snapshot(db: Path, tag: str) -> None:
    """把状态、请求头、落库记录三类核对材料落盘。"""
    out = DEMO / tag
    out.mkdir(parents=True, exist_ok=True)
    for name, args in (
        ("status", ["status", "--db", str(db), "--json"]),
        ("requests", ["requests", "--db", str(db), "--json"]),
        ("readings", ["readings", "--db", str(db), "--json"]),
        ("incidents", ["incidents", "--db", str(db), "--json"]),
    ):
        p = run([sys.executable, "-m", "collector.cli", *args], check=False)
        (out / f"{name}.json").write_text(p.stdout, encoding="utf-8")


def main() -> None:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)

    fixture = subprocess.Popen(
        [sys.executable, str(ROOT / "fixtures" / "sse_upstream.py"),
         "--port", str(FIXTURE_PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(1.0)
    if fixture.poll() is not None:
        raise SystemExit("fixture 启动失败：" + (fixture.stdout.read() if fixture.stdout else ""))
    try:
        # ---------------------------------------------------------------- #
        section("场景 1：半条消息断开后的恢复（partial，断在事件 4 的半个码位上）")
        db1 = DEMO / "partial.db"
        url = f"{BASE}?case=partial&client=demo-partial"
        cli("run-foreground", "--url", url, "--db", str(db1),
            "--max-attempts", "1", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db1, "01_after_half_message")
        print(">> 事件 4 的半条消息未入账，可信断点停在 streamA:3（见 01 目录）")

        cli("run-foreground", "--url", url, "--db", str(db1),
            "--max-attempts", "3", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db1, "02_after_reconnect")
        print(">> 重连请求头带 Last-Event-ID: streamA:3；4/5/6 完整入账，无缺号")

        # ---------------------------------------------------------------- #
        section("场景 2：完整消息重发 -> 内容一致自动去重（不重复入账）")
        # 再跑一轮：服务端依据 Last-Event-ID 无新数据时重放最后事件，
        # 或重新从断点后开始；重放内容与库内一致，只增 duplicate_count。
        before = json.loads((DEMO / "02_after_reconnect" / "status.json").read_text())
        cli("run-foreground", "--url", url, "--db", str(db1),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db1, "03_after_resend")
        after = json.loads((DEMO / "03_after_resend" / "status.json").read_text())
        print(f">> readings 行数保持 {after['counts']['readings']}（未新增）；"
              f"重复数 {before['streams'][0]['duplicate_count']} -> "
              f"{after['streams'][0]['duplicate_count']}")

        # ---------------------------------------------------------------- #
        section("场景 3a：进程在完整事件接收后、数据库提交前被杀死（SIGKILL 语义）")
        db2 = DEMO / "crash_pre_commit.db"
        url_n = f"{BASE}?case=normal&client=demo-crash-pre"
        p = run([sys.executable, "-m", "collector.cli", "run-foreground",
                 "--url", url_n, "--db", str(db2), "--max-attempts", "1",
                 "--read-size", "3"],
                env={"COLLECTOR_CRASH_AFTER_EVENT": "3"}, check=False)
        print(f">> 子进程退出码 {p.returncode}（137=模拟 SIGKILL，无清理）")
        snapshot(db2, "04_pre_commit_crash")
        print(">> 事件 3 未提交：readings 只到 2，断点 streamA:2")

        cli("run-foreground", "--url", url_n, "--db", str(db2),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db2, "05_pre_commit_recovered")
        print(">> 重启后上游重放事件 3，正常入账；最终 1..6 连续无重复")

        section("场景 3b：进程在事务提交成功后被杀死")
        db3 = DEMO / "crash_post_commit.db"
        url_p = f"{BASE}?case=partial&client=demo-crash-post"
        p = run([sys.executable, "-m", "collector.cli", "run-foreground",
                 "--url", url_p, "--db", str(db3), "--max-attempts", "1",
                 "--read-size", "3"],
                env={"COLLECTOR_CRASH_AFTER_COMMIT": "3"}, check=False)
        print(f">> 子进程退出码 {p.returncode}")
        snapshot(db3, "06_post_commit_crash")
        print(">> 事件 3 已提交：readings 含 1..3，断点 streamA:3")

        cli("run-foreground", "--url", url_p, "--db", str(db3),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db3, "07_post_commit_recovered")
        print(">> 重启用 streamA:3 续接；重放的事件内容一致 -> 去重，无重复入账")

        # ---------------------------------------------------------------- #
        section("场景 4：冲突（同序号不同内容）-> 停止流、保留原始、人工恢复")
        db4 = DEMO / "conflict.db"
        url_c = f"{BASE}?case=conflict&client=demo-conflict"
        cli("run-foreground", "--url", url_c, "--db", str(db4),
            "--max-attempts", "1", "--read-size", "3")
        cli("run-foreground", "--url", url_c, "--db", str(db4),
            "--max-attempts", "3", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db4, "08_conflict")
        st = json.loads((DEMO / "08_conflict" / "status.json").read_text())
        inc = st["open_incidents"][0]
        print(f">> 流状态 {st['streams'][0]['state']}；原始记录 value=正常 未被改写覆盖")
        cli("recover", "conflict", "--db", str(db4),
            "--incident", str(inc["id"]), "--note", "演练：保留原值")
        cli("run-foreground", "--url", url_c, "--db", str(db4),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db4, "09_conflict_recovered")

        # ---------------------------------------------------------------- #
        section("场景 5：410 保留窗口缺口 -> 显式缺口，人工选择恢复入口")
        db5 = DEMO / "expired.db"
        url_e = f"{BASE}?case=expired&client=demo-expired"
        cli("run-foreground", "--url", url_e, "--db", str(db5),
            "--max-attempts", "1", "--read-size", "3")
        cli("run-foreground", "--url", url_e, "--db", str(db5),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db5, "10_retention_gap")
        st = json.loads((DEMO / "10_retention_gap" / "status.json").read_text())
        inc = st["open_incidents"][0]
        print(f">> 状态 {st['streams'][0]['state']}，incident kind={inc['kind']}；"
              f"客户端拒绝偷偷续接")
        # 人工选择：接受缺口，从上游当前可用位置重拉（新 client 名避免再次 410）。
        cli("recover", "gap", "--db", str(db5),
            "--incident", str(inc["id"]), "--accept-restart",
            "--note", "演练：接受缺口，不宣称连续")
        url_e2 = f"{BASE}?case=expired&client=demo-expired-resume"
        cli("run-foreground", "--url", url_e2, "--db", str(db5),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db5, "11_gap_accepted")

        # ---------------------------------------------------------------- #
        section("场景 6：流标识改变（rotate）-> 不沿用旧流序号，人工确认换流")
        db6 = DEMO / "rotate.db"
        url_r = f"{BASE}?case=rotate&client=demo-rotate"
        cli("run-foreground", "--url", url_r, "--db", str(db6),
            "--max-attempts", "1", "--read-size", "3")
        cli("run-foreground", "--url", url_r, "--db", str(db6),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db6, "12_rotation")
        st = json.loads((DEMO / "12_rotation" / "status.json").read_text())
        inc = st["open_incidents"][0]
        print(f">> streamA={st['streams'][0]['state']}，等待人工确认新流")
        cli("recover", "rotate", "--db", str(db6),
            "--incident", str(inc["id"]), "--note", "演练：确认 streamB")
        cli("run-foreground", "--url", url_r, "--db", str(db6),
            "--max-attempts", "3", "--read-size", "3",
            "--initial-backoff", "0.1", "--max-backoff", "0.5")
        snapshot(db6, "13_rotation_adopted")
        print(">> streamB 序号独立从 1 开始；streamA 保留为历史停止流")

        section("守护进程入口演示：start / status / stop")
        db7 = DEMO / "daemon.db"
        pidfile = DEMO / "collector.pid"
        logfile = DEMO / "collector.log"
        url_d = f"{BASE}?case=normal&client=demo-daemon"
        cli("start", "--url", url_d, "--db", str(db7),
            "--pidfile", str(pidfile), "--log", str(logfile),
            "--max-attempts", "2", "--read-size", "3",
            "--initial-backoff", "0.2", "--max-backoff", "1")
        time.sleep(3)
        cli("status", "--db", str(db7))
        cli("stop", "--pidfile", str(pidfile))
        snapshot(db7, "14_daemon")
        print(f">> 守护日志见 {logfile}")

        section(f"演练完成。核对材料目录：{DEMO}")
        print("每个编号目录含 status.json / requests.json / readings.json / incidents.json")
    finally:
        fixture.send_signal(signal.SIGTERM)
        fixture.wait(timeout=10)


if __name__ == "__main__":
    main()
