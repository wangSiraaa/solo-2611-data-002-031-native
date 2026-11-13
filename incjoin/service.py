"""核心服务：批次应用、整快照装载与局部物化维护。

设计要点
--------
* 每个批次在一个 SERIALIZABLE 事务内完成：源表写入、连接明细增删改、
  汇总增删改、批次回执，要么全部生效，要么全部回滚。
* 物化维护是真正的局部维护：所有 SQL 都以本批 affected_keys 为过滤条件，
  未受影响键的 join_detail / key_summary 行（含 last_changed_batch）不会被触碰。
* 两侧同时改同一键时，不做“左右增量分别相加”，而是在两侧写入完成后，
  对受影响键统一做集合差/集合并：旧配对中已失效的删除、现存源表 JOIN
  出的全部配对 upsert，天然覆盖交叉新增与交叉删除。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

import psycopg
from psycopg import IsolationLevel
from psycopg.errors import (
    DeadlockDetected,
    NumericValueOutOfRange,
    SerializationFailure,
    UniqueViolation,
)

from . import batches as bmod
from .db import connect
from .errors import BatchConflictError, BatchValidationError

# SERIALIZABLE 冲突 / 死锁时的重试参数
_MAX_ATTEMPTS = 10
_BASE_DELAY_SECONDS = 0.02

# 崩溃测试钩子（只在测试环境设置）
_CRASH_BEFORE_COMMIT = "INCJOIN_CRASH_BEFORE_COMMIT"
_CRASH_AFTER_COMMIT = "INCJOIN_CRASH_AFTER_COMMIT"


class _Replay(Exception):
    """事务内发现 batch_id 已存在且内容一致：走幂等重放路径。"""


class _Conflict(Exception):
    """事务内发现 batch_id 已存在但内容不同：整批拒绝。"""


def _maybe_crash(env_var: str, batch_id: str) -> None:
    """模拟提交前后进程被杀；os._exit 不刷缓冲、不触发 finally/atexit。"""
    if os.environ.get(env_var) == batch_id:
        os._exit(137)


# ---------------------------------------------------------------------------
# 单条操作应用（按 seq 升序，调用方已排好序）
# ---------------------------------------------------------------------------

def _apply_operation(
    cur: psycopg.Cursor,
    op: dict[str, Any],
    affected: set[str],
    batch_id: str,
) -> None:
    side = op["side"]
    table = "l_source" if side == "L" else "r_source"
    row_id = op["row_id"]

    if op["action"] == "delete":
        cur.execute(
            f"SELECT join_key FROM {table} WHERE row_id = %s FOR UPDATE",
            (row_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise BatchValidationError(
                f"delete 失败：{side} 表中不存在 row_id={row_id!r}"
            )
        affected.add(row[0])
        cur.execute(f"DELETE FROM {table} WHERE row_id = %s", (row_id,))
        return

    # put：先取旧键（若存在），键迁移时旧键也必须进入受影响集合
    cur.execute(
        f"SELECT join_key FROM {table} WHERE row_id = %s FOR UPDATE",
        (row_id,),
    )
    row = cur.fetchone()
    if row is not None:
        affected.add(row[0])
    affected.add(op["join_key"])

    if side == "L":
        cur.execute(
            f"""
            INSERT INTO {table} (row_id, join_key, qty, last_changed_batch)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (row_id) DO UPDATE SET
                join_key = EXCLUDED.join_key,
                qty = EXCLUDED.qty,
                last_changed_batch = EXCLUDED.last_changed_batch
            """,
            (row_id, op["join_key"], op["qty"], batch_id),
        )
    else:
        cur.execute(
            f"""
            INSERT INTO {table} (row_id, join_key, unit_cents, last_changed_batch)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (row_id) DO UPDATE SET
                join_key = EXCLUDED.join_key,
                unit_cents = EXCLUDED.unit_cents,
                last_changed_batch = EXCLUDED.last_changed_batch
            """,
            (row_id, op["join_key"], op["unit_cents"], batch_id),
        )


def _maintain_materialized(
    cur: psycopg.Cursor, keys: list[str], batch_id: str
) -> None:
    """只针对受影响键做连接明细与汇总的局部维护。"""
    params = {"keys": keys, "batch_id": batch_id}

    # 1) 删除已失效配对：受影响键上，现存 L/R 源表按 join_key 相等
    #    已 JOIN 不出来的旧配对（删行、键迁移、被替换行导致的失效都在此处理）。
    cur.execute(
        """
        DELETE FROM join_detail d
        WHERE d.join_key = ANY(%(keys)s)
          AND NOT EXISTS (
                SELECT 1
                FROM l_source l
                JOIN r_source r ON r.join_key = l.join_key
                WHERE l.row_id = d.l_row_id
                  AND r.row_id = d.r_row_id
          )
        """,
        params,
    )

    # 2) 现存源表在受影响键上的全部现存配对做 upsert。
    #    两侧同时新增的交叉配对由这一条 JOIN 直接产生，不依赖两边增量相加。
    #    行内容未变化时 WHERE 条件为假，连 last_changed_batch 都不更新。
    cur.execute(
        """
        INSERT INTO join_detail
            (l_row_id, r_row_id, join_key, qty, unit_cents,
             amount_cents, last_changed_batch)
        SELECT l.row_id, r.row_id, l.join_key, l.qty, r.unit_cents,
               l.qty * r.unit_cents, %(batch_id)s
        FROM l_source l
        JOIN r_source r ON r.join_key = l.join_key
        WHERE l.join_key = ANY(%(keys)s)
        ON CONFLICT (l_row_id, r_row_id) DO UPDATE SET
            join_key = EXCLUDED.join_key,
            qty = EXCLUDED.qty,
            unit_cents = EXCLUDED.unit_cents,
            amount_cents = EXCLUDED.amount_cents,
            last_changed_batch = EXCLUDED.last_changed_batch
        WHERE join_detail.join_key       IS DISTINCT FROM EXCLUDED.join_key
           OR join_detail.qty            IS DISTINCT FROM EXCLUDED.qty
           OR join_detail.unit_cents     IS DISTINCT FROM EXCLUDED.unit_cents
           OR join_detail.amount_cents   IS DISTINCT FROM EXCLUDED.amount_cents
        """,
        params,
    )

    # 3) 汇总按受影响键从明细重算（数量按每个实际配对累加，不做来源去重）。
    cur.execute(
        """
        INSERT INTO key_summary
            (join_key, pair_count, qty_total, amount_total_cents,
             last_changed_batch)
        SELECT join_key, count(*), sum(qty), sum(amount_cents), %(batch_id)s
        FROM join_detail
        WHERE join_key = ANY(%(keys)s)
        GROUP BY join_key
        ON CONFLICT (join_key) DO UPDATE SET
            pair_count = EXCLUDED.pair_count,
            qty_total = EXCLUDED.qty_total,
            amount_total_cents = EXCLUDED.amount_total_cents,
            last_changed_batch = EXCLUDED.last_changed_batch
        WHERE key_summary.pair_count         IS DISTINCT FROM EXCLUDED.pair_count
           OR key_summary.qty_total          IS DISTINCT FROM EXCLUDED.qty_total
           OR key_summary.amount_total_cents IS DISTINCT FROM EXCLUDED.amount_total_cents
        """,
        params,
    )

    # 4) 最后一个配对被删除的键：汇总行一并删除（无配对即无汇总）。
    cur.execute(
        """
        DELETE FROM key_summary k
        WHERE k.join_key = ANY(%(keys)s)
          AND NOT EXISTS (
                SELECT 1 FROM join_detail d WHERE d.join_key = k.join_key
          )
        """,
        params,
    )


def _apply_once(
    conn: psycopg.Connection, doc: dict[str, Any], digest: str, canonical: str
) -> tuple[str, list[str]]:
    """在调用方提供的事务中执行整批；返回 (status, affected_keys)。"""
    batch_id = doc["batch_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM batch_receipt WHERE batch_id = %s FOR UPDATE",
            (batch_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing[0] == digest:
                raise _Replay()
            raise _Conflict()

        affected: set[str] = set(bmod.affected_keys(doc))
        for op in doc["operations"]:  # 已按 seq 升序
            _apply_operation(cur, op, affected, batch_id)

        keys = sorted(affected)
        if keys:
            _maintain_materialized(cur, keys, batch_id)

        cur.execute(
            """
            INSERT INTO batch_receipt
                (batch_id, content_hash, canonical_json, affected_keys,
                 op_count, status)
            VALUES (%s, %s, %s, %s, %s, 'committed')
            """,
            (
                batch_id,
                digest,
                canonical,
                json.dumps(keys, ensure_ascii=False),
                len(doc["operations"]),
            ),
        )
    return "committed", keys


def _fetch_receipt(dsn: str | None, batch_id: str) -> dict[str, Any] | None:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT batch_id, content_hash, canonical_json, affected_keys,
                   op_count, status
            FROM batch_receipt WHERE batch_id = %s
            """,
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "batch_id": row[0],
        "content_hash": row[1],
        "canonical_json": row[2],
        "affected_keys": row[3],
        "op_count": row[4],
        "status": row[5],
    }


