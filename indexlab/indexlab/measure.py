"""测量：EXPLAIN (ANALYZE, BUFFERS) 计划归档、全参数组计时、结果一致性校验。"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from .config import Config, QuerySpec
from .db import to_pyformat


def _norm(v):
    if isinstance(v, Decimal):
        return str(v)
    return repr(v)


def result_hash(rows) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update("|".join(_norm(v) for v in row).encode())
        h.update(b"\n")
    return h.hexdigest()


def plan_index_names(plan_node) -> list[str]:
    """递归收集计划中实际使用的索引名与节点类型。"""
    found = []
    if isinstance(plan_node, dict):
        if "Index Name" in plan_node:
            found.append(plan_node["Index Name"])
        for k in ("Plans",):
            for sub in plan_node.get(k, []):
                found.extend(plan_index_names(sub))
    return found


def plan_node_types(plan_node) -> list[str]:
    types = []

    def walk(n):
        if isinstance(n, dict):
            if "Node Type" in n:
                types.append(n["Node Type"])
            for sub in n.get("Plans", []):
                walk(sub)

    walk(plan_node)
    return types


def explain_analyze(conn, q: QuerySpec, params: list) -> dict:
    sql, args = to_pyformat(q.sql, params)
    cur = conn.execute(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", args
    )
    return cur.fetchone()[0][0]


def run_one_timed(conn, q: QuerySpec, params: list) -> tuple[float, str]:
    sql, args = to_pyformat(q.sql, params)
    t0 = time.perf_counter()
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    dt = time.perf_counter() - t0
    return dt, result_hash(rows)


def measure_workload(cfg: Config, conn, out_dir: Path,
                     tag: str) -> dict:
    """对全部 (查询, 参数组) 做 warmup + repeats 次计时 + 1 次 EXPLAIN ANALYZE。

    返回 {qid: {group: {"times": [...], "hash": str, "plan_file": str,
                        "indexes_used": [...], "node_types": [...]}}}
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = out_dir / "plans"
    plan_dir.mkdir(exist_ok=True)
    m = cfg.measurement
    result: dict = {}

    # warmup：每个 (q,g) 跑一遍
    for q in cfg.queries:
        for g in q.param_groups:
            run_one_timed(conn, q, g["params"])

    # 计时：交错执行，降低缓存/噪声偏向
    times: dict[tuple[str, str], list[float]] = {}
    hashes: dict[tuple[str, str], str] = {}
    pairs = [(q, g) for q in cfg.queries for g in q.param_groups]
    for _rep in range(m["repeats"]):
        for q, g in pairs:
            dt, h = run_one_timed(conn, q, g["params"])
            times.setdefault((q.qid, g["name"]), []).append(dt)
            prev = hashes.setdefault((q.qid, g["name"]), h)
            if prev != h:
                raise RuntimeError(f"同一组合内结果不稳定: {q.qid}/{g['name']}")

    # 计划归档
    for q, g in pairs:
        plan = explain_analyze(conn, q, g["params"])
        pf = plan_dir / f"{q.qid}__{g['name']}.json"
        pf.write_text(json.dumps(plan, indent=1, default=str))
        root = plan["Plan"]
        result.setdefault(q.qid, {})[g["name"]] = {
            "times_ms": [round(t * 1000, 3) for t in times[(q.qid, g["name"])]],
            "hash": hashes[(q.qid, g["name"])],
            "plan_file": str(pf.relative_to(out_dir.parent)),
            "indexes_used": sorted(set(plan_index_names(root))),
            "node_types": plan_node_types(root),
            "plan_time_ms": round(root.get("Actual Total Time", 0.0), 3),
            "shared_hit": root.get("Shared Hit Blocks", 0),
            "shared_read": root.get("Shared Read Blocks", 0),
        }
    (out_dir / "measure.json").write_text(json.dumps(result, indent=1))
    return result


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return float("nan")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def summarize_times(times: list[float]) -> dict:
    return {
        "n": len(times),
        "min": round(min(times), 3),
        "median": round(median(times), 3),
        "max": round(max(times), 3),
    }
