"""独立监控连接：查询 pg_stat_activity 与类型名。

刻意不占用工作池连接，避免状态接口与业务请求互相阻塞。应用记录只是诊断，
这里读到的 pg_stat_activity 才是数据库真实状态。
"""
from __future__ import annotations

from psycopg import AsyncConnection

from .config import Config
from .events import EventLogger


class Monitor:
    def __init__(self, config: Config, logger: EventLogger) -> None:
        self._config = config
        self._logger = logger
        self._conn: AsyncConnection | None = None
        self._type_names: dict[int, str] = {}

    async def start(self) -> None:
        self._conn = await AsyncConnection.connect(
            self._config.dsn,
            autocommit=True,
            application_name=f"{self._config.app_name}-monitor",
            connect_timeout=5,
        )
        await self._load_type_names()
        self._logger.event("monitor_started", backend_pid=self._conn.info.backend_pid)

    async def _load_type_names(self) -> None:
        assert self._conn is not None
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)",
                (list(_KNOWN_OIDS),),
            )
            for oid, name in await cur.fetchall():
                self._type_names[oid] = name

    def type_name(self, oid: int) -> str:
        return self._type_names.get(oid, f"oid:{oid}")

    async def activities(self, backend_pids: list[int]) -> list[dict]:
        if self._conn is None or self._conn.closed:
            return []
        if not backend_pids:
            return []
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                SELECT pid, state, wait_event_type, wait_event,
                       xact_start, query_start, state_change,
                       left(query, 200) AS query_excerpt
                  FROM pg_stat_activity
                 WHERE pid = ANY(%s)
                 ORDER BY pid
                """,
                (backend_pids,),
            )
            cols = [d.name for d in cur.description]
            rows = await cur.fetchall()
        result = []
        for row in rows:
            item = {}
            for name, value in zip(cols, row):
                # datetime 等非 JSON 原生类型统一转 ISO 字符串，保证状态接口可序列化
                item[name] = value.isoformat() if hasattr(value, "isoformat") else value
            result.append(item)
        return result

    async def pids_still_active(self, backend_pids: list[int]) -> list[int]:
        """给定 backend 中仍存在（查询未消失）的 pid 列表。"""
        if not backend_pids or self._conn is None or self._conn.closed:
            return []
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT pid FROM pg_stat_activity WHERE pid = ANY(%s)",
                (backend_pids,),
            )
            return [row[0] for row in await cur.fetchall()]

    async def idle_in_transaction_pids(self) -> list[int]:
        if self._conn is None or self._conn.closed:
            return []
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                SELECT pid FROM pg_stat_activity
                 WHERE application_name LIKE %s AND state = 'idle in transaction'
                """,
                ("qgateway%",),
            )
            return [row[0] for row in await cur.fetchall()]

    async def close(self) -> None:
        if self._conn is not None:
            with __import__("contextlib").suppress(Exception):
                await self._conn.close()


_KNOWN_OIDS = {
    16: "bool",
    17: "bytea",
    18: "char",
    19: "name",
    20: "int8",
    21: "int2",
    23: "int4",
    25: "text",
    700: "float4",
    701: "float8",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    114: "json",
    3802: "jsonb",
    2950: "uuid",
}
