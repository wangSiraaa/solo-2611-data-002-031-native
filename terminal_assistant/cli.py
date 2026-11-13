"""Command-line entry point for the PTY legacy entry assistant."""
from __future__ import annotations

import argparse
import sys

from .orchestrator import Assistant


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pty-entry-assistant",
        description="通过真实 PTY 录入并回读旧系统商品资料（只观察屏幕，不读数据库）",
    )
    p.add_argument("--csv", required=True, help="变更 CSV（catalog_code,name,unit,price,expected_old_name）")
    p.add_argument("--db", required=True, help="旧系统 SQLite 路径（仅传给应用程序，助手不读取）")
    p.add_argument("--faults", required=True, help="一次性故障控制文件路径（仅传给应用程序）")
    p.add_argument(
        "--app",
        default="fixtures/legacy_app.py",
        help="旧系统程序路径（默认 fixtures/legacy_app.py）",
    )
    p.add_argument("--cwd", default=None, help="启动应用程序的工作目录")
    p.add_argument("--report", default=None, help="报告输出 JSON 路径")
    p.add_argument("--raw-dir", default=None, help="原始 ANSI 审计记录目录（不参与判断）")
    p.add_argument("--plan-only", action="store_true", help="只校验并生成计划，不进入编辑")
    p.add_argument("--yes", action="store_true", help="非交互确认计划（用于脚本/演示）")
    p.add_argument("--resume", default=None, help="上一份报告；只处理其中未确认项")
    p.add_argument(
        "--retry-policy",
        choices=("safe-old-values", "manual"),
        default="safe-old-values",
        help="断线后回读仍为旧值且修订号未变时的重试策略（manual=不自动重试）",
    )
    p.add_argument("--max-crash-retries", type=int, default=1, help="断线后按策略重试的最多次数")
    p.add_argument("--query-attempts", type=int, default=2, help="查询暂不可用/断线时的最多尝试次数")
    p.add_argument("--retry-delay", type=float, default=0.25, help="查询重试间隔秒数")
    p.add_argument("--settle", type=float, default=0.35, help="屏幕输出静止去抖秒数")
    p.add_argument("--read-timeout", type=float, default=10.0, help="单次读取超时秒数")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_crash_retries < 0:
        print("--max-crash-retries 不能为负", file=sys.stderr)
        return 2
    if args.query_attempts < 1:
        print("--query-attempts 至少为 1", file=sys.stderr)
        return 2
    assistant = Assistant(args)
    try:
        return assistant.run()
    finally:
        assistant.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
