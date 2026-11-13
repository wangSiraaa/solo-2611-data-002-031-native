"""数据库连接与系统目录工具。"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager

import psycopg

from .config import Config, dsn

# $N -> %s，供 psycopg 执行；参数按出现顺序展开（支持同一参数多次出现）
_DOLLAR_RE = re.compile(r"\$(\d+)")


def to_pyformat(sql: str, params: list) -> tuple[str, list]:
    args: list = []

    def repl(m: re.Match) -> str:
        args.append(params[int(m.group(1)) - 1])
        return "%s"

    return _DOLLAR_RE.sub(repl, sql), args


@contextmanager
def connect(cfg: Config, schema: str | None = None, timeout_ms: int | None = None):
    conn = psycopg.connect(dsn(cfg), autocommit=True)
    try:
        if schema:
            conn.execute(f"SET search_path TO {schema}")
        if timeout_ms:
            conn.execute(f"SET statement_timeout = {int(timeout_ms)}")
        yield conn
    finally:
        conn.close()


def table_sizes(conn, schema: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT c.relname, pg_relation_size(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        """,
        (schema,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def list_experiment_objects(conn, cfg: Config) -> dict:
    """从系统目录核对本任务名下的全部对象（schema / 实验索引）。"""
    schemas = [
        r[0]
        for r in conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s ORDER BY 1",
            (cfg.tag + "\\_%",),
        ).fetchall()
    ]
    indexes = [
        {"name": r[0], "schema": r[1], "table": r[2], "size": r[3]}
        for r in conn.execute(
            """
            SELECT c.relname, n.nspname, t.relname, pg_relation_size(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            JOIN pg_class t ON t.oid = i.indrelid
            WHERE c.relname LIKE %s
            ORDER BY 1
            """,
            (cfg.index_prefix + "%",),
        ).fetchall()
    ]
    return {"schemas": schemas, "indexes": indexes}


def existing_index_keys(conn, schema: str) -> list[dict]:
    """已有索引的规范化定义：(表, 键列有序元组, INCLUDE 列元组, 是否主键)。"""
    rows = conn.execute(
        """
        SELECT t.relname, i.indisprimary, i.indnkeyatts, i.indkey::int2[], c.relname,
               am.amname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_am am ON am.oid = c.relam
        WHERE n.nspname = %s
        """,
        (schema,),
    ).fetchall()
    out = []
    for tname, is_pk, nkey, indkey, iname, amname in rows:
        attnos = list(indkey)
        by_num = {}
        for attno in attnos:
            by_num[attno] = conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum = %s",
                (f"{schema}.{tname}", attno),
            ).fetchone()[0]
        names = [by_num[a] for a in attnos]
        out.append(
            {
                "table": tname,
                "name": iname,
                "am": amname,
                "is_primary": is_pk,
                "key_cols": tuple(names[:nkey]),
                "include_cols": tuple(names[nkey:]),
            }
        )
    return out


def server_meta(conn) -> dict:
    ver = conn.execute("SELECT version()").fetchone()[0]
    settings = {}
    for k in (
        "shared_buffers",
        "work_mem",
        "max_parallel_workers_per_gather",
        "random_page_cost",
        "effective_cache_size",
        "default_statistics_target",
        "jit",
    ):
        settings[k] = conn.execute(
            "SELECT setting FROM pg_settings WHERE name = %s", (k,)
        ).fetchone()[0]
    return {"version": ver, "settings": settings}


def table_fingerprint(conn, schema: str, table: str) -> dict:
    """行数 + 全表扩展哈希，作为原始数据指纹记入实验元数据。"""
    row = conn.execute(
        f"SELECT count(*), coalesce(bit_xor(hashtextextended(t::text, 42)),0) "
        f"FROM {schema}.{table} t"
    ).fetchone()
    return {"rows": row[0], "hash": row[1]}


def timed(fn):
    t0 = time.perf_counter()
    res = fn()
    return res, time.perf_counter() - t0
