#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接器 B：乱序连接器。

行为：同一进程同时服务多个 query，后到的请求故意先完成（按到达顺序反转延迟），
用于证明宿主按 JSON-RPC id 关联响应、按 query_id 核对批次，而不是按发送顺序配对。
不支持 cancel（宿主对它只能走超时终止策略）。
来源：reorder_shipments。
"""

from __future__ import annotations

import json
import os
import threading

from common import ConnectorBase, diag, load_jsonl


class ReorderConnector(ConnectorBase):
    name = "reorder"
    capabilities_version = "2.3.1"
    supports_cancel = False
    default_batch_size = 30

    def __init__(self) -> None:
        super().__init__()
        self._arrival = 0
        self._arrival_lock = threading.Lock()
        self._delays: dict[int, float] = {}

    def _catalog(self) -> dict:
        with open(os.path.join(self.data_dir, "catalog.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def sources(self) -> dict:
        spec = self._catalog()["reorder_shipments"]
        return {
            "reorder_shipments": {
                "fields": spec["fields"],
                "filterable": spec["filterable"],
                "queryParams": ["fields", "filters", "batchSize"],
                "description": spec["description"],
            }
        }

    def load_source(self, source: str) -> list[dict]:
        return load_jsonl(os.path.join(self.data_dir, f"{source}.jsonl"))

    def per_request_delay(self) -> float:
        # 由 _run_query 通过 rpc_id 预设；基类无参钩子，这里用线程本地不可行，
        # 因此重写 _run_query 的入口延迟分配。
        return 0.0

    def _run_query(self, rpc_id: int, params: dict) -> None:  # type: ignore[override]
        with self._arrival_lock:
            self._arrival += 1
            seq = self._arrival
        # 第 1 个请求等最久，序号越大等得越短 -> 完成顺序与发送顺序相反。
        delay = max(0.05, 1.2 - seq * 0.2)
        self._delays[rpc_id] = delay
        diag(f"query rpc_id={rpc_id} arrival={seq} assigned delay={delay:.2f}s")
        import time

        time.sleep(delay)
        super()._run_query(rpc_id, params)


if __name__ == "__main__":
    ReorderConnector().run()
