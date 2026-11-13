# -*- coding: utf-8 -*-
"""独立真值校验器。

与连接器代码零共享：直接读取 scripts/generate_data.py 产出的原始 JSONL，
按协议文档中声明的过滤/投影语义重新计算每个查询的期望记录，
再与宿主 SQLite + 结果文件中导出的实际记录逐查询比对。
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

OPS = {"eq", "ne", "gt", "ge", "lt", "le", "in", "contains"}


def _match(actual, op: str, value) -> bool:
    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "gt":
        return actual > value
    if op == "ge":
        return actual >= value
    if op == "lt":
        return actual < value
    if op == "le":
        return actual <= value
    if op == "in":
        return actual in value
    if op == "contains":
        return isinstance(actual, str) and value in actual
    raise ValueError(f"unknown op {op}")


def expected_records(data_dir: str, source: str, fields: List[str], filters: dict) -> List[dict]:
    """真值：从原始数据文件独立计算。"""
    with open(os.path.join(data_dir, f"{source}.jsonl"), "r", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    for field_name, cond in filters.items():
        (op, value), = cond.items()
        if op not in OPS:
            raise ValueError(f"unknown op {op}")
        records = [r for r in records if _match(r.get(field_name), op, value)]

    if fields:
        records = [{f: r[f] for f in fields} for r in records]
    return records


def read_result_file(path: Optional[str]) -> List[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def verify_manifest(manifest_path: str, data_dir: str) -> Tuple[List[dict], List[dict]]:
    """返回 (成功项, 失败/异常项)，每项带 verdict 与细节。"""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    ok_items: List[dict] = []
    bad_items: List[dict] = []
    for q in manifest["queries"]:
        item = {"query_id": q["query_id"], "connector": q["connector"], "source": q["source"],
                "status": q["status"], "terminal_state": q["terminal_state"]}
        if q["status"] != "succeeded":
            item["verdict"] = "expected-failure" if q["fail_reason"] else "missing-failure-reason"
            item["fail_reason"] = q["fail_reason"]
            (ok_items if item["verdict"] == "expected-failure" else bad_items).append(item)
            continue

        expected = expected_records(
            data_dir, q["source"], q["fields"], q["filters"]
        )
        actual = q["records"] if q["records"] else read_result_file(q.get("result_path"))
        problems = []
        if len(actual) != len(expected):
            problems.append(f"record count: expected {len(expected)} got {len(actual)}")
        if q["records_total"] != len(expected):
            problems.append(f"declared recordsTotal {q['records_total']} != truth {len(expected)}")
        if q["batches_total"] != q["batches_seen"]:
            problems.append(
                f"batches total/seen mismatch {q['batches_total']}/{q['batches_seen']}"
            )
        if actual == expected:
            item["verdict"] = "match"
            item["records"] = len(actual)
            item["batches"] = q["batches_total"]
            ok_items.append(item)
        else:
            # 定位第一条不一致记录，方便排查串批/串查询。
            first_diff = next(
                (i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]),
                min(len(actual), len(expected)),
            )
            problems.append(f"content mismatch first at row {first_diff}")
            item["verdict"] = "mismatch"
            item["problems"] = problems
            bad_items.append(item)
    return ok_items, bad_items
