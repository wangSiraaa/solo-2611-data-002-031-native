"""命令行入口：serve / query / export / status / cancel / check-db / demo。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp

from . import catalog as catalog_mod
from .config import Config, RUNTIME_DIR
from .values import decode_value
from .webapp import run_server


def _load_params(path: str | None, inline: str | None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if inline:
        return json.loads(inline)
    return {}


async def _stream_query(
    base_url: str,
    query_id: str,
    params: dict[str, Any],
    *,
    pace_ms: float = 0.0,
    abort_after_rows: int | None = None,
    sink_file: Path | None = None,
    print_rows: bool = True,
) -> dict[str, Any]:
    """实际并发客户端：逐帧读取 NDJSON，可模拟慢读与中途断连。"""
    sink = sink_file.open("w", encoding="utf-8") if sink_file else None
    summary: dict[str, Any] = {
        "frames": 0,
        "rows": 0,
        "columns": None,
        "terminal": None,
        "completed": False,
        "aborted": False,
        "transport_error": None,
    }
    try:
        async with aiohttp.ClientSession(base_url=base_url, timeout=aiohttp.ClientTimeout(total=None)) as session:
            try:
                async with session.post(
                    "/query", json={"query_id": query_id, "params": params}
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {body}")
                    request_id = None
                    async for raw in resp.content:
                        frame = json.loads(raw.decode("utf-8"))
                        summary["frames"] += 1
                        ftype = frame["type"]
                        if ftype == "meta":
                            request_id = frame["request_id"]
                            summary["request_id"] = request_id
                            summary["columns"] = frame["columns"]
                            if sink:
                                sink.write(json.dumps(frame, ensure_ascii=False) + "\n")
                        elif ftype == "rows":
                            decoded_rows = [[decode_value(v) for v in row] for row in frame["rows"]]
                            summary["rows"] += len(decoded_rows)
                            if sink:
                                sink.write(json.dumps(frame, ensure_ascii=False) + "\n")
                            if print_rows:
                                for row in decoded_rows[:3]:
                                    print(json.dumps(row, default=str, ensure_ascii=False))
                                if len(decoded_rows) > 3:
                                    print(f"... 本帧 {len(decoded_rows)} 行")
                            if pace_ms:
                                # 逐帧（每批）停顿，真实制造慢 socket，驱动服务端背压
                                await asyncio.sleep(pace_ms / 1000.0)
                            if abort_after_rows is not None and summary["rows"] >= abort_after_rows:
                                summary["aborted"] = True
                                summary["transport_error"] = "client_aborted_after_rows"
                                return summary
                        elif ftype == "terminal":
                            summary["terminal"] = frame
                            summary["completed"] = frame["status"] == "success"
                            if sink:
                                sink.write(json.dumps(frame, ensure_ascii=False) + "\n")
                            return summary
            except (aiohttp.ClientError, asyncio.CancelledError, RuntimeError) as exc:
                summary["transport_error"] = repr(exc)
                return summary
    finally:
        if sink:
            sink.close()
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    term = summary.get("terminal")
    print("\n==== 客户端结果摘要 ====")
    print(f"request_id     : {summary.get('request_id')}")
    print(f"收到数据行     : {summary['rows']}")
    print(f"收到帧数       : {summary['frames']}")
    if term is None:
        print("终态帧         : 未收到 -> 结果未完成（中断/拒绝）")
        print(f"中断原因       : {summary.get('transport_error') or summary.get('aborted')}")
        print("客户端标记     : INCOMPLETE（只有读到 success 终态才允许标记完成）")
    else:
        print(f"终态           : {term['status']}")
        print(f"服务端行数统计 : {term.get('rows_sent')}")
        print(f"服务端字节统计 : {term.get('bytes_sent')}")
        if term.get("detail"):
            print(f"终态详情       : {term['detail']}")
        print(f"客户端标记     : {'COMPLETE' if summary['completed'] else 'INCOMPLETE'}")


async def cmd_query(args: argparse.Namespace) -> int:
    params = _load_params(args.params_file, args.params)
    summary = await _stream_query(
        args.base_url,
        args.query_id,
        params,
        pace_ms=args.pace_ms,
        abort_after_rows=args.abort_after_rows,
        print_rows=not args.quiet,
    )
    _print_summary(summary)
    return 0 if summary["completed"] else 2


async def cmd_export(args: argparse.Namespace) -> int:
    params = _load_params(args.params_file, args.params)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = await _stream_query(
        args.base_url,
        args.query_id,
        params,
        sink_file=out,
        print_rows=False,
        pace_ms=args.pace_ms,
        abort_after_rows=args.abort_after_rows,
    )
    manifest = {
        "output": str(out),
        "completed": summary["completed"],
        "rows": summary["rows"],
        "terminal": summary["terminal"],
        "transport_error": summary["transport_error"],
    }
    manifest_path = out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(summary)
    print(f"结果帧文件     : {out}")
    print(f"清单文件       : {manifest_path}")
    return 0 if summary["completed"] else 2


async def cmd_status(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        url = f"{args.base_url}/status"
        if args.request_id:
            url = f"{args.base_url}/requests/{args.request_id}/status"
        async with session.get(url) as resp:
            print(json.dumps(await resp.json(), ensure_ascii=False, indent=2))
    return 0


async def cmd_cancel(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{args.base_url}/requests/{args.request_id}/cancel") as resp:
            print(json.dumps(await resp.json(), ensure_ascii=False, indent=2))
            return 0 if resp.status == 200 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qgateway", description="本地 HTTP 查询网关")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动网关")
    p_serve.add_argument("--pool-size", type=int, default=3)
    p_serve.add_argument("--wait-queue-limit", type=int, default=2)
    p_serve.add_argument("--frame-queue-limit", type=int, default=4)
    p_serve.add_argument("--fetch-block-rows", type=int, default=200)
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--dsn", default=None)
    p_serve.add_argument("--log-file", default=str(RUNTIME_DIR / "gateway.log"))
    p_serve.set_defaults(func=cmd_serve)

    def add_query_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "query_id",
            choices=sorted(item["query_id"] for item in catalog_mod.catalog_snapshot()),
        )
        p.add_argument("--params", help="内联 JSON 参数")
        p.add_argument("--params-file", help="JSON 参数文件")
        p.add_argument("--pace-ms", type=float, default=0.0, help="每帧停顿毫秒数，模拟慢客户端")
        p.add_argument("--abort-after-rows", type=int, default=None, help="读到指定行数后主动断连")

    p_query = sub.add_parser("query", help="提交查询并逐帧读取")
    add_query_args(p_query)
    p_query.add_argument("--quiet", action="store_true")
    p_query.set_defaults(func=cmd_query)

    p_export = sub.add_parser("export", help="提交查询并导出 NDJSON 结果与清单")
    add_query_args(p_export)
    p_export.add_argument("--output", required=True)
    p_export.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status", help="查看服务/请求状态")
    p_status.add_argument("request_id", nargs="?")
    p_status.set_defaults(func=cmd_status)

    p_cancel = sub.add_parser("cancel", help="取消指定请求")
    p_cancel.add_argument("request_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_check = sub.add_parser("check-db", help="独立直连数据库核对结果（见 demo 模块）")
    p_check.add_argument("--dsn", default=None)
    p_check.add_argument("--export-file", required=True)
    p_check.set_defaults(func=cmd_check_db)

    p_demo = sub.add_parser("demo", help="可重复的综合负载演示与验收")
    p_demo.add_argument("--pool-size", type=int, default=3)
    p_demo.add_argument("--wait-queue-limit", type=int, default=2)
    p_demo.add_argument("--port", type=int, default=8080)
    p_demo.add_argument("--no-spawn", action="store_true", help="不自行拉起服务与数据库，复用已运行实例")
    p_demo.set_defaults(func=cmd_demo)

    return parser


async def cmd_serve(args: argparse.Namespace) -> int:
    config = Config(
        dsn=args.dsn or Config().dsn,
        port=args.port,
        pool_size=args.pool_size,
        wait_queue_limit=args.wait_queue_limit,
        frame_queue_limit=args.frame_queue_limit,
        fetch_block_rows=args.fetch_block_rows,
        log_path=Path(args.log_file),
    )
    await run_server(config)
    return 0


async def cmd_check_db(args: argparse.Namespace) -> int:
    from .demo_check import check_export_against_database

    code = await check_export_against_database(args.dsn or Config().dsn, Path(args.export_file))
    return code


async def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return await run_demo(
        pool_size=args.pool_size,
        wait_queue_limit=args.wait_queue_limit,
        port=args.port,
        spawn=not args.no_spawn,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