def apply_batch(
    dsn: str | None,
    doc: dict[str, Any],
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    crash_hooks: bool = True,
) -> dict[str, Any]:
    """应用一个已校验批次，带 SERIALIZABLE 重试与幂等重放。"""
    batch_id = doc["batch_id"]
    digest = bmod.content_hash(doc)
    canonical = bmod.canonical_json(doc)

    # 快速路径：此前已提交过，事务外先判定，避免无谓的写事务
    prior = _fetch_receipt(dsn, batch_id)
    if prior is not None:
        if prior["content_hash"] != digest:
            raise BatchConflictError(
                f"batch_id={batch_id!r} 已提交但内容不同，拒绝覆盖"
            )
        return {"status": "replayed", "affected_keys": prior["affected_keys"],
                "receipt": prior}

    attempt = 0
    while True:
        attempt += 1
        conn = connect(dsn)
        conn.isolation_level = IsolationLevel.SERIALIZABLE
        try:
            try:
                status, keys = _apply_once(conn, doc, digest, canonical)
            except _Replay:
                conn.rollback()
                prior = _fetch_receipt(dsn, batch_id)
                return {"status": "replayed",
                        "affected_keys": prior["affected_keys"],
                        "receipt": prior}
            except _Conflict:
                conn.rollback()
                raise BatchConflictError(
                    f"batch_id={batch_id!r} 已提交但内容不同，拒绝覆盖"
                )
            except UniqueViolation:
                # 并发下两个事务同时插入同一 batch_id：败者判定内容是否一致
                conn.rollback()
                prior = _fetch_receipt(dsn, batch_id)
                if prior is not None and prior["content_hash"] == digest:
                    return {"status": "replayed",
                            "affected_keys": prior["affected_keys"],
                            "receipt": prior}
                raise BatchConflictError(
                    f"batch_id={batch_id!r} 已提交但内容不同，拒绝覆盖"
                )
            except NumericValueOutOfRange as exc:
                conn.rollback()
                raise BatchValidationError(
                    f"批次导致整数溢出（如 qty×unit_cents 超 64 位），整批回滚: {exc}"
                ) from exc

            if crash_hooks:
                _maybe_crash(_CRASH_BEFORE_COMMIT, batch_id)
            conn.commit()
            if crash_hooks:
                _maybe_crash(_CRASH_AFTER_COMMIT, batch_id)

            receipt = _fetch_receipt(dsn, batch_id)
            return {"status": status, "affected_keys": keys, "receipt": receipt}
        except (SerializationFailure, DeadlockDetected):
            conn.rollback()
            if attempt >= max_attempts:
                raise BatchConflictError(
                    f"批次 {batch_id!r} 并发冲突，重试 {max_attempts} 次仍失败"
                )
            # 指数退避 + 抖动，避免活锁
            delay = _BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            time.sleep(delay * (0.5 + random.random()))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 整快照装载（load）：单事务全量替换，物化结果从零重建
