# -*- coding: utf-8 -*-
"""单个连接器子进程的监管器。

职责：
1. 拉起子进程，stdin 写帧 / stdout 读帧 / stderr 持续排空，三条管道各自独立任务，
   任何一条都不会因为另一条阻塞而死锁（stderr 洪水、超大响应都不会卡住写入）；
2. 维护 JSON-RPC id -> 在途请求、query_id -> 查询作业两张表，
   响应严格按 id 关联，通知（无 id）绝不与查询结果配对；
3. 校验 dataBatch 的归属与递增 batch_no、终态响应的总批数/总记录数；
4. 每连接器独立的在途上限、消息大小、等待超时、待保存数据上限；
5. 协议错误携带连接器名与 stdout 字节偏移，按策略只终止本连接器，其他连接器不受影响；
6. 连接器异常退出时，令其全部未完成请求明确失败，已核对完成的结果保留；
7. 重启只建立新进程，绝不自动重发可能已部分返回的请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from .framing import FrameAssembler, ProtocolError, encode_frame
from .storage import Storage

_PAGE_SIZE = resource.getpagesize()


def _read_rss_bytes(pid: int) -> Optional[int]:
    """从 /proc/<pid>/statm 读当前 RSS（字节）。Linux 专用。"""
    try:
        with open(f"/proc/{pid}/statm", "r") as fh:
            fields = fh.read().split()
        return int(fields[1]) * _PAGE_SIZE
    except (OSError, IndexError, ValueError):
        return None


@dataclass
class RequestJob:
    rpc_id: int
    method: str
    query_id: Optional[str]
    future: asyncio.Future
    timer: Optional[asyncio.TimerHandle] = None
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class QueryJob:
    query_id: str
    source: str
    fields: List[str]
    filters: dict
    params: dict
    result_path: str
    result_fh: Any
    future: asyncio.Future
    next_batch: int = 1
    batches_seen: int = 0
    records_saved: int = 0
    saved_bytes: int = 0
    cancel_sent: bool = False
    terminal_state: str = "running"
    fail_reason: Optional[str] = None
    grace_timer: Optional[asyncio.TimerHandle] = None


class Metrics:
    """进程级指标：在途峰值与 RSS 峰值（线程安全，事件循环内使用）。"""

    def __init__(self) -> None:
        self._inflight: Dict[str, int] = {}
        self._peak_inflight: Dict[str, int] = {}
        self._peak_rss: Dict[str, int] = {}
        self.host_peak_rss = 0

    def inc_inflight(self, connector: str) -> None:
        n = self._inflight.get(connector, 0) + 1
        self._inflight[connector] = n
        self._peak_inflight[connector] = max(self._peak_inflight.get(connector, 0), n)

    def dec_inflight(self, connector: str) -> None:
        self._inflight[connector] = max(0, self._inflight.get(connector, 0) - 1)

    def sample_rss(self, connector: str, rss: Optional[int]) -> None:
        if rss is not None:
            self._peak_rss[connector] = max(self._peak_rss.get(connector, 0), rss)

    def sample_host(self) -> None:
        rss = _read_rss_bytes(os.getpid())
        if rss is not None:
            self.host_peak_rss = max(self.host_peak_rss, rss)

    def snapshot(self) -> dict:
        return {
            "peak_inflight_by_connector": dict(self._peak_inflight),
            "peak_rss_bytes_by_connector": dict(self._peak_rss),
            "host_peak_rss_bytes": self.host_peak_rss,
        }


class ConnectorSupervisor:
    STATES = ("starting", "initializing", "alive", "terminated")

    def __init__(
        self,
        name: str,
        config: dict,
        storage: Storage,
        metrics: Metrics,
        logs_dir: str,
        results_dir: str,
        emit: Callable[[str, str], Awaitable[None]],
    ):
        self.name = name
        self.cfg = config
        self.storage = storage
        self.metrics = metrics
        self.logs_dir = logs_dir
        self.results_dir = results_dir
        self._emit = emit  # async emit(level, message)，宿主把事件送到 REPL/报告

        self.state = "starting"
        self.capability: Optional[dict] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.exit_code: Optional[int] = None
        self.terminate_reason: Optional[str] = None

        self._asm: Optional[FrameAssembler] = None
        self._next_rpc_id = 1
        self._requests: Dict[int, RequestJob] = {}
        self._queries: Dict[str, QueryJob] = {}
        self._slots = asyncio.Semaphore(int(config["max_inflight"]))
        self._write_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._stderr_tail: Deque[str] = deque(maxlen=int(config.get("stderr_keep_lines", 200)))
        self._stderr_bytes = 0
        self._stderr_log_path = os.path.join(logs_dir, f"{name}.stderr.log")
        self._tasks: List[asyncio.Task] = []
        self._terminated = asyncio.Event()

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> dict:
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.cfg["command"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
        )
        self.state = "initializing"
        self._asm = FrameAssembler(int(self.cfg["max_message_bytes"]))
        self._tasks = [
            asyncio.create_task(self._stdout_loop(), name=f"{self.name}:stdout"),
            asyncio.create_task(self._writer_loop(), name=f"{self.name}:writer"),
            asyncio.create_task(self._stderr_loop(), name=f"{self.name}:stderr"),
            asyncio.create_task(self._rss_loop(), name=f"{self.name}:rss"),
        ]
        try:
            capability = await self._send_simple_request("initialize", {})
        except Exception as exc:
            await self.terminate(f"initialize failed: {exc}")
            raise
        self.capability = capability
        self.state = "alive"
        self.storage.upsert_connector(
            self.name, " ".join(self.cfg["command"]), self.cfg, capability
        )
        await self._emit("info", f"[{self.name}] initialized: {json.dumps(capability, ensure_ascii=False)}")
        return capability

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["CONNECTOR_NAME"] = self.name
        env["CONNECTOR_DATA_DIR"] = self.cfg.get("data_dir", "")
        # 注入混沌开关（仅 chaos 连接器使用），普通连接器忽略。
        for key, value in self.cfg.get("env", {}).items():
            env[key] = str(value)
        return env

    async def terminate(self, reason: str) -> None:
        if self._terminated.is_set():
            return
        self._terminated.set()
        self.terminate_reason = reason
        self.state = "terminated"
        await self._emit("warn", f"[{self.name}] terminating: {reason}")

        proc = self.proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        if proc is not None:
            self.exit_code = proc.returncode

        # 关闭写队列并清空，防止重启前有发送者永久阻塞；
        # 队列消费任务（writer_loop）随下面的任务取消一起结束。
        if proc is not None and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:
                pass
        while not self._write_q.empty():
            try:
                self._write_q.get_nowait()
            except asyncio.QueueEmpty:
                break

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # 全部在途请求明确失败；已完成的作业不在 self._queries 中，结果保留。
        pending = list(self._queries.values())
        for job in pending:
            self._fail_job(job, reason)
        for req in list(self._requests.values()):
            if req.timer:
                req.timer.cancel()
            if not req.future.done():
                req.future.set_exception(ConnectionError(f"connector terminated: {reason}"))
        self._requests.clear()
        self.metrics.sample_rss(self.name, _read_rss_bytes(proc.pid) if proc else None)

    async def restart(self) -> dict:
        """重启连接器。旧进程未完成请求已失败，这里不重发任何请求。"""
        if not self._terminated.is_set():
            await self.terminate("manual restart")
        # 重置全部运行态，保留 capability 之外的统计由 metrics 自身累计峰值。
        self.state = "starting"
        self.proc = None
        self.exit_code = None
        self.terminate_reason = None
        self._asm = None
        self._next_rpc_id = 1
        self._requests = {}
        self._queries = {}
        self._slots = asyncio.Semaphore(int(self.cfg["max_inflight"]))
        self._write_q = asyncio.Queue(maxsize=1024)
        self._stderr_tail.clear()
        self._stderr_bytes = 0
        self._tasks = []
        self._terminated = asyncio.Event()
        return await self.start()

    # ------------------------------------------------------------ 管道任务
    async def _stdout_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    if not self._terminated.is_set():
                        await self.terminate(
                            f"connector exited early (stdout EOF, exit={proc.returncode})"
                        )
                    return
                try:
                    frames = self._asm.feed(chunk)
                except ProtocolError as exc:
                    # feed 内部抛出的异常还没有连接器上下文，这里补上，
                    # 确保错误能定位到“连接器 + stdout 字节偏移”。
                    exc.connector = self.name
                    await self.terminate(exc.describe())
                    return
                for frame in frames:
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            await self.terminate(f"stdout reader error: {exc!r}")

    async def _writer_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdin is not None
        try:
            while True:
                data = await self._write_q.get()
                try:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
                except (ConnectionError, BrokenPipeError, RuntimeError) as exc:
                    if not self._terminated.is_set():
                        await self.terminate(f"stdin write failed: {exc!r}")
                    return
        except asyncio.CancelledError:
            raise

    async def _stderr_loop(self) -> None:
        """持续排空 stderr：内存只留有界尾部，全量写入有大小上限的日志文件。"""
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        max_bytes = int(self.cfg.get("stderr_log_max_bytes", 256 * 1024))
        with open(self._stderr_log_path, "w", encoding="utf-8") as log_fh:
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", errors="replace")
                    self._stderr_tail.append(text.rstrip("\n"))
                    if self._stderr_bytes < max_bytes:
                        self._stderr_bytes += len(line)
                        log_fh.write(text)
                        log_fh.flush()
            except asyncio.CancelledError:
                raise

    async def _rss_loop(self) -> None:
        try:
            while True:
                if self.proc is not None and self.proc.pid:
                    self.metrics.sample_rss(self.name, _read_rss_bytes(self.proc.pid))
                self.metrics.sample_host()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ 请求发送
    async def _register_request(
        self, method: str, params: dict, query_id: Optional[str], future: asyncio.Future
    ) -> int:
        """登记一个在途请求并把帧放入写队列，返回 JSON-RPC id。

        query 请求复用 :class:`QueryJob` 的同一个 future 作为唯一终态同步点，
        避免“请求 future 与查询 future 两个同步对象、终态只完成一个”的缺陷。
        """
        if self._terminated.is_set():
            raise ConnectionError(f"connector {self.name} terminated")
        rpc_id = self._next_rpc_id
        self._next_rpc_id += 1
        loop = asyncio.get_running_loop()
        req = RequestJob(rpc_id=rpc_id, method=method, query_id=query_id, future=future)
        req.timer = loop.call_later(
            float(self.cfg["wait_timeout_s"]), self._on_timeout, rpc_id
        )
        self._requests[rpc_id] = req
        frame = encode_frame(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        )
        try:
            self._write_q.put_nowait(frame)
        except asyncio.QueueFull:
            self._requests.pop(rpc_id, None)
            if req.timer is not None:
                req.timer.cancel()
            raise ConnectionError(f"connector {self.name} stdin queue full")
        return rpc_id

    async def _send_simple_request(self, method: str, params: dict):
        """initialize/status 等非查询请求：自建 future 并等待其完成。"""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        req_id = await self._register_request(method, params, query_id=None, future=future)
        try:
            return await future
        finally:
            req = self._requests.get(req_id)
            if req is not None and req.timer is not None:
                req.timer.cancel()

    async def _safe_enqueue(self, frame: bytes) -> None:
        """入队一帧；连接器已终止或队列满时直接放弃（终止流程会失败全部在途请求）。"""
        if self._terminated.is_set():
            return
        try:
            self._write_q.put_nowait(frame)
        except asyncio.QueueFull:
            await self.terminate("writer queue full: connector not draining stdin")

    def _on_timeout(self, rpc_id: int) -> None:
        req = self._requests.get(rpc_id)
        if req is None or req.future.done():
            return
        if req.method != "query" or not self.capability or not self.capability.get("supportsCancel"):
            # 不支持取消（或非查询请求）：超时直接结束进程，再报告受影响请求。
            asyncio.create_task(
                self.terminate(f"request id={rpc_id} method={req.method} timed out "
                               f"after {self.cfg['wait_timeout_s']}s (cancel unsupported)")
            )
            return
        # 支持取消：先发独立 cancel 通知，给宽限期，仍无响应再结束进程。
        job = self._queries.get(req.query_id) if req.query_id else None
        if job is not None and not job.cancel_sent:
            job.cancel_sent = True
            asyncio.create_task(self._emit("info", f"[{self.name}] timeout -> cancel {req.query_id}"))
            asyncio.create_task(
                self._safe_enqueue(
                    encode_frame(
                        {
                            "jsonrpc": "2.0",
                            "method": "cancel",
                            "params": {"queryId": req.query_id},
                        }
                    )
                )
            )
            grace = float(self.cfg.get("cancel_grace_s", self.cfg["wait_timeout_s"]))
            job.grace_timer = asyncio.get_running_loop().call_later(
                grace, self._on_cancel_grace, rpc_id
            )
        else:
            self._on_cancel_grace(rpc_id)

    def _on_cancel_grace(self, rpc_id: int) -> None:
        req = self._requests.get(rpc_id)
        if req is None or req.future.done():
            return
        asyncio.create_task(
            self.terminate(f"query {req.query_id} not cancelled within grace period; "
                           f"process killed, affected requests reported as failed")
        )

    # ------------------------------------------------------------ 帧分发
    def _dispatch(self, msg: dict) -> None:
        if msg.get("jsonrpc") != "2.0":
            self._protocol_fail("missing/invalid jsonrpc version field")
            return
        if "id" in msg:
            self._on_response(msg)
        else:
            self._on_notification(msg)

    def _protocol_fail(self, reason: str, offset: Optional[int] = None) -> None:
        off = offset if offset is not None else (self._asm.stream_offset if self._asm else -1)
        # 始终带上连接器名；FrameAssembler 抛出的 ProtocolError 在 _stdout_loop 里
        # 也会补 connector，但这里先兜底，避免错误信息出现占位符。
        exc = ProtocolError(reason, off, connector=self.name)
        asyncio.create_task(self.terminate(str(exc)))

    def _on_response(self, msg: dict) -> None:
        rpc_id = msg["id"]
        req = self._requests.pop(rpc_id, None)
        if req is None:
            # 未知响应 id：协议错误，定位连接器与偏移，终止该连接器。
            self._protocol_fail(f"response with unknown rpc id={rpc_id}")
            return
        if req.timer is not None:
            req.timer.cancel()

        if "error" in msg:
            err = msg["error"]
            code = err.get("code")
            try:
                code = int(code)
            except (TypeError, ValueError):
                pass
            if req.method == "query" and req.query_id:
                job = self._queries.get(req.query_id)
                if job:
                    self._fail_job(
                        job,
                        f"connector rpc error {code}: {err.get('message')}",
                        terminal="rpc-error",
                    )
            else:
                if not req.future.done():
                    req.future.set_exception(
                        RuntimeError(f"rpc error {code}: {err.get('message')}")
                    )
            return

        result = msg.get("result", {})
        if req.method in ("initialize", "status"):
            if not req.future.done():
                req.future.set_result(result)
            return

        # query 终态响应（req.future 与 job.future 是同一个对象）
        job = self._queries.get(req.query_id) if req.query_id else None
        if job is None:
            self._protocol_fail(f"terminal response for unknown query {req.query_id}")
            return
        if job.future.done():
            # 重复终态：同一查询出现第二个终态响应。
            self._protocol_fail(f"duplicate terminal response for query {job.query_id}")
            return
        if result.get("queryId") != job.query_id:
            self._protocol_fail(
                f"terminal response queryId={result.get('queryId')} != {job.query_id} (mis-attribution)"
            )
            return

        # 连接器确认取消（cancel 在与最终响应的竞争中获胜）。
        if result.get("status") == "cancelled":
            self._fail_job(
                job,
                "connector confirmed cancellation before terminal response",
                terminal="cancelled (cancel won the race)",
            )
            return

        batches_total = result.get("batchesTotal")
        records_total = result.get("recordsTotal")
        # 核对：批序必须 1..N 连续到齐，记录数必须与逐批累计一致，才能标记完整。
        if batches_total != job.batches_seen or job.next_batch != batches_total + 1:
            self._fail_job(
                job,
                f"batch accounting mismatch: declared total={batches_total}, "
                f"contiguous batches seen={job.batches_seen}",
                terminal="protocol-mismatch",
                kill=True,
            )
            return
        if records_total != job.records_saved:
            self._fail_job(
                job,
                f"record accounting mismatch: declared total={records_total}, "
                f"saved={job.records_saved}",
                terminal="protocol-mismatch",
                kill=True,
            )
            return

        # 取消竞争：cancel 通知已发出，但最终成功响应先到（或同时到）——响应获胜。
        if job.cancel_sent:
            job.terminal_state = "completed (cancel request lost the race)"
        else:
            job.terminal_state = "completed"
        job.result_fh.close()
        self.storage.finish_query(
            job.query_id,
            status="succeeded",
            batches_total=batches_total,
            records_total=records_total,
            result_path=job.result_path,
            fail_reason=None,
            terminal_state=job.terminal_state,
        )
        self._queries.pop(job.query_id, None)
        if not job.future.done():
            job.future.set_result(
                {
                    "query_id": job.query_id,
                    "status": "succeeded",
                    "batches": batches_total,
                    "records": records_total,
                    "terminal_state": job.terminal_state,
                    "result_path": job.result_path,
                }
            )

    def _on_notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "progress":
            job = self._queries.get(params.get("queryId"))
            if job is None:
                self._protocol_fail(f"progress notification for unknown query {params.get('queryId')}")
                return
            # 纯进度，不含记录，绝不作为查询结果。
            return
        if method == "dataBatch":
            self._on_data_batch(params)
            return
        self._protocol_fail(f"unknown notification method {method!r}")

    def _on_data_batch(self, params: dict) -> None:
        query_id = params.get("queryId")
        job = self._queries.get(query_id)
        if job is None:
            self._protocol_fail(f"dataBatch for unknown query {query_id}")
            return
        batch_no = params.get("batchNo")
        records = params.get("records")
        if not isinstance(batch_no, int) or batch_no < 1 or not isinstance(records, list):
            self._protocol_fail(f"malformed dataBatch for query {query_id}")
            return
        if batch_no != job.next_batch:
            # 批序错误：跳号、重复、乱序 —— 不同查询/批次的记录绝不允许串到一起。
            self._fail_job(
                job,
                f"batch sequence error: expected batchNo={job.next_batch}, got {batch_no}",
                terminal="protocol-mismatch",
                kill=True,
            )
            return

        payload = "".join(
            json.dumps(rec, ensure_ascii=False) + "\n" for rec in records
        ).encode("utf-8")
        if job.saved_bytes + len(payload) > int(self.cfg["data_cap_bytes"]):
            self._fail_job(
                job,
                f"data cap {self.cfg['data_cap_bytes']} bytes exceeded "
                f"(would have {job.saved_bytes + len(payload)} bytes)",
                terminal="data-cap-exceeded",
                kill=True,
            )
            return

        job.result_fh.write(payload.decode("utf-8"))
        job.result_fh.flush()
        job.saved_bytes += len(payload)
        job.next_batch += 1
        job.batches_seen += 1
        job.records_saved += len(records)
        self.storage.record_batch(job.query_id, batch_no, len(records), len(payload))

    def _fail_job(
        self,
        job: QueryJob,
        reason: str,
        terminal: str = "failed",
        kill: bool = False,
    ) -> None:
        if job.future.done():
            return
        job.fail_reason = reason
        # 取消竞争下连接器以错误结束：cancel 获胜。
        if job.cancel_sent and terminal == "failed":
            job.terminal_state = "cancelled (cancel won the race)"
        else:
            job.terminal_state = terminal
        try:
            job.result_fh.close()
        except Exception:
            pass
        self.storage.finish_query(
            job.query_id,
            status="cancelled" if job.terminal_state.startswith("cancelled") else "failed",
            batches_total=None,
            records_total=None,
            result_path=None,
            fail_reason=reason,
            terminal_state=job.terminal_state,
        )
        self._queries.pop(job.query_id, None)
        req = next((r for r in self._requests.values() if r.query_id == job.query_id), None)
        if req is not None:
            self._requests.pop(req.rpc_id, None)
            if req.timer:
                req.timer.cancel()
        job.future.set_exception(RuntimeError(reason))
        if kill:
            self._protocol_fail(reason)

    # ------------------------------------------------------------ 宿主入口
    async def submit_query(
        self, query_id: str, source: str, fields: List[str], filters: dict
    ) -> dict:
        # 在途上限：拿不到槽位就在这里排队（排队不计入在途峰值）。
        async with self._slots:
            if self._terminated.is_set():
                raise ConnectionError(f"connector {self.name} terminated: {self.terminate_reason}")
            result_path = os.path.join(self.results_dir, self.name, f"{query_id}.jsonl")
            os.makedirs(os.path.dirname(result_path), exist_ok=True)
            result_fh = open(result_path, "w", encoding="utf-8")
            params = {"queryId": query_id, "source": source, "fields": fields, "filters": filters}
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            job = QueryJob(
                query_id=query_id,
                source=source,
                fields=fields,
                filters=filters,
                params=params,
                result_path=result_path,
                result_fh=result_fh,
                future=future,
            )
            self._queries[query_id] = job
            try:
                self.storage.insert_query(
                    query_id,
                    self.name,
                    source,
                    fields,
                    filters,
                    params,
                    str(self.capability.get("capabilitiesVersion", "unknown"))
                    if self.capability
                    else "unknown",
                )
            except Exception:
                result_fh.close()
                self._queries.pop(query_id, None)
                raise
            self.metrics.inc_inflight(self.name)
            try:
                await self._register_request("query", params, query_id=query_id, future=future)
                return await future
            finally:
                self.metrics.dec_inflight(self.name)

    async def cancel_query(self, query_id: str) -> str:
        job = self._queries.get(query_id)
        if job is None:
            return f"no running query {query_id} on connector {self.name}"
        if not self.capability or not self.capability.get("supportsCancel"):
            return (f"connector {self.name} does not support cancel; "
                    f"timeout policy will terminate the process if the query hangs")
        if job.cancel_sent:
            return f"cancel for {query_id} already sent"
        job.cancel_sent = True
        await self._safe_enqueue(
            encode_frame(
                {"jsonrpc": "2.0", "method": "cancel", "params": {"queryId": query_id}}
            )
        )
        return f"cancel notification sent for {query_id}"

    # ------------------------------------------------------------ 状态视图
    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "pid": self.proc.pid if self.proc else None,
            "exit_code": self.exit_code,
            "terminate_reason": self.terminate_reason,
            "inflight": len(self._queries),
            "queued_or_running_ids": sorted(self._queries.keys()),
            "supports_cancel": bool(self.capability and self.capability.get("supportsCancel")),
            "capabilities_version": (self.capability or {}).get("capabilitiesVersion"),
            "stderr_tail": list(self._stderr_tail)[-20:],
            "stderr_log": self._stderr_log_path,
        }
