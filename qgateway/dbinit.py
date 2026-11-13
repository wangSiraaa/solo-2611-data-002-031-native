"""合成数据库初始化：schema 与可重复生成的演示数据（默认 12 万行）。

所有 SQL 与数据由项目生成；随机数使用固定种子，保证多次初始化内容一致。
"""
from __future__ import annotations

import argparse
import asyncio

from psycopg import AsyncConnection

from .config import Config

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS gateway;

DROP TABLE IF EXISTS gateway.events CASCADE;
DROP TABLE IF EXISTS gateway.event_kinds CASCADE;

CREATE TABLE gateway.event_kinds (
    kind_id   integer PRIMARY KEY,
    kind_name text NOT NULL
);

CREATE TABLE gateway.events (
    event_id    bigint PRIMARY KEY,
    tenant_id   integer NOT NULL,
    kind_id     integer NOT NULL REFERENCES gateway.event_kinds(kind_id),
    amount      numeric(14, 2) NOT NULL,
    event_time  timestamptz NOT NULL,
    payload     bytea,
    occurred_on date NOT NULL,
    notes       text
);

CREATE INDEX idx_events_kind_time ON gateway.events (kind_id, event_time);
CREATE INDEX idx_events_time ON gateway.events (event_time);
"""

KINDS_SQL = """
INSERT INTO gateway.event_kinds (kind_id, kind_name) VALUES
    (1, 'login'), (2, 'purchase'), (3, 'refund'), (4, 'click')
"""

# 固定种子的确定性数据：int8 主键、NUMERIC(14,2)、timestamptz、date、bytea 与可空 text
# 注意：该语句带 %s 参数，取模运算符必须写成 %% 以通过 psycopg 占位符解析
EVENTS_SQL = """
INSERT INTO gateway.events
    (event_id, tenant_id, kind_id, amount, event_time, payload, occurred_on, notes)
SELECT
    g.event_id,
    (g.event_id %% 7) + 1 AS tenant_id,
    (g.event_id %% 4) + 1 AS kind_id,
    round((random() * 9000 + 100)::numeric, 2) AS amount,
    timestamp with time zone '2026-01-01 00:00:00+00'
        + make_interval(secs => g.event_id * 37) AS event_time,
    decode(lpad(to_hex(g.event_id %% 65536), 8, '0'), 'hex') AS payload,
    date '2026-01-01' + (g.event_id / 1000) AS occurred_on,
    CASE WHEN g.event_id %% 10 = 0 THEN NULL ELSE 'note-' || (g.event_id %% 100) END AS notes
  FROM generate_series(1, %s) AS g(event_id)
"""

ANALYZE_SQL = "ANALYZE gateway.events; ANALYZE gateway.event_kinds;"


async def initialize(dsn: str, seed_rows: int) -> None:
    conn = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT setseed(0.4242)")
            await cur.execute(SCHEMA_SQL)
            await cur.execute(KINDS_SQL)
            await cur.execute(EVENTS_SQL, (seed_rows,))
            await cur.execute(ANALYZE_SQL)
            await cur.execute(
                "CREATE TABLE IF NOT EXISTS gateway.meta (key text PRIMARY KEY, value text NOT NULL)"
            )
            await cur.execute(
                "INSERT INTO gateway.meta (key, value) VALUES ('seed_rows', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(seed_rows),),
            )
            await cur.execute("SELECT count(*) FROM gateway.events")
            count = (await cur.fetchone())[0]
        print(f"初始化完成：gateway.events {count} 行，gateway.event_kinds 4 行")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化合成数据库")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--seed-rows", type=int, default=120_000)
    args = parser.parse_args()
    asyncio.run(initialize(args.dsn or Config().dsn, args.seed_rows))


if __name__ == "__main__":
    main()
