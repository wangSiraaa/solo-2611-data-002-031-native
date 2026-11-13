"""命令行入口：init / change / ancestors / descendants / verify / synthetic。

约定
----
- 图文件 JSON：{"nodes": [1,2,...], "edges": [[u,v],...]}
- 批次文件 JSON：{"operation_id": "...", "operations": [
      {"seq": 1, "op": "add", "prerequisite": 1, "dependent": 2}, ...]}
- 成功输出统一打印到 stdout，一条 JSON；错误打印到 stderr，退出码非 0：
    2 输入/批次被拒绝（含未知节点、自环、环——环路径随 JSON 给出）
    3 幂等冲突（operation_id 换了内容）
    4 其余工具/数据库错误
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import db, repo, synthetic
from .repo import BatchRejected, DepgraphError, IdempotencyConflict


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _print(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _edges_from_payload(payload: Any) -> list[tuple[int, int]]:
    return [(int(u), int(v)) for u, v in payload.get("edges", [])]


def _open_conn(args: argparse.Namespace):
    conn = db.connect(args.dsn)
    db.run_migrations(conn)
    return conn


def cmd_init(args: argparse.Namespace) -> int:
    payload = _load_json(args.file)
    nodes = [int(n) for n in payload["nodes"]]
    edges = _edges_from_payload(payload)
    with _open_conn(args) as conn:
        result = repo.initialize_graph(conn, nodes, edges, reset=args.reset)
    _print({"command": "init", "result": result})
    return 0


def cmd_change(args: argparse.Namespace) -> int:
    payload = _load_json(args.file)
    operation_id = payload["operation_id"]
    with _open_conn(args) as conn:
        receipt = repo.apply_change(
            conn,
            operation_id,
            payload["operations"],
            crash_at=args.crash_at,
        )
    _print({"command": "change", "receipt": _jsonable(receipt)})
    return 0


def cmd_ancestors(args: argparse.Namespace) -> int:
    with _open_conn(args) as conn:
        result = repo.ancestors(conn, args.node_id)
    _print({"node_id": args.node_id, "ancestors": result})
    return 0


def cmd_descendants(args: argparse.Namespace) -> int:
    with _open_conn(args) as conn:
        result = repo.descendants(conn, args.node_id)
    _print({"node_id": args.node_id, "descendants": result})
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    with _open_conn(args) as conn:
        report = repo.verify(conn)
    _print({"command": "verify", "report": report})
    return 0 if report["ok"] else 1


def cmd_synthetic(args: argparse.Namespace) -> int:
    nodes, edges = synthetic.build(
        args.style,
        args.nodes,
        isolated=args.isolated,
        seed=args.seed,
        extra_edges=args.extra_edges,
    )
    payload = {
        "style": args.style,
        "nodes": nodes,
        "edges": [list(edge) for edge in edges],
        "isolated": nodes[len(nodes) - args.isolated:] if args.isolated else [],
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        _print({"written": args.out, "nodes": len(nodes),
                "edges": len(edges)})
    else:
        _print(payload)
    return 0


def _jsonable(obj: Any) -> Any:
    """把回执里的 tuple 对转为 list，便于 JSON 序列化。"""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="depgraph",
        description="本地工序依赖维护工具（直接边 + 物化可达闭包，PostgreSQL 16）",
    )
    parser.add_argument("--dsn", default=None,
                        help="PG 连接串，默认取 $DEPGRAPH_DSN 或本机 socket 配置")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化固定节点集合与初始 DAG")
    p_init.add_argument("file", help="图 JSON 文件，- 表示 stdin")
    p_init.add_argument("--reset", action="store_true",
                        help="清空已有业务数据后重建")
    p_init.set_defaults(func=cmd_init)

    p_change = sub.add_parser("change", help="提交一个变更批次")
    p_change.add_argument("file", help="批次 JSON 文件，- 表示 stdin")
    p_change.add_argument(
        "--crash-at",
        choices=sorted(repo.VALID_CRASH_POINTS),
        default=None,
        help="崩溃演练注入点（正常使用不要指定）",
    )
    p_change.set_defaults(func=cmd_change)

    p_anc = sub.add_parser("ancestors", help="查询节点全部祖先")
    p_anc.add_argument("node_id", type=int)
    p_anc.set_defaults(func=cmd_ancestors)

    p_desc = sub.add_parser("descendants", help="查询节点全部后代")
    p_desc.add_argument("node_id", type=int)
    p_desc.set_defaults(func=cmd_descendants)

    p_verify = sub.add_parser(
        "verify", help="独立遍历重建参考闭包并逐对核对物化表"
    )
    p_verify.set_defaults(func=cmd_verify)

    p_syn = sub.add_parser("synthetic", help="生成合成 DAG 数据（仅合成数据）")
    p_syn.add_argument("style", choices=["chain", "diamond", "layered"])
    p_syn.add_argument("nodes", type=int, help="节点总数，<=2000")
    p_syn.add_argument("--isolated", type=int, default=0, help="孤立节点数")
    p_syn.add_argument("--extra-edges", type=int, default=0)
    p_syn.add_argument("--seed", type=int, default=7)
    p_syn.add_argument("--out", default=None, help="写入文件而非 stdout")
    p_syn.set_defaults(func=cmd_synthetic)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BatchRejected as exc:
        _print({"error": "batch_rejected", "reason": exc.reason,
                "cycle": exc.cycle})
        return 2
    except IdempotencyConflict as exc:
        _print({"error": "idempotency_conflict", "reason": str(exc)})
        return 3
    except DepgraphError as exc:
        _print({"error": "depgraph_error", "reason": str(exc)})
        return 4
    except Exception as exc:  # 数据库等未预期错误
        print(json.dumps({"error": "fatal", "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
