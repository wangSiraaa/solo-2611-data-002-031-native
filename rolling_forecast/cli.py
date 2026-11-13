"""命令行入口：run / explain / verify。"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .backtest import run_backtest
from .config import load_plan
from .evaluate import evaluate_predictions
from .io_data import load_calendar, load_sales
from .reporting import write_outputs
from .verify import run_verification


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rolling_forecast",
        description="点-in-time 滚动原点销量预测（不建在线服务，纯离线批处理）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行逐折预测、评价并写出 CSV/JSON/HTML")
    p_run.add_argument("--sales", required=True, help="sales.csv 路径")
    p_run.add_argument("--calendar", required=True, help="calendar.csv 路径")
    p_run.add_argument("--plan", required=True, help="plan.json 路径")
    p_run.add_argument("--out", required=True, help="输出目录")

    p_explain = sub.add_parser("explain", help="打印方法学说明（多步策略、回退、资格规则）")
    p_explain.add_argument("--out", help="可选：同时把说明写到该目录的 explain.md")

    p_verify = sub.add_parser(
        "verify", help="独立核验（朴素手算、滞后来源、防泄漏、重排/缺测/冲突）"
    )
    p_verify.add_argument("--sales", default="fixtures/sales.csv")
    p_verify.add_argument("--calendar", default="fixtures/calendar.csv")
    p_verify.add_argument("--plan", default="fixtures/plan.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "explain":
        from .explain import methodology_markdown

        text = methodology_markdown()
        print(text)
        if args.out:
            from pathlib import Path

            Path(args.out).mkdir(parents=True, exist_ok=True)
            (Path(args.out) / "explain.md").write_text(text, encoding="utf-8")
        return 0

    if args.command == "verify":
        return run_verification(args.sales, args.calendar, args.plan)

    # command == "run"
    plan = load_plan(args.plan)
    sales = load_sales(args.sales)
    calendar = load_calendar(args.calendar)

    predictions, fold_evidence = run_backtest(sales, calendar, plan)
    predictions, metrics, reasons = evaluate_predictions(predictions, sales, plan)
    write_outputs(
        out_dir=args.out,
        plan=plan,
        sales_path=args.sales,
        calendar_path=args.calendar,
        plan_path=args.plan,
        predictions=predictions,
        fold_evidence=fold_evidence,
        metrics=metrics,
        reasons=reasons,
    )
    print(f"[run] 完成，输出目录：{args.out}")
    print(f"[run] 预测行 {len(predictions)} 条；折证据 {len(fold_evidence)} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
