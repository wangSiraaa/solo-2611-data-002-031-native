"""查询执行引擎：有界背压流水线 + 取消/断连清理 + 终态管理。

每个 HTTP 请求一条独立流水线：

    数据库命名游标 --fetchmany(有界块)--> 有界帧队列 --drain 背压--> HTTP 写出
          生产者协程                         asyncio.Queue              消费者(请求生成器)

队列满时生产者在 ``queue.put`` 处挂起（状态 WAITING_FOR_CLIENT），不会继续拉取
游标，因此慢客户端不会导致完整结果物化进 Python 列表。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

from . import frames
from .catalog import QueryEntry, bind_params
from .config import Config
from .events import EventLogger
from .frames import (
    T_BYTE_BUDGET,
    T_CANCELLED,
    T_CLIENT_DISCONNECTED,
    T_QUERY_ERROR,
    T_ROW_BUDGET,
    T_SEND_FAILED,
    T_SUCCESS,
    T_TIME_LIMIT,
)
from .monitor import Monitor
from .pool import BoundedPool, PoolClosed, PoolQueueFull
from .registry import (
    EXECUTING,
    QUEUED,
    WAITING_FOR_CLIENT,
    Registry,
    RequestRecord,
)

# 仅出现在服务状态、不作为帧终态的拒绝原因
REJECTED_QUEUE_FULL = "rejected_queue_full"
REJECTED_SHUTDOWN = "rejected_shutdown"
REJECTED_BAD_PARAMS = "rejected_bad_params"


class _RowBudget(Exception):
    pass


class _ByteBudget(Exception):
    pass


class _TimeBudget(Exception):
    pass


@dataclass
class Context:
    request_id: str
    record: RequestRecord
    queue: asyncio.Queue
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    conn: Any = None
    producer: asyncio.Task | None = None
    # 传输层要求的终态原因（断连/发送失败）；显式取消时保持 None
    transport_reason: str | None = None
    shutdown_requested: bool = False


class Engine:
    def __init__(
        self,
        config: Config,
        pool: BoundedPool,
        monitor: Monitor,
        registry: Registry,
        logger: EventLogger,
    ) -> None:
        self._config = config
        self._pool = pool
        self._monitor = monitor
        self._registry = registry
        self._logger = logger
        self._contexts: dict[str, Context] = {}
        self._closing = False

    # ---------------------------------------------------------------- 提交与流

    def build_context(self, record: RequestRecord, entry: QueryEntry, raw_params: Any) -> Context:
        bound = bind_params(entry, raw_params)
        record.params = {p.name: _safe_param(v) for p, v in zip(entry.params, bound)}
        ctx = Context(
            request_id=record.request_id,
            record=record,
            queue=asyncio.Queue(maxsize=self._config.frame_queue_limit),
        )
        # 同步预检：池与队列已满时在此抛出，HTTP 层直接 503，不创建任何后台协程
        self._pool.preflight_acquire()
        ctx.producer = asyncio.create_task(
            self._produce(ctx, entry, bound), name=f"producer-{record.request_id}"
        )
        self._contexts[record.request_id] = ctx
        return ctx

    async def stream(self, ctx: Context):
        """请求生成器：从有界队列取帧并产出线上字节。首帧前抛出的异常由路由转成 JSON 错误。"""
        record = ctx.record
        producer = ctx.producer
        assert producer is not None
        try:
            while True:
                item = await ctx.queue.get()
                kind = item[0]
                if kind == "fatal":
                    raise item[1]
                payload: bytes = item[1]
                ctx.queue.task_done()
                record.inflight_frames = ctx.queue.qsize()
                record.inflight_bytes = _queue_bytes(ctx.queue)
                if kind == "terminal":
                    record.terminal_delivered = True
                    yield payload
                    return
                yield payload
        except GeneratorExit:
            # 消费者被关闭：客户端断连或服务停止，通知生产者终止数据库操作
            if not producer.done():
                await self._request_stop(ctx, ctx.transport_reason or T_CLIENT_DISCONNECTED)
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(producer), timeout=3.0)
            raise
        finally:
            self._contexts.pop(ctx.request_id, None)

    # ---------------------------------------------------------------- 取消接口

    async def cancel_request(self, request_id: str) -> dict[str, Any]:
        """显式取消指定请求。只作用于该 request_id，复用同一 TCP 连接的其它请求不受影响。"""
        record = self._registry.get(request_id)
        ctx = self._contexts.get(request_id)
        if record is None:
            return {"found": False}
        if record.terminal_reason is not None:
            return {"found": True, "already_terminal": True, "state": record.state}
        await self._request_stop(ctx, T_CANCELLED)
        self._logger.event("request_cancel_api", request_id=request_id, backend_pid=record.backend_pid)
        return {"found": True, "already_terminal": False, "state": record.state}

    def notify_transport_failure(self, request_id: str, reason: str) -> None:
        ctx = self._contexts.get(request_id)
        if ctx is None:
            return
        ctx.transport_reason = reason
        self._logger.event("transport_failure", request_id=request_id, reason=reason)

    async def _request_stop(self, ctx: Context | None, reason: str) -> None:
        if ctx is None:
            return
        if reason in (T_CLIENT_DISCONNECTED, T_SEND_FAILED):
            ctx.transport_reason = reason
        ctx.cancel_event.set()
        conn = ctx.conn
        if conn is not None and not conn.closed:
            # 从另一条连接发 CancelRequest，真正终止服务端查询
            with contextlib.suppress(Exception):
                await conn.cancel()

    # ---------------------------------------------------------------- 生产者

    async def _produce(self, ctx: Context, entry: QueryEntry, bound: list[Any]) -> None:
        record = ctx.record
        queue = ctx.queue
        # 执行预算从“拿到连接、真正开始执行”起算；排队等待连接的时间不计入，
        # 否则一个正常排队的点查会因队列等待而被误判为执行超时。
        deadline: float | None = None
        conn = None
        cursor = None
        reason = T_QUERY_ERROR
        detail: str | None = None
        sqlstate: str | None = None
        reusable = False

        try:
            try:
                conn = await self._pool.acquire()
            except PoolQueueFull as exc:
                record.finish(REJECTED_QUEUE_FULL, detail=str(exc))
                await self._enqueue_fatal(queue, exc)
                self._logger.event("request_rejected", request_id=ctx.request_id, reason="queue_full")
                return
            except PoolClosed as exc:
                record.finish(REJECTED_SHUTDOWN, detail=str(exc))
                await self._enqueue_fatal(queue, exc)
                return

            ctx.conn = conn
            record.backend_pid = conn.info.backend_pid
            record.mark(EXECUTING)
            # 此刻才开始计算执行时间预算
            deadline = time.monotonic() + entry.time_limit_seconds
            self._logger.event(
                "request_executing",
                request_id=ctx.request_id,
                query_id=entry.query_id,
                backend_pid=record.backend_pid,
            )

            # 每个 HTTP 请求自己的只读事务、自己的固定快照（REPEATABLE READ）
            await conn.execute("BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ")
            # SET 不接受绑定参数；该值由目录预算换算为整数毫秒，不包含用户输入
            timeout_ms = max(1, int(entry.time_limit_seconds * 1000))
            await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            await conn.execute("SET LOCAL lock_timeout = 2000")

            cursor = conn.cursor(name=f"srvsql_{ctx.request_id.replace('-', '')[:16]}")
            await cursor.execute(entry.sql, bound)
            columns = self._columns(cursor)
            column_oids = [c["type_oid"] for c in columns]
            await self._enqueue_frame(
                ctx,
                frames.meta_frame(
                    ctx.request_id, entry.query_id, columns, record.params
                ),
                count_bytes=False,
            )

            seq = 0
            while True:
                if ctx.cancel_event.is_set():
                    raise asyncio.CancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _TimeBudget()
                # 等待上限只取“剩余预算”：首次 fetch 可能需要等待整条查询完成，
                # 不能用固定的 fetch_timeout 误杀正常长查询；数据库侧 statement_timeout
                # 与这里的剩余预算共同保证超时终态。
                try:
                    batch = await asyncio.wait_for(
                        cursor.fetchmany(self._config.fetch_block_rows), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    # Python 侧时间预算到期：必须主动终止仍在运行的数据库查询
                    with contextlib.suppress(Exception):
                        await conn.cancel()
                    raise _TimeBudget()
                record.fetch_calls += 1
                if not batch:
                    break
                record.rows_fetched += len(batch)

                frame_obj, frame_bytes = frames.rows_frame(seq, batch, column_oids)
                seq += 1
                if record.rows_sent + len(batch) > entry.row_limit:
                    raise _RowBudget()
                if record.bytes_sent + frame_bytes > entry.byte_budget:
                    raise _ByteBudget()
                await self._enqueue_frame(ctx, frame_obj, wire_bytes=frame_bytes, row_count=len(batch))

            reason = T_SUCCESS

        except _RowBudget:
            reason = T_ROW_BUDGET
            detail = f"行数预算 {entry.row_limit} 已超出"
        except _ByteBudget:
            reason = T_BYTE_BUDGET
            detail = f"字节预算 {entry.byte_budget} 已超出"
        except _TimeBudget:
            reason = T_TIME_LIMIT
            detail = f"执行时间预算 {entry.time_limit_seconds}s 已超出"
        except asyncio.CancelledError:
            reason = ctx.transport_reason or T_CANCELLED
            detail = "服务停止" if ctx.shutdown_requested else "请求被取消"
        except psycopg.errors.QueryCanceled as exc:
            reason, detail, sqlstate = self._classify_cancel(ctx, exc)
        except psycopg.Error as exc:
            reason = T_QUERY_ERROR
            detail = str(exc).strip()[:300]
            sqlstate = exc.sqlstate
            record.last_error = detail
            self._logger.event(
                "query_error",
                request_id=ctx.request_id,
                sqlstate=sqlstate,
                backend_pid=record.backend_pid,
            )
        except BaseException as exc:  # 兜底：任何意外都必须走清理流程
            reason = T_QUERY_ERROR
            detail = repr(exc)[:300]
            record.last_error = detail
        finally:
            await self._cleanup_cursor(cursor)
            if conn is not None:
                reusable = await self._finish_transaction(conn, reason)
                await self._pool.release(conn, reusable=reusable, reason=reason)
            term = frames.terminal_frame(
                ctx.request_id,
                reason,
                rows_sent=record.rows_sent,
                bytes_sent=record.bytes_sent,
                detail=detail,
                sqlstate=sqlstate,
            )
            record.finish(reason, detail=detail, sqlstate=sqlstate)
            self._logger.event(
                "request_terminal",
                request_id=ctx.request_id,
                reason=reason,
                rows_sent=record.rows_sent,
                bytes_sent=record.bytes_sent,
                fetch_calls=record.fetch_calls,
                reusable=reusable,
                backend_pid=record.backend_pid,
            )
            # 终态帧尽力发送；消费者若已消失，5s 入队失败即只保留在服务状态
            try:
                await asyncio.wait_for(
                    queue.put(("terminal", frames.serialize(term), term)), timeout=5.0
                )
            except asyncio.TimeoutError:
                self._logger.event(
                    "terminal_undeliverable", request_id=ctx.request_id, reason=reason
                )

    # ---------------------------------------------------------------- 清理工具

    async def _cleanup_cursor(self, cursor: Any) -> None:
        if cursor is None:
            return
        with contextlib.suppress(Exception):
            await cursor.close()

    async def _finish_transaction(self, conn: Any, reason: str) -> bool:
        """回滚事务并判断连接能否回池。"""
        if conn.closed:
            return False
        from psycopg.pq import TransactionStatus

        try:
            if conn.info.transaction_status != TransactionStatus.IDLE:
                await conn.execute("ROLLBACK")
        except Exception as exc:
            self._logger.event(
                "rollback_failed", backend_pid=conn.info.backend_pid, error=repr(exc)
            )
            return False
        return conn.info.transaction_status == TransactionStatus.IDLE

    def _classify_cancel(self, ctx: Context, exc: psycopg.Error) -> tuple[str, str, str | None]:
        message = str(exc)
        if ctx.transport_reason is not None:
            return ctx.transport_reason, "客户端连接中断，查询已终止", exc.sqlstate
        if "statement timeout" in message or "statement_timeout" in message:
            return T_TIME_LIMIT, "数据库 statement_timeout 触发", exc.sqlstate
        if ctx.cancel_event.is_set():
            return T_CANCELLED, "取消请求已送达数据库", exc.sqlstate
        return T_QUERY_ERROR, message.strip()[:300], exc.sqlstate

    # ---------------------------------------------------------------- 队列与背压

    async def _enqueue_frame(
        self,
        ctx: Context,
        frame_obj: dict[str, Any],
        *,
        count_bytes: bool = True,
        wire_bytes: int | None = None,
        row_count: int = 0,
    ) -> None:
        record = ctx.record
        payload = frames.serialize(frame_obj)
        size = wire_bytes if wire_bytes is not None else len(payload)
        if ctx.queue.full():
            # 队列满：生产者在此挂起，停止继续 fetchmany，形成有界背压
            if record.state == EXECUTING:
                record.mark(WAITING_FOR_CLIENT)
        await ctx.queue.put(("frame", payload, frame_obj, size))
        record.inflight_frames = ctx.queue.qsize()
        record.inflight_bytes = _queue_bytes(ctx.queue)
        record.peak_inflight_frames = max(record.peak_inflight_frames, record.inflight_frames)
        record.peak_inflight_bytes = max(record.peak_inflight_bytes, record.inflight_bytes)
        # 一旦消费者取走过帧（队列不再满），生产者恢复拉取，状态切回执行中
        if record.state == WAITING_FOR_CLIENT and not ctx.queue.full():
            record.mark(EXECUTING)
        if count_bytes:
            record.rows_sent += row_count
            record.bytes_sent += size

    async def _enqueue_fatal(self, queue: asyncio.Queue, exc: Exception) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(queue.put(("fatal", exc)), timeout=2.0)

    def _columns(self, cursor: Any) -> list[dict[str, Any]]:
        result = []
        for d in cursor.description or []:
            result.append(
                {
                    "name": d.name,
                    "type_oid": d.type_code,
                    "type_name": self._monitor.type_name(d.type_code),
                    "display_size": d.display_size,
                }
            )
        return result

    # ---------------------------------------------------------------- 状态与停止

    async def status(self) -> dict[str, Any]:
        pids = [r.backend_pid for r in self._registry.active() if r.backend_pid]
        activities = await self._monitor.activities(pids)
        for act in activities:
            rec = next((r for r in self._registry.all() if r.backend_pid == act["pid"]), None)
            if rec is not None:
                rec.backend_state = act["state"]
                rec.backend_xact_start = act["xact_start"]
        snap = self._registry.snapshot()
        stats = self._pool.stats()
        return {
            "service": {
                "closing": self._closing,
                "pool": stats.__dict__,
                "rss_bytes": _rss_bytes(),
                "maxrss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "active_requests": len(self._registry.active()),
            },
            "pg_stat_activity": activities,
            **snap,
        }

    async def shutdown(self) -> None:
        """拒绝新请求 -> 通知在途请求取消 -> 有限宽限 -> 强制终止残余数据库操作。"""
        self._closing = True
        self._logger.event("shutdown_begin", active=len(self._contexts))
        await self._pool.shutdown()

        for ctx in self._contexts.values():
            ctx.shutdown_requested = True
            await self._request_stop(ctx, T_CANCELLED)

        if self._contexts:
            producers = [c.producer for c in self._contexts.values() if c.producer]
            done, pending = await asyncio.wait(
                {asyncio.shield(p) for p in producers if p},
                timeout=self._config.shutdown_grace_seconds,
            )
            for task in pending:
                task.cancel()
            for task in list(pending):
                with contextlib.suppress(Exception):
                    await task
        # 宽限期后仍占用的连接：强制关闭，PostgreSQL backend 随之消失
        await self._pool.force_close_busy()
        await self._monitor.close()
        self._logger.event("shutdown_complete")

    @property
    def closing(self) -> bool:
        return self._closing


def _queue_bytes(queue: asyncio.Queue) -> int:
    total = 0
    for item in queue._queue:  # type: ignore[attr-defined]
        if item[0] in ("frame", "terminal"):
            total += item[3] if len(item) > 3 else len(item[1])
    return total


def _rss_bytes() -> int:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        return 0


def _safe_param(value: Any) -> Any:
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
