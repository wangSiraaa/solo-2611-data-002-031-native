#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接器 C：进度连接器。

行为：每批数据之间发送 ``progress`` 通知（只含进度百分比，不含记录），
数据仍走带 query_id / batch_no 的 ``dataBatch``；宿主必须区分通知与结果。
支持 cancel，批间延迟使取消与终态竞争可以被稳定演示。
来源：progress_inventory。
"""

from __future__ import annotations

import json
import os

from common import ConnectorBase, diag, load_jsonl


class ProgressConnector(ConnectorBase):
    name = "progress"
    capabilities_version = "3.1.0"
    supports_cancel = True
    default_batch_size = 20

    def _catalog(self) -> dict:
        with open(os.path.join(self.data_dir, "catalog.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def sources(self) -> dict:
        spec = self._catalog()["progress_inventory"]
        return {
            "progress_inventory": {
                "fields": spec["fields"],
                "filterable": spec["filterable"],
                "queryParams": ["fields", "filters", "batchSize"],
                "description": spec["description"],
            }
        }

    def load_source(self, source: str) -> list[dict]:
        return load_jsonl(os.path.join(self.data_dir, f"{source}.jsonl"))

    def per_batch_delay(self) -> float:
        # 让 progress 通知在时间上可见，也给取消留出窗口。
        return float(os.environ.get("PROGRESS_BATCH_DELAY", "0.25"))

    def on_progress(self, query_id: str, batch_no: int, batch_total: int, sent: int, total: int) -> None:
        pct = round(100.0 * sent / max(1, total), 1)
        diag(f"query {query_id} progress {pct}% (batch {batch_no}/{batch_total})")
        self.send_notification(
            "progress",
            {
                "queryId": query_id,
                "batchNo": batch_no,
                "batchesTotal": batch_total,
                "recordsSent": sent,
                "recordsTotal": total,
                "percent": pct,
            },
        )


if __name__ == "__main__":
    ProgressConnector().run()
