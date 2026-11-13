"""命令行入口：bootstrap / load / apply / query / verify / generate。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import batches as bmod
from . import generator, queries, service
from .db import DEFAULT_DSN, bootstrap as db_bootstrap, resolve_dsn
from .errors import IncrementalJoinError
from .verify import verify


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _add_dsn(parser: argparse.ArgumentParser) -> None:
    """兼容占位：--dsn 已提升为全局参数，子解析器无需重复定义。"""
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incjoin",
        description="增量连接物化 CLI：局部维护连接明细与按 join_key 的汇总",
    )
    # 全局参数：子命令前后均可，默认 DSN 也可由 INCJOIN_DSN 提供
    parser.add_argument(
        "--dsn",
        default=None,
        help=f"PostgreSQL DSN，默认 {DEFAULT_DSN}，也可用 INCJOIN_DSN",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_boot = sub.add_parser("bootstrap", help="建库（如需）并应用迁移")
    _add_dsn(p_boot)

    p_load = sub.add_parser("load", help="整快照全量替换源表并重建物化结果")
    _add_dsn(p_load)
    p_load.add_argument("--left", required=True, help="左表快照 JSON 数组文件")
    p_load.add_argument("--right", required=True, help="右表快照 JSON 数组文件")

    p_apply = sub.add_parser("apply", help="应用一个批次 JSON（事务、幂等）")
    _add_dsn(p_apply)
    p_apply.add_argument("batch_file", help="批次 JSON 文件（含 batch_id/operations）")

    p_query = sub.add_parser("query", help="只读查询并输出 JSON")
    _add_dsn(p_query)
    p_query.add_argument(
        "what", choices=["detail", "summary", "left", "right", "receipts"]
    )
    p_query.add_argument("--key", default=None, help="可选：按 join_key 过滤")

    p_verify = sub.add_parser(
        "verify", help="完整 SQL JOIN 重算并逐行比对（仅用于验证）"
    )
    _add_dsn(p_verify)

    p_gen = sub.add_parser("generate", help="生成合成快照、场景批次与随机批次")
    p_gen.add_argument("out_dir", help="输出目录")

    return parser


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    db_bootstrap(args.dsn)
    _emit({"status": "ok", "dsn": resolve_dsn(args.dsn),
           "message": "数据库与表结构已就绪"})
    return 0


def _cmd_load(args: argparse.Namespace) -> int:
    db_bootstrap(args.dsn)
    left = generator.validate_snapshot_rows(_read_json(args.left), "L")
    right = generator.validate_snapshot_rows(_read_json(args.right), "R")
    result = service.load_snapshot(args.dsn, left, right)
    report = verify(args.dsn)
    _emit({"status": "loaded", **result, "verify": report.as_dict()})
    return 0 if report.ok else 2


def _cmd_apply(args: argparse.Namespace) -> int:
    db_bootstrap(args.dsn)
    raw = Path(args.batch_file).read_text(encoding="utf-8")
    doc = bmod.load_batch_document(raw)  # 静态非法输入在此拒绝
    result = service.apply_batch(args.dsn, doc)
    report = verify(args.dsn)
    _emit({
        "status": result["status"],
        "batch_id": doc["batch_id"],
        "content_hash": bmod.content_hash(doc),
        "affected_keys": result["affected_keys"],
        "verify": report.as_dict(),
    })
    return 0 if report.ok else 2


def _cmd_query(args: argparse.Namespace) -> int:
    if args.what == "detail":
        _emit(queries.query_detail(args.dsn, args.key))
    elif args.what == "summary":
        _emit(queries.query_summary(args.dsn, args.key))
    elif args.what in ("left", "right"):
        _emit(queries.query_source(args.dsn, args.what.upper()))
    else:
        _emit(queries.query_receipts(args.dsn))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify(args.dsn)
    _emit(report.as_dict())
    return 0 if report.ok else 2


def _cmd_generate(args: argparse.Namespace) -> int:
    written = generator.generate_workspace(args.out_dir)
    _emit({"status": "generated", "out_dir": args.out_dir, "files": written})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "bootstrap": _cmd_bootstrap,
        "load": _cmd_load,
        "apply": _cmd_apply,
        "query": _cmd_query,
        "verify": _cmd_verify,
        "generate": _cmd_generate,
    }
    try:
        return handlers[args.command](args)
    except IncrementalJoinError as exc:
        _emit({"status": "error", "error": str(exc)})
        return 1
    except FileNotFoundError as exc:
        _emit({"status": "error", "error": f"文件不存在: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