# ---------------------------------------------------------------------------

def load_snapshot(
    dsn: str | None, left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]]
) -> dict[str, int]:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE batch_receipt, key_summary, join_detail, "
                    "l_source, r_source")
        cur.executemany(
            "INSERT INTO l_source (row_id, join_key, qty, last_changed_batch) "
            "VALUES (%(row_id)s, %(join_key)s, %(qty)s, 'load')",
            left_rows,
        )
        cur.executemany(
            "INSERT INTO r_source (row_id, join_key, unit_cents, last_changed_batch) "
            "VALUES (%(row_id)s, %(join_key)s, %(unit_cents)s, 'load')",
            right_rows,
        )
        cur.execute(
            """
            INSERT INTO join_detail
                (l_row_id, r_row_id, join_key, qty, unit_cents,
                 amount_cents, last_changed_batch)
            SELECT l.row_id, r.row_id, l.join_key, l.qty, r.unit_cents,
                   l.qty * r.unit_cents, 'load'
            FROM l_source l
            JOIN r_source r ON r.join_key = l.join_key
            """
        )
        cur.execute(
            """
            INSERT INTO key_summary
                (join_key, pair_count, qty_total, amount_total_cents,
                 last_changed_batch)
            SELECT join_key, count(*), sum(qty), sum(amount_cents), 'load'
            FROM join_detail
            GROUP BY join_key
            """
        )
        conn.commit()

    return {
        "left_rows": len(left_rows),
        "right_rows": len(right_rows),
    }
