#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接器 A：快速连接器。

行为：initialize 立即响应；query 以小批次迅速返回全部数据；支持 cancel。
来源：fast_orders（本地 JSONL，由 scripts/generate_data.py 生成）。
"""

from __future__ import annotations

import json
import os

from common import ConnectorBase, diag, load_jsonl


class FastConnector(ConnectorBase):
    name = "fast"
    capabilities_version = "1.0.0"
    supports_cancel = True
    default_batch_size = 40

    def _catalog(self) -> dict:
        path = os.path.join(self.data_dir, "catalog.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def sources(self) -> dict:
        catalog = self._catalog()
        spec = catalog["fast_orders"]
        return {
            "fast_orders": {
                "fields": spec["fields"],
                "filterable": spec["filterable"],
                "queryParams": ["fields", "filters", "batchSize"],
                "description": spec["description"],
            }
        }

    def load_source(self, source: str) -> list[dict]:
        diag(f"loading source {source}")
        return load_jsonl(os.path.join(self.data_dir, f"{source}.jsonl"))


if __name__ == "__main__":
    FastConnector().run()
