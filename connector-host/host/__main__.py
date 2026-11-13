#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主命令行入口。

用法：
    python -m host --config config/host.json list-sources [--connector NAME]
    python -m host --config config/host.json query --connector NAME --source S
           [--fields a,b,c] [--filter 'region:eq=华北']... [--no-wait]
    python -m host --config config/host.json cancel --query-id q-xxxx
    python -m host --config config/host.json status [--json]
    python -m host --config config/host.json restart --connector NAME
    python -m host --config config/host.json export --out runs/manifest.json
    python -m host --config config/host.json shell
    python -m host --config config/host.json run-script scripts/demo_commands.txt

过滤条件语法（受控）：--filter '<字段>:<操作符>=<JSON 值>'，
例如 --filter 'region:eq="华北"' --filter 'amount:gt=100'。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .app import HostApp, HostError


def parse_filter(tokens: list[str]) -> dict:
    filters: dict = {}
    for token in tokens:
        if ":" not in token or "=" not in token:
            raise SystemExit(f"bad --filter {token!r}, expected field:op=json-value")
        field_name, rest = token.split(":", 1)
        op, raw_value = rest.split("=", 1)
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        filters[field_name] = {op: value}
    return filters


async def run_one(app: HostApp, args: argparse.Namespace) -> object:
    if args.command == "list-sources":
        return await app.list_sources(args.connector)
    if args.command == "query":
        return await app.submit_query(
            connector=args.connector,
            source=args.source,
            fields=[f for f in args.fields.split(",") if f] if args.fields else [],
            filters=parse_filter(args.filter or []),
            wait=not args.no_wait,
        )
    if args.command == "cancel":
        return await app.cancel_query(args.query_id)
    if args.command == "status":
        return await app.status()
    if args.command == "restart":
        return await app.restart(args.connector)
    if args.command == "export":
        return await app.export(args.out)
    if args.command == "run-script":
        return await run_script(app, args.path, args.config)
    if args.command == "shell":
        return await interactive_shell(app, args.config)
    raise HostError(f"unknown command {args.command}")


async def run_script(app: HostApp, path: str, config_path: str) -> list[object]:
    results = []
    with open(path, "r", encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
    for line in lines:
        print(f"\n$ {line}", file=sys.stderr)
        # 脚本行只含子命令，自动继承全局 --config。
        argv = ["--config", config_path, *line.split()]
        args = build_parser().parse_args(argv)
        try:
            results.append(await run_one(app, args))
        except HostError as exc:
            results.append({"error": str(exc)})
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
    return results


async def interactive_shell(app: HostApp, config_path: str) -> None:
    print("宿主 REPL；输入 help 查看命令，exit 退出（会优雅关闭并回收子进程）。")
    while True:
        try:
            line = input("host> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        if line == "help":
            print(
                "list-sources [--connector N]\n"
                "query --connector N --source S [--fields a,b] [--filter f:op=v] [--no-wait]\n"
                "cancel --query-id Q\nstatus\nrestart --connector N\nexport --out PATH"
            )
            continue
        try:
            # 交互行只含子命令，自动继承全局 --config。
            args = build_parser().parse_args(["--config", config_path, *line.split()])
            result = await run_one(app, args)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        except SystemExit:
            pass
        except HostError as exc:
            print(f"error: {exc}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="host")
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-sources").add_argument("--connector")

    q = sub.add_parser("query")
    q.add_argument("--connector", required=True)
    q.add_argument("--source", required=True)
    q.add_argument("--fields", default="")
    q.add_argument("--filter", action="append")
    q.add_argument("--no-wait", action="store_true")

    c = sub.add_parser("cancel")
    c.add_argument("--query-id", required=True)

    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")

    r = sub.add_parser("restart")
    r.add_argument("--connector", required=True)

    e = sub.add_parser("export")
    e.add_argument("--out", required=True)

    sub.add_parser("shell")

    rs = sub.add_parser("run-script")
    rs.add_argument("path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    async def _main() -> int:
        app = HostApp(args.config)
        await app.start_all()
        try:
            result = await run_one(app, args)
            if result is not None and args.command != "shell":
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        except HostError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        finally:
            await app.shutdown()

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
