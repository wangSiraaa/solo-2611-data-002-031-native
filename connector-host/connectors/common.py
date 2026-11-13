# -*- coding: utf-8 -*-
"""连接器公共运行时。

四个合成连接器都基于本模块，各自只提供“数据 + 行为策略”：
- stdin 逐字节读取、按 Content-Length 拆帧（与宿主同一套规则的独立实现）；
- stdout 写响应/通知；stderr 只做诊断（写 ``[diag]`` 行）；
- 查询工作线程执行，主线程负责 IO，因此同一进程可同时存在多个在途请求，
  响应严格按各自的 JSON-RPC id 关联，不依赖发送顺序；
- 取消是独立的 ``cancel`` 通知，通过每查询的 Event 生效；通知没有响应 id。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# 与 host/framing.py 相同的线路规则，这里保持独立实现，
# 使 connectors/ 目录可以单独拷贝给其他团队而不依赖宿主代码。
MAX_HEADER_BYTES = 4096


class ConnectorProtocolError(Exception):
    pass


class FrameReader:
    """从 stdin 文件描述符增量读帧。

    注意不能用 ``sys.stdin.buffer.read(n)``：BufferedReader 在管道上会尽量
    填满 n 字节才返回，短帧会一直阻塞到 EOF。``os.read`` 在管道上是
    “有多少返回多少”，正确适配一次读拿到半帧/粘连多帧的场景。
    """

    def __init__(self, fd: int = 0) -> None:
        self._fd = fd
        self._buf = bytearray()
        self.offset = 0  # 已从 stdin 消费的字节偏移

    def _read_some(self, size: int) -> bytes:
        chunk = os.read(self._fd, size)
        if not chunk:
            raise EOFError
        return chunk

    def _read_until_header_end(self) -> int:
        while b"\r\n\r\n" not in self._buf and b"\n\n" not in self._buf:
            if len(self._buf) > MAX_HEADER_BYTES:
                raise ConnectorProtocolError("header too large")
            self._buf.extend(self._read_some(256))
        crlf = self._buf.find(b"\r\n\r\n")
        lf = self._buf.find(b"\n\n")
        if crlf != -1 and (lf == -1 or crlf < lf):
            return crlf + 4
        return lf + 2

    def read_message(self) -> Optional[dict]:
        head_end = self._read_until_header_end()
        header_block = bytes(self._buf[:head_end])
        del self._buf[:head_end]
        self.offset += head_end
        length: Optional[int] = None
        for line in header_block.replace(b"\r\n", b"\n").split(b"\n"):
            line = line.strip()
            if not line:
                continue
            if not line.lower().startswith(b"content-length:"):
                raise ConnectorProtocolError(f"bad header line: {line!r}")
            try:
                length = int(line[len(b"content-length:"):].strip())
            except ValueError:
                raise ConnectorProtocolError("bad Content-Length") from None
        if length is None:
            raise ConnectorProtocolError("missing Content-Length")

        while len(self._buf) < length:
            self._buf.extend(self._read_some(length - len(self._buf)))
        body = bytes(self._buf[:length])
        del self._buf[:length]
        self.offset += length
        return json.loads(body.decode("utf-8"))


def diag(message: str) -> None:
    """stderr 仅用于诊断，宿主会持续排空且只有界保留。"""
    print(f"[diag][{os.environ.get('CONNECTOR_NAME', 'connector')}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 过滤引擎
FILTER_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "contains")


def apply_filters(records: List[dict], filters: dict, allowed_fields: List[str]) -> List[dict]:
    """受控过滤：只允许声明过的字段与固定操作符，拒绝未知字段/操作符。"""
    if not isinstance(filters, dict):
        raise ConnectorProtocolError("filters must be an object")
    for field_name, cond in filters.items():
        if field_name not in allowed_fields:
            raise ConnectorProtocolError(f"field {field_name!r} is not filterable")
        if not isinstance(cond, dict) or len(cond) != 1:
            raise ConnectorProtocolError(f"filter on {field_name!r} must be a single op object")
        (op, value), = cond.items()
        if op not in FILTER_OPS:
            raise ConnectorProtocolError(f"unsupported filter op {op!r}")
        if op == "in" and not isinstance(value, list):
            raise ConnectorProtocolError("'in' filter requires a list")

    def keep(rec: dict) -> bool:
        for field_name, cond in filters.items():
            (op, value), = cond.items()
            actual = rec.get(field_name)
            if op == "eq" and not actual == value:
                return False
            if op == "ne" and not actual != value:
                return False
            if op == "gt" and not actual > value:
                return False
            if op == "ge" and not actual >= value:
                return False
            if op == "lt" and not actual < value:
                return False
            if op == "le" and not actual <= value:
                return False
            if op == "in" and actual not in value:
                return False
            if op == "contains" and not (isinstance(actual, str) and value in actual):
                return False
        return True

    return [rec for rec in records if keep(rec)]


def project(records: List[dict], fields: List[str]) -> List[dict]:
    if not fields:
        return [dict(rec) for rec in records]
    out = []
    for rec in records:
        missing = [f for f in fields if f not in rec]
        if missing:
            raise ConnectorProtocolError(f"unknown field(s) requested: {missing}")
        out.append({f: rec[f] for f in fields})
    return out


# ---------------------------------------------------------------- 连接器基类
class ConnectorBase:
    """具体连接器实现 :meth:`load_source` 与行为钩子即可。"""

    name = "base"
    capabilities_version = "1.0.0"
    supports_cancel = False
    default_batch_size = 50

    def __init__(self) -> None:
        self._cancel_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 写 stdout 加锁：多工作线程并发响应，帧绝不能互相穿插。
        self._write_lock = threading.Lock()
        self.data_dir = os.environ.get("CONNECTOR_DATA_DIR", "")

    # ---- 由子类实现 ----
    def sources(self) -> Dict[str, dict]:
        """返回 source 目录：{source: {fields, filterable, description}}。"""
        raise NotImplementedError

    def load_source(self, source: str) -> List[dict]:
        raise NotImplementedError

    # ---- 行为钩子（默认：快速、无延迟）----
    def per_request_delay(self) -> float:
        return 0.0

    def per_batch_delay(self) -> float:
        return 0.0

    def batch_size(self, params: dict) -> int:
        return int(params.get("batchSize") or self.default_batch_size)

    def chunk_bytes(self) -> int:
        """stdout 每次 write 的最大字节数（演示极小字节块/半个中文字符）。"""
        return int(os.environ.get("FRAME_CHUNK_BYTES", "0")) or 1 << 20

    # ---- 协议 ----
    def capability(self) -> dict:
        return {
            "connector": self.name,
            "capabilitiesVersion": self.capabilities_version,
            "supportsCancel": self.supports_cancel,
            "sources": self.sources(),
            "filterOps": list(FILTER_OPS),
        }

    def _write_frame(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        chunk = self.chunk_bytes()
        with self._write_lock:
            out = sys.stdout.buffer
            for i in range(0, len(frame), chunk):
                out.write(frame[i : i + chunk])
                out.flush()

    def send_result(self, rpc_id: int, result: dict) -> None:
        self._write_frame({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def send_error(self, rpc_id: int, code: int, message: str) -> None:
        self._write_frame(
            {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
        )

    def send_notification(self, method: str, params: dict) -> None:
        self._write_frame({"jsonrpc": "2.0", "method": method, "params": params})

    def send_cancelled(self, rpc_id: int, query_id: str) -> None:
        self._write_frame(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "queryId": query_id,
                    "status": "cancelled",
                    "batchesTotal": None,
                    "recordsTotal": None,
                },
            }
        )

    # ---- 查询执行（工作线程）----
    def _run_query(self, rpc_id: int, params: dict) -> None:
        query_id = params["queryId"]
        try:
            event = threading.Event()
            with self._lock:
                self._cancel_events[query_id] = event

            time.sleep(self.per_request_delay())
            source = params.get("source")
            catalog = self.sources()
            if source not in catalog:
                self.send_error(rpc_id, -32602, f"unknown source {source!r}")
                return
            spec = catalog[source]
            records = self.load_source(source)
            records = apply_filters(records, params.get("filters") or {}, spec["filterable"])
            records = project(records, params.get("fields") or [])

            size = self.batch_size(params)
            batches = [records[i : i + size] for i in range(0, len(records), size)]
            if not batches:
                batches = [[]]
            total = len(records)

            sent = 0
            for idx, batch in enumerate(batches, start=1):
                if event.is_set():
                    diag(f"query {query_id} cancelled before batch {idx}")
                    self.send_cancelled(rpc_id, query_id)
                    return
                if self.per_batch_delay():
                    time.sleep(self.per_batch_delay())
                self.send_notification(
                    "dataBatch",
                    {"queryId": query_id, "batchNo": idx, "records": batch},
                )
                sent += len(batch)
                self.on_progress(query_id, idx, len(batches), sent, total)

            if event.is_set():
                # 取消与最终响应同时到达的竞争点：数据已全部发出时取消才生效。
                diag(f"query {query_id} cancel arrived at terminal boundary")
                self.send_cancelled(rpc_id, query_id)
                return
            self.send_result(
                rpc_id,
                {
                    "queryId": query_id,
                    "status": "completed",
                    "batchesTotal": len(batches),
                    "recordsTotal": total,
                },
            )
        except ConnectorProtocolError as exc:
            self.send_error(rpc_id, -32602, str(exc))
        except Exception as exc:  # 连接器内部异常以 RPC 错误返回，不静默吞掉
            diag(f"query {query_id} failed: {exc!r}")
            self.send_error(rpc_id, -32603, f"internal error: {exc!r}")
        finally:
            with self._lock:
                self._cancel_events.pop(query_id, None)

    def on_progress(self, query_id: str, batch_no: int, batch_total: int, sent: int, total: int) -> None:
        """钩子：progress 连接器在此发送纯进度通知。"""

    def handle_cancel(self, params: dict) -> None:
        query_id = (params or {}).get("queryId")
        with self._lock:
            event = self._cancel_events.get(query_id)
        if event is not None:
            event.set()
            diag(f"cancel event set for {query_id}")
        else:
            diag(f"cancel for unknown/finished query {query_id} ignored")

    # ---- 主循环 ----
    def run(self) -> None:
        reader = FrameReader(fd=0)
        diag(f"starting pid={os.getpid()} capability={self.capabilities_version}")
        threads: List[threading.Thread] = []
        try:
            while True:
                try:
                    msg = reader.read_message()
                except EOFError:
                    diag("stdin closed, exiting")
                    break
                except ConnectorProtocolError as exc:
                    diag(f"fatal framing error at offset {reader.offset}: {exc}")
                    sys.exit(2)
                if msg is None:
                    break
                rpc_id = msg.get("id")
                method = msg.get("method")
                params = msg.get("params") or {}

                if "id" not in msg:
                    # 通知：宿主 -> 连接器，目前只有 cancel；通知没有响应。
                    if method == "cancel":
                        self.handle_cancel(params)
                    else:
                        diag(f"unknown notification {method!r} ignored")
                    continue

                if method == "initialize":
                    self.send_result(rpc_id, self.capability())
                elif method == "status":
                    self.send_result(
                        rpc_id,
                        {"connector": self.name, "activeQueries": len(self._cancel_events)},
                    )
                elif method == "query":
                    t = threading.Thread(
                        target=self._run_query,
                        args=(rpc_id, params),
                        name=f"query-{rpc_id}",
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)
                else:
                    self.send_error(rpc_id, -32601, f"method {method!r} not found")
        finally:
            for t in threads:
                t.join(timeout=5)


def load_jsonl(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def new_query_id() -> str:
    return "q-" + uuid.uuid4().hex[:12]
