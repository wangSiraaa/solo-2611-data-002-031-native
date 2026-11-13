#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接器 D：混沌连接器（故障注入）。

通过环境变量 CHAOS_MODE 选择故障，供合成演示覆盖：
- reorder_batches   : 数据批故意乱序（宿主应发现 batch_no 不连续）
- unknown_id        : 发出宿主无法识别的 JSON-RPC 响应 id
- oversize          : 声明超大 Content-Length（超过宿主消息上限）
- half_exit         : 发出第一批后进程直接退出（stdout EOF）
- stderr_flood      : 查询期间向 stderr 持续洪水输出（宿主须持续排空不死锁）
- cancel_race       : 延迟到取消边界，制造 cancel 与最终响应同时到达
- complete_on_race  : 与 cancel_race 叠加，窄缝中看到 cancel 仍发成功终态，
                      确定性复现“completed (cancel request lost the race)”
- hang              : 收到 query 后永久挂起（验证超时终止策略）

FRAME_CHUNK_BYTES（公共运行时支持）把每条帧切成极小字节块写出，
可与上述任意模式叠加，验证宿主对半条头/半个中文字符的处理。
"""

from __future__ import annotations

import json
import os
import sys
import time

from common import ConnectorBase, ConnectorProtocolError, diag, load_jsonl


class ChaosConnector(ConnectorBase):
    name = "chaos"
    default_batch_size = 25

    def __init__(self) -> None:
        super().__init__()
        self.modes = {
            m.strip() for m in os.environ.get("CHAOS_MODE", "").split(",") if m.strip()
        }
        # 演示需要：显式关闭取消支持时，宿主只能走超时结束进程的路径。
        self.supports_cancel = os.environ.get("CHAOS_SUPPORTS_CANCEL", "1") != "0"
        self.capabilities_version = os.environ.get("CHAOS_CAP_VERSION", "4.0.0-chaos")
        diag(f"chaos modes={sorted(self.modes)} supports_cancel={self.supports_cancel}")

    def _catalog(self) -> dict:
        with open(os.path.join(self.data_dir, "catalog.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def sources(self) -> dict:
        spec = self._catalog()["chaos_lab"]
        return {
            "chaos_lab": {
                "fields": spec["fields"],
                "filterable": spec["filterable"],
                "queryParams": ["fields", "filters", "batchSize"],
                "description": spec["description"] + f" [modes={sorted(self.modes)}]",
            }
        }

    def load_source(self, source: str) -> list[dict]:
        return load_jsonl(os.path.join(self.data_dir, f"{source}.jsonl"))

    def per_batch_delay(self) -> float:
        return float(os.environ.get("CHAOS_BATCH_DELAY", "0.1"))

    # ------------------------------------------------------------ 故障注入
    def _run_query(self, rpc_id: int, params: dict) -> None:  # type: ignore[override]
        query_id = params["queryId"]
        if "unknown_id" in self.modes:
            diag(f"injecting unknown response id (real id={rpc_id})")
            self.send_result(rpc_id + 900000, {"bogus": True})
            return  # 真正的 rpc_id 永远不响应，宿主先因未知 id 终止本连接器

        if "oversize" in self.modes:
            diag("injecting oversize Content-Length frame")
            declared = int(os.environ.get("CHAOS_OVERSIZE_BYTES", str(20 * 1024 * 1024)))
            body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {}}).encode("utf-8")
            frame = b"Content-Length: %d\r\n\r\n%s" % (declared, body)
            with self._write_lock:
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()
            return

        if "hang" in self.modes:
            diag(f"injecting hang for rpc_id={rpc_id}")
            while True:
                time.sleep(3600)

        if "stderr_flood" in self.modes:
            self._stderr_flood()

        if "cancel_race" in self.modes:
            # 批间等待，直到“最后一个边界点”：取消若在此期间到达则取消生效，
            # 若在最后一批发出后到达则最终响应获胜 —— 竞争结果由宿主明确展示。
            return self._run_cancel_race(rpc_id, params)

        return self._run_batched(rpc_id, params, swap_batches="reorder_batches" in self.modes,
                                 half_exit="half_exit" in self.modes)

    def _stderr_flood(self) -> None:
        # 洪水在后台线程持续产生，查询本身继续，验证宿主不会被 stderr 拖死。
        import threading

        def flood() -> None:
            i = 0
            while True:
                print(f"[diag][chaos] flood line {i} " + "x" * 200, file=sys.stderr, flush=True)
                i += 1
                time.sleep(0.002)

        threading.Thread(target=flood, daemon=True).start()

    def _run_cancel_race(self, rpc_id: int, params: dict) -> None:
        query_id = params["queryId"]
        try:
            import threading

            event = threading.Event()
            with self._lock:
                self._cancel_events[query_id] = event

            source = params["source"]
            spec = self.sources()[source]
            records = self.load_source(source)
            from common import apply_filters, project

            records = project(apply_filters(records, params.get("filters") or {},
                                            spec["filterable"]), params.get("fields") or [])
            size = self.batch_size(params)
            batches = [records[i : i + size] for i in range(0, len(records), size)] or [[]]

            for idx, batch in enumerate(batches, start=1):
                # 每批前检查取消；最后一批后再给取消一个极短竞争窗口。
                if event.is_set():
                    self.send_cancelled(rpc_id, query_id)
                    return
                time.sleep(self.per_batch_delay())
                self.send_notification(
                    "dataBatch", {"queryId": query_id, "batchNo": idx, "records": batch}
                )

            time.sleep(float(os.environ.get("CHAOS_RACE_WINDOW_S", "0.8")))
            # complete_on_race：确定性复现“cancel 已登记、成功终态抢先”。
            # 连接器在窄缝中确认收到了 cancel 事件，但仍按竞赛规则发出成功终态，
            # 由宿主侧用 cancel_sent 标记 “completed (cancel request lost the race)”。
            if "complete_on_race" in self.modes:
                tight = float(os.environ.get("CHAOS_TIGHT_WINDOW_S", "1.0"))
                saw_cancel = event.wait(tight)
                diag(f"complete_on_race: cancel_seen_during_window={saw_cancel} "
                     f"-> emitting successful terminal anyway for {query_id}")
                self.send_result(
                    rpc_id,
                    {"queryId": query_id, "status": "completed",
                     "batchesTotal": len(batches), "recordsTotal": len(records)},
                )
                return
            if event.is_set():
                diag(f"cancel won the race for {query_id}")
                self.send_cancelled(rpc_id, query_id)
            else:
                diag(f"completion won the race for {query_id}")
                self.send_result(
                    rpc_id,
                    {"queryId": query_id, "status": "completed",
                     "batchesTotal": len(batches), "recordsTotal": len(records)},
                )
        except ConnectorProtocolError as exc:
            self.send_error(rpc_id, -32602, str(exc))
        finally:
            with self._lock:
                self._cancel_events.pop(query_id, None)

    def _run_batched(self, rpc_id: int, params: dict, swap_batches: bool, half_exit: bool) -> None:
        query_id = params["queryId"]
        try:
            import threading

            event = threading.Event()
            with self._lock:
                self._cancel_events[query_id] = event

            source = params["source"]
            spec = self.sources()[source]
            records = self.load_source(source)
            from common import apply_filters, project

            records = project(apply_filters(records, params.get("filters") or {},
                                            spec["filterable"]), params.get("fields") or [])
            size = self.batch_size(params)
            batches = [records[i : i + size] for i in range(0, len(records), size)] or [[]]

            order = list(range(1, len(batches) + 1))
            if swap_batches and len(order) >= 2:
                order[0], order[1] = order[1], order[0]  # 第 1、2 批对调 -> 宿主在批 1 即发现乱序
                diag(f"injecting reordered batches: {order}")

            for seq, batch_no in enumerate(order, start=1):
                if event.is_set():
                    self.send_cancelled(rpc_id, query_id)
                    return
                time.sleep(self.per_batch_delay())
                self.send_notification(
                    "dataBatch",
                    {"queryId": query_id, "batchNo": batch_no, "records": batches[batch_no - 1]},
                )
                if half_exit and seq == 1:
                    diag("injecting half_exit: os._exit after first batch")
                    sys.stderr.flush()
                    sys.stdout.flush()
                    os._exit(7)

            self.send_result(
                rpc_id,
                {"queryId": query_id, "status": "completed",
                 "batchesTotal": len(batches), "recordsTotal": len(records)},
            )
        except ConnectorProtocolError as exc:
            self.send_error(rpc_id, -32602, str(exc))
        finally:
            with self._lock:
                self._cancel_events.pop(query_id, None)


if __name__ == "__main__":
    ChaosConnector().run()
