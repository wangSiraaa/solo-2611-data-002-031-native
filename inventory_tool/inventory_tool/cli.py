"""库存预占工具命令行入口.

子命令:
  init        建表迁移 + 导入初始库存 CSV
  run         执行 JSONL 请求, 每条请求输出一行终态结果
  order       查询订单状态 / 明细 / 关联请求
  reconcile   库存对账 (表值 vs 流水重算 vs RESERVED 订单之和), 按 SKU 排序
  stock       查看库存与可用量, 按 SKU 排序
  requests    查看请求台账
  ledger      查看逐商品流水
  generate    生成合成 stock.csv 与 requests.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import psycopg2

from . import repository as repo
from .config import resolve_dsn
from .generator import generate_dataset
from .service import connect, process_request
from .validation import ValidationError


def _read_stock_csv(path: Path) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "sku" not in reader.fieldnames \
                or "on_hand" not in reader.fieldnames:
            raise SystemExit(f"库存 CSV 必须包含 sku,on_hand 两列: {path}")
        for line_no, row in enumerate(reader, start=2):
            try:
                qty = int(row["on_hand"])
            except (TypeError, ValueError):
                raise SystemExit(f"第 {line_no} 行库存不是整数: {row['on_hand']!r}")
            rows.append((row["sku"], qty))
    return rows


def cmd_init(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        if args.reset:
            repo.reset_all(conn)
        applied = repo.run_migrations(conn)
        rows = _read_stock_csv(Path(args.stock_csv))
        count = repo.load_initial_stock(conn, rows, upsert=args.reset)
    finally:
        conn.close()
    print(json.dumps({
        "migrations": applied,
        "stock_loaded": count,
        "reset": bool(args.reset),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    out_fh = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    validation_errors = 0
    try:
        with open(args.input, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    outcome = process_request(conn, raw)
                except json.JSONDecodeError as exc:
                    validation_errors += 1
                    outcome_obj = {
                        "line_no": line_no,
                        "success": False,
                        "result_code": "MALFORMED_JSON",
                        "message": f"JSON 解析失败: {exc}",
                    }
                except ValidationError as exc:
                    validation_errors += 1
                    outcome_obj = {
                        "line_no": line_no,
                        "success": False,
                        "result_code": "VALIDATION_ERROR",
                        "message": str(exc),
                    }
                else:
                    outcome_obj = outcome.to_dict()
                    outcome_obj["line_no"] = line_no
                out_fh.write(json.dumps(outcome_obj, ensure_ascii=False) + "\n")
                out_fh.flush()
    finally:
        if args.output:
            out_fh.close()
        conn.close()
    if validation_errors:
        print(f"完成, 其中 {validation_errors} 条输入格式错误", file=sys.stderr)
        return 1
    return 0


def cmd_order(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        order = repo.get_order(conn, args.order_id)
    finally:
        conn.close()
    if order is None:
        print(json.dumps(
            {"order_id": args.order_id, "found": False}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(order, ensure_ascii=False, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        report = repo.reconcile(conn)
    finally:
        conn.close()
    ok = all(r["on_hand_match"] and r["reserved_match"] and r["orders_match"]
             for r in report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"对账 SKU 数={len(report)}, 全部一致={ok}", file=sys.stderr)
    return 0 if ok else 3


def cmd_stock(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        rows = repo.list_stock(conn)
    finally:
        conn.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        rows = repo.list_requests(conn)
    finally:
        conn.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    conn = connect(resolve_dsn())
    try:
        rows = repo.list_ledger(conn)
    finally:
        conn.close()
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    paths = generate_dataset(
        args.out_dir, seed=args.seed,
        sku_count=args.sku_count, order_count=args.order_count,
    )
    print(json.dumps({k: str(v) for k, v in paths.items()},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inventory-tool",
        description="本地库存预占工具 (PostgreSQL 16, 全量整数, 事务+流水)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="执行迁移并导入初始库存")
    p_init.add_argument("stock_csv", help="初始库存 CSV, 列: sku,on_hand")
    p_init.add_argument("--reset", action="store_true",
                        help="清空全部业务表后重新导入")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="执行 JSONL 请求文件")
    p_run.add_argument("input", help="请求 JSONL 路径")
    p_run.add_argument("-o", "--output", help="结果 JSONL 输出路径 (默认 stdout)")
    p_run.set_defaults(func=cmd_run)

    p_order = sub.add_parser("order", help="查询订单")
    p_order.add_argument("order_id")
    p_order.set_defaults(func=cmd_order)

    sub.add_parser("reconcile", help="库存对账").set_defaults(func=cmd_reconcile)
    sub.add_parser("stock", help="查看库存").set_defaults(func=cmd_stock)
    sub.add_parser("requests", help="查看请求台账").set_defaults(func=cmd_requests)
    sub.add_parser("ledger", help="查看逐商品流水").set_defaults(func=cmd_ledger)

    p_gen = sub.add_parser("generate", help="生成合成输入")
    p_gen.add_argument("out_dir")
    p_gen.add_argument("--seed", type=int, default=20261113)
    p_gen.add_argument("--sku-count", type=int, default=8)
    p_gen.add_argument("--order-count", type=int, default=12)
    p_gen.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (psycopg2.Error, RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
