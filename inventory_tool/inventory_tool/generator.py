"""合成输入生成: 全部数据由项目生成, 不依赖真实仓库或外部服务.

生成内容:
- stock.csv: sku, 非负整数库存
- requests.jsonl: request_id / order_id / action / items, 状态机上合法的请求序列
种子固定时输出可复现.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

# 操作后订单进入的状态
_NEXT_STATUS = {"reserve": "RESERVED", "ship": "SHIPPED", "cancel": "CANCELLED"}


def generate_dataset(out_dir: str | Path, *, seed: int = 20261113,
                     sku_count: int = 8, order_count: int = 12) -> dict[str, Path]:
    """生成一套自洽的合成库存与请求文件.

    生成器内部模拟库存预占, 保证成功请求在"无并发"假设下都能执行;
    失败请求 (库存不足 / 终态互转 / 重复预占) 也会明确包含, 供演示失败路径.
    """
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    skus = [f"SKU{idx:03d}" for idx in range(1, sku_count + 1)]
    on_hand = {sku: rng.randint(0, 40) for sku in skus}
    reserved = {sku: 0 for sku in skus}

    stock_path = out / "stock.csv"
    with stock_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sku", "on_hand"])
        for sku in skus:
            writer.writerow([sku, on_hand[sku]])

    requests: list[dict] = []
    order_status: dict[str, str] = {}
    order_items: dict[str, list[dict]] = {}
    seq = 0

    def add(order_id: str, action: str, items: list[dict]) -> None:
        nonlocal seq
        seq += 1
        requests.append({
            "request_id": f"REQ-{seq:05d}",
            "order_id": order_id,
            "action": action,
            "items": items,
        })

    for n in range(1, order_count + 1):
        order_id = f"ORD-{n:04d}"
        # 每个订单 1~3 个商品
        picks = rng.sample(skus, k=rng.randint(1, min(3, sku_count)))
        items = [{"sku": sku, "qty": rng.randint(1, 8)} for sku in picks]

        # 约 1/4 概率构造一个注定缺货的大数量订单 (失败但不部分预占)
        force_short = rng.random() < 0.25
        if force_short:
            big = [{"sku": picks[0],
                    "qty": on_hand[picks[0]] - reserved[picks[0]] + rng.randint(1, 5)}]
            add(order_id, "reserve", big)
            order_status[order_id] = "FAILED_RESERVE"
            order_items[order_id] = big
            continue

        add(order_id, "reserve", items)
        for it in items:
            reserved[it["sku"]] += it["qty"]
        order_status[order_id] = "RESERVED"
        order_items[order_id] = items

        # 对 RESERVED 订单随机 ship / cancel
        terminal = rng.choice(["ship", "cancel"])
        add(order_id, terminal, items)
        if terminal == "ship":
            for it in items:
                on_hand[it["sku"]] -= it["qty"]
                reserved[it["sku"]] -= it["qty"]
        else:
            for it in items:
                reserved[it["sku"]] -= it["qty"]
        order_status[order_id] = _NEXT_STATUS[terminal]

    # 追加若干明确的非法请求, 演示拒绝路径
    if requests:
        first = requests[0]
        seq += 1
        # 同编号换内容
        requests.append({
            "request_id": first["request_id"],
            "order_id": first["order_id"],
            "action": first["action"],
            "items": [{"sku": first["items"][0]["sku"],
                       "qty": first["items"][0]["qty"] + 1}],
        })
        # 对终态订单再做终态操作 (终态互转)
        shipped = next((r for r in requests
                        if order_status.get(r["order_id"]) == "SHIPPED"), None)
        if shipped:
            seq += 1
            requests.append({
                "request_id": f"REQ-{seq:05d}",
                "order_id": shipped["order_id"],
                "action": "cancel",
                "items": shipped["items"],
            })
        # 对已预占过的订单再次 reserve (拒绝叠加)
        reserved_order = next(
            (oid for oid, st in order_status.items() if st == "RESERVED"), None
        )
        if reserved_order:
            seq += 1
            requests.append({
                "request_id": f"REQ-{seq:05d}",
                "order_id": reserved_order,
                "action": "reserve",
                "items": order_items[reserved_order],
            })

    req_path = out / "requests.jsonl"
    with req_path.open("w", encoding="utf-8") as fh:
        for req in requests:
            fh.write(json.dumps(req, ensure_ascii=False) + "\n")

    return {"stock": stock_path, "requests": req_path}
