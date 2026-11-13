"""命令行入口：export / apply / verify 三个子命令。

连接参数优先取 --dsn，其次回退到 libpq 标准环境变量（PGHOST/PGPORT/PGDATABASE/PGUSER）。
"""

import argparse
import json
import sys

from .applier import apply_manifest
from .errors import ApplyError, ExportError, UnsupportedDependency
from .exporter import load_request, run_export, write_outputs
from .verifier import VerificationFailure, verify


def _default_dsn():
    import os

    host = os.environ.get("PGHOST", "/tmp/pg_task_sock")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "taskdb")
    user = os.environ.get("PGUSER", os.environ.get("USER", "node"))
    return f"host={host} port={port} dbname={db} user={user}"


def _load_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cmd_export(args):
    request = load_request(args.request)
    manifest = run_export(args.dsn, request)
    manifest_path, sql_path = write_outputs(manifest, args.out_dir)

    print("== 结构清单 ==")
    for item in manifest["structure_inventory"]:
        print(f"  [{item['kind']:<18}] {item['source']}")
    print("\n== 依赖边 ==")
    for edge in manifest["dependency_edges"]:
        print(f"  {edge['object']}  --{edge['deptype']}-->  {edge['referenced']}")
    print("\n== Schema 引用改写点（AST 级，字符串常量不在此列） ==")
    for ref in manifest["remapped_references"]:
        print(f"  {ref['context']}: {ref['kind']} -> {ref['name']}")
    print(f"\n对象计数：{manifest['object_count_by_kind']}")
    print(f"已写出：{manifest_path}\n        {sql_path}")
    return 0


def cmd_apply(args):
    manifest = _load_manifest(args.manifest)
    try:
        apply_manifest(args.dsn, manifest, manifest["target_schema"])
    except ApplyError as exc:
        print(f"应用失败并已整体回滚：\n  对象：{exc.failed_object}\n  原因：{exc.reason}",
              file=sys.stderr)
        print(f"  失败 DDL：{exc.sql}", file=sys.stderr)
        return 2
    print(f"已在事务中应用全部 {len(manifest['ddl'])} 条 DDL，目标 schema：{manifest['target_schema']}")
    return 0


def cmd_verify(args):
    manifest = _load_manifest(args.manifest)
    try:
        report = verify(args.dsn, manifest)
    except VerificationFailure as exc:
        print("验证失败：", exc, file=sys.stderr)
        return 3
    for check in report["checks"]:
        print(f"  [PASS] {check['message']}")
    if report["smoke"]:
        print("\n== 冒烟测试（事务已回滚，不留测试行） ==")
        print(json.dumps(report["smoke"], ensure_ascii=False, indent=2, default=str))
    print("\n全部验证通过。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pgview_export",
        description="从 pg_catalog 导出视图依赖闭包并迁移到目标 schema（受限结构迁移工具）",
    )
    parser.add_argument("--dsn", default=_default_dsn(), help="libpq 连接串")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="读取 request 生成清单与 DDL 文件")
    p_export.add_argument("--request", default="fixtures/request.json")
    p_export.add_argument("--out-dir", default="out")
    p_export.set_defaults(func=cmd_export)

    p_apply = sub.add_parser("apply", help="单事务应用导出的 DDL")
    p_apply.add_argument("--manifest", default="out/export_manifest.json")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="验证目标 schema 并做冒烟测试")
    p_verify.add_argument("--manifest", default="out/export_manifest.json")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UnsupportedDependency as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ExportError as exc:
        print(f"导出中止：{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
