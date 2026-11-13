# -*- coding: utf-8 -*-
"""生成本地合成数据（确定性、可复现，作为逐查询真值比对的原始事实）。

每个来源一个 JSONL 文件，记录包含中文文本（UTF-8 下每字 3 字节），
用于证明分帧按字节而非字符工作。真值校验器直接读取这些原始文件，
不经过连接器代码。
"""

from __future__ import annotations

import json
import os
import random
import sys

REGIONS = ["华北", "华东", "华南", "西南", "西北", "东北", "华中"]
CATEGORIES = ["订单", "库存", "物流", "退款", "会员"]
NAMES = ["北京中心", "上海枢纽", "广州节点", "成都仓", "西安站", "沈阳点", "武汉库"]

# source -> (记录数, 随机种子)
SOURCES = {
    "fast_orders": (200, 1001),
    "reorder_shipments": (150, 2002),
    "progress_inventory": (120, 3003),
    "chaos_lab": (100, 4004),
}

CATALOG = {
    "fast_orders": {
        "fields": ["id", "name", "region", "category", "amount", "score", "ts"],
        "filterable": ["region", "category", "amount", "score"],
        "description": "快速来源：请求后立即分批返回",
    },
    "reorder_shipments": {
        "fields": ["id", "name", "region", "category", "amount", "score", "ts"],
        "filterable": ["region", "category", "amount", "score"],
        "description": "乱序来源：多个并发请求故意以相反顺序完成",
    },
    "progress_inventory": {
        "fields": ["id", "name", "region", "category", "amount", "score", "ts"],
        "filterable": ["region", "category", "amount", "score"],
        "description": "进度来源：批与批之间发送 progress 采集进度通知",
    },
    "chaos_lab": {
        "fields": ["id", "name", "region", "category", "amount", "score", "ts"],
        "filterable": ["region", "category", "amount", "score"],
        "description": "混沌来源：由环境变量注入各类协议/进程故障",
    },
}


def gen_records(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    records = []
    base_ts = 1_700_000_000
    for i in range(n):
        region = rng.choice(REGIONS)
        records.append(
            {
                "id": i + 1,
                # 中文名称 + 序号：制造多字节 UTF-8 边界
                "name": f"{rng.choice(NAMES)}-{region}-编号{i + 1:04d}",
                "region": region,
                "category": rng.choice(CATEGORIES),
                "amount": round(rng.uniform(10, 5000), 2),
                "score": rng.randint(0, 100),
                "ts": base_ts + i * 37,
            }
        )
    return records


def main(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    for source, (n, seed) in SOURCES.items():
        records = gen_records(n, seed)
        path = os.path.join(data_dir, f"{source}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"wrote {path}: {len(records)} records")
    with open(os.path.join(data_dir, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(CATALOG, fh, ensure_ascii=False, indent=2)
    print(f"wrote catalog with {len(CATALOG)} sources")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    main(out)
