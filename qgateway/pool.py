"""有界连接池：固定大小 + 有上限等待队列 + 严格归还校验。

关键规则：
- 池忙且等待队列已满时，acquire 抛 PoolQueueFull，HTTP 层快速 503；
- 归还时检查事务状态，失败事务/活动事务对应的连接绝不回池，直接关闭并补位，
  保证新请求拿到的连接一定是干净的（无残余结果、无失败事务）；
- 关闭中的池拒绝新请求，排队者收到 PoolClosed。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus

from .config import Config
from .events import EventLogger


class PoolQueueFull(Exception):
    """等待队列已满，快速拒绝。"""


class PoolClosed(Exception):
    """池正在关闭或已关闭。"""


@dataclass
class PoolStats:
    size: int
    idle: int
    busy: int
    waiters: int
    total_created: int
    total_closed: int
    total_rejected: int
    closing: bool


class BoundedPool:
    def __init__(self, config: Config, logger: EventLogger) -> None:
        self._config = config
        self._logger = logger
        # busy 集合中的 None 表示“槽位已占位、连接正在建立”
        self._idle: list[AsyncConnection] = []
        self._busy: set[AsyncConnection | None] = set()
        self._waiters: deque[object] = deque()
        self._cond = asyncio.Condition()
        self._total_created = 0
        self._total_closed = 0
        self._total_rejected = 0
        self._closing = False

    async def start(self) -> None:
        # 预热一条连接，尽早暴露数据库不可用问题
        conn = await self._open_connection()
        async with self._cond:
            self._idle.append(conn)
        self._logger.event("pool_started", size=self._config.pool_size, queue=self._config.wait_queue_limit)

    async def _open_connection(self) -> AsyncConnection:
        conn = await AsyncConnection.connect(
            self._config.dsn,
            autocommit=True,  # 事务由引擎显式 BEGIN/ROLLBACK 控制
            application_name=self._config.app_name,
            connect_timeout=5,
        )
        self._total_created += 1
        self._logger.event(
            "connection_opened",
            backend_pid=conn.info.backend_pid,
            total_created=self._total_created,
        )
        return conn

    def preflight_acquire(self) -> None:
        """同步容量预检（不占位、不入队）：满则立即抛 PoolQueueFull。

        仅用于在创建后台生产者之前快速拒绝；真正的 acquire 仍在锁内再次判定，
        因此预检通过与实际入队之间的竞态由 acquire 兜底。
        """
        if self._closing:
            raise PoolClosed("连接池正在关闭")
        if (
            not self._idle
            and len(self._busy) >= self._config.pool_size
            and len(self._waiters) >= self._config.wait_queue_limit
        ):
            self._total_rejected += 1
            self._logger.event(
                "pool_reject_full",
                waiters=len(self._waiters),
                busy=len(self._busy),
            )
            raise PoolQueueFull("连接池与等待队列均已满")

    async def acquire(self) -> AsyncConnection:
        # 先在锁内做一次性容量判定：池与队列都满时立即拒绝，
        # 调用方据此在创建任何后台工作协程之前返回 503，杜绝“孤儿生产者”。
        async with self._cond:
            if self._closing:
                raise PoolClosed("连接池正在关闭")
            if (
                not self._idle
                and len(self._busy) >= self._config.pool_size
                and len(self._waiters) >= self._config.wait_queue_limit
            ):
                self._total_rejected += 1
                self._logger.event(
                    "pool_reject_full",
                    waiters=len(self._waiters),
                    busy=len(self._busy),
                )
                raise PoolQueueFull("连接池与等待队列均已满")

        marker: object | None = None
        async with self._cond:
            if self._closing:
                raise PoolClosed("连接池正在关闭")
            try:
                while True:
                    if self._idle:
                        conn = self._idle.pop()
                        self._busy.add(conn)
                        self._logger.event(
                            "pool_acquire_idle",
                            backend_pid=conn.info.backend_pid,
                            waiters=len(self._waiters),
                        )
                        return conn
                    if len(self._busy) < self._config.pool_size:
                        # 槽位先占位，建连期间不允许超额
                        self._busy.add(None)
                        break
                    # 池已满：进入有界等待队列（兜底再判一次，防竞态）
                    if marker is None:
                        if len(self._waiters) >= self._config.wait_queue_limit:
                            self._total_rejected += 1
                            self._logger.event(
                                "pool_reject_full",
                                waiters=len(self._waiters),
                                busy=len(self._busy),
                            )
                            raise PoolQueueFull("连接池与等待队列均已满")
                        marker = object()
                        self._waiters.append(marker)
                    try:
                        await self._cond.wait()
                    except asyncio.CancelledError:
                        # 排队请求被取消（含服务停止）：移除自己的排队标记
                        with contextlib.suppress(ValueError):
                            self._waiters.remove(marker)
                        raise
                    if self._closing:
                        with contextlib.suppress(ValueError):
                            self._waiters.remove(marker)
                        raise PoolClosed("连接池正在关闭")
            finally:
                if marker is not None:
                    with contextlib.suppress(ValueError):
                        self._waiters.remove(marker)
        # 已占槽位，锁外建立连接（建连是网络 IO）
        try:
            conn = await self._open_connection()
        except BaseException:
            async with self._cond:
                self._busy.discard(None)
                self._cond.notify_all()
            raise
        async with self._cond:
            self._busy.discard(None)
            self._busy.add(conn)
        return conn

    async def release(self, conn: AsyncConnection, *, reusable: bool, reason: str) -> None:
        """归还连接。不可复用则关闭；唤醒排队者自行补建。"""
        async with self._cond:
            self._busy.discard(conn)
            healthy = reusable and self._is_clean(conn)
            if healthy and not self._closing:
                self._idle.append(conn)
                self._logger.event(
                    "pool_release_idle",
                    backend_pid=conn.info.backend_pid,
                    reason=reason,
                    idle=len(self._idle),
                )
            else:
                await self._close_connection(conn, reason=reason, clean_check=healthy)
            # 唤醒排队者：要么拿走 idle 连接，要么看到空槽自行建连
            self._cond.notify_all()

    def _is_clean(self, conn: AsyncConnection) -> bool:
        """连接能否安全交给新请求：未关闭且事务状态为 IDLE。"""
        if conn.closed:
            return False
        status = conn.info.transaction_status
        if status != TransactionStatus.IDLE:
            self._logger.event(
                "pool_dirty_transaction",
                backend_pid=conn.info.backend_pid,
                transaction_status=status.name,
            )
            return False
        return True

    async def _close_connection(self, conn: AsyncConnection, *, reason: str, clean_check: bool) -> None:
        pid = conn.info.backend_pid if not conn.closed else None
        try:
            # 失败事务或残余结果存在时，先 ROLLBACK 终结事务再关连接
            with contextlib.suppress(Exception):
                if not conn.closed and conn.info.transaction_status != TransactionStatus.IDLE:
                    await conn.execute("ROLLBACK")
            with contextlib.suppress(Exception):
                await conn.close()
        finally:
            self._total_closed += 1
            self._logger.event(
                "connection_closed",
                backend_pid=pid,
                reason=reason,
                clean_check=clean_check,
                total_closed=self._total_closed,
            )

    async def shutdown(self) -> None:
        """停止接新请求，关闭全部空闲连接；在途连接由引擎取消后经 release 清理。"""
        async with self._cond:
            self._closing = True
            while self._idle:
                conn = self._idle.pop()
                await self._close_connection(conn, reason="shutdown_idle", clean_check=True)
            self._cond.notify_all()
        self._logger.event("pool_shutdown_initiated", busy=len(self._busy))

    async def force_close_busy(self) -> None:
        """宽限期后仍未结束的数据库操作：强制关闭连接，后端随之终止，不留闲置事务。"""
        async with self._cond:
            real = [c for c in list(self._busy) if c is not None]
            for conn in real:
                self._busy.discard(conn)
        for conn in real:
            await self._close_connection(conn, reason="shutdown_force", clean_check=False)
        self._logger.event("pool_force_closed", count=len(real))

    def stats(self) -> PoolStats:
        return PoolStats(
            size=self._config.pool_size,
            idle=len(self._idle),
            busy=len(self._busy),
            waiters=len(self._waiters),
            total_created=self._total_created,
            total_closed=self._total_closed,
            total_rejected=self._total_rejected,
            closing=self._closing,
        )

    @property
    def busy_count(self) -> int:
        return len(self._busy)
