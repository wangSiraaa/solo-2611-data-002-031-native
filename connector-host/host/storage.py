# -*- coding: utf-8 -*-
"""SQLite 持久层：连接器目录、查询元数据、结果记录。

设计要点：
- 只有核对完整的查询才写 ``succeeded=1`` 的结果记录；未完成/失败的查询
  以 ``succeeded=0`` 保存元数据与失败原因，绝不冒充成功数据；
- 结果数据本身以 JSON Lines 落盘（一个查询一个文件，每行一条记录），
  数据库只保存路径、批数、记录数等清单信息；
- 写入使用短事务 + WAL，宿主主事件循环通过 ``run_in_executor`` 调用，不阻塞循环。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: str, results_dir: str):
        self.db_path = db_path
        self.results_dir = results_dir
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(results_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS connectors (
                    name             TEXT PRIMARY KEY,
                    command          TEXT NOT NULL,
                    capability       TEXT NOT NULL,   -- initialize 返回的能力 JSON
                    capabilities_ver TEXT NOT NULL,
                    supports_cancel  INTEGER NOT NULL,
                    max_inflight     INTEGER NOT NULL,
                    max_message_bytes INTEGER NOT NULL,
                    wait_timeout_s   REAL NOT NULL,
                    data_cap_bytes   INTEGER NOT NULL,
                    registered_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queries (
                    query_id        TEXT PRIMARY KEY,
                    connector       TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    fields_json     TEXT NOT NULL,    -- 请求的字段列表
                    filters_json    TEXT NOT NULL,    -- 受控过滤条件
                    params_json     TEXT NOT NULL,    -- 完整查询参数快照
                    capabilities_ver TEXT NOT NULL,   -- 当时连接器能力版本
                    status          TEXT NOT NULL,    -- running/succeeded/failed/cancelled
                    batches_total   INTEGER,          -- 终态响应声明的总批数
                    batches_seen    INTEGER NOT NULL DEFAULT 0,
                    records_total   INTEGER,          -- 终态响应声明的总记录数
                    records_saved   INTEGER NOT NULL DEFAULT 0,
                    result_path     TEXT,
                    fail_reason     TEXT,
                    terminal_state  TEXT,             -- 实际采用的终态（取消竞争时关键）
                    created_at      TEXT NOT NULL,
                    finished_at     TEXT
                );

                CREATE TABLE IF NOT EXISTS query_batches (
                    query_id   TEXT NOT NULL,
                    batch_no   INTEGER NOT NULL,
                    n_records  INTEGER NOT NULL,
                    bytes_len  INTEGER NOT NULL,
                    PRIMARY KEY (query_id, batch_no)
                );
                """
            )

    # ------------------------------------------------------------------ 目录
    def upsert_connector(self, name: str, command: str, config: dict, capability: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO connectors(name, command, capability, capabilities_ver,
                                      supports_cancel, max_inflight, max_message_bytes,
                                      wait_timeout_s, data_cap_bytes, registered_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    command=excluded.command,
                    capability=excluded.capability,
                    capabilities_ver=excluded.capabilities_ver,
                    supports_cancel=excluded.supports_cancel,
                    max_inflight=excluded.max_inflight,
                    max_message_bytes=excluded.max_message_bytes,
                    wait_timeout_s=excluded.wait_timeout_s,
                    data_cap_bytes=excluded.data_cap_bytes,
                    registered_at=excluded.registered_at
                """,
                (
                    name,
                    command,
                    json.dumps(capability, ensure_ascii=False),
                    str(capability.get("capabilitiesVersion", "unknown")),
                    1 if capability.get("supportsCancel") else 0,
                    int(config["max_inflight"]),
                    int(config["max_message_bytes"]),
                    float(config["wait_timeout_s"]),
                    int(config["data_cap_bytes"]),
                    _utcnow(),
                ),
            )

    def list_connectors(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM connectors ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 查询
    def insert_query(
        self,
        query_id: str,
        connector: str,
        source: str,
        fields: List[str],
        filters: dict,
        params: dict,
        capabilities_ver: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO queries(query_id, connector, source, fields_json, filters_json,
                                   params_json, capabilities_ver, status, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    query_id,
                    connector,
                    source,
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(filters, ensure_ascii=False),
                    json.dumps(params, ensure_ascii=False),
                    capabilities_ver,
                    "running",
                    _utcnow(),
                ),
            )

    def record_batch(self, query_id: str, batch_no: int, n_records: int, bytes_len: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO query_batches(query_id, batch_no, n_records, bytes_len) VALUES(?,?,?,?)",
                (query_id, batch_no, n_records, bytes_len),
            )
            self._conn.execute(
                "UPDATE queries SET batches_seen = batches_seen + 1, "
                "records_saved = records_saved + ? WHERE query_id = ?",
                (n_records, query_id),
            )

    def finish_query(
        self,
        query_id: str,
        status: str,
        batches_total: Optional[int],
        records_total: Optional[int],
        result_path: Optional[str],
        fail_reason: Optional[str],
        terminal_state: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE queries SET status=?, batches_total=?, records_total=?,
                                   result_path=?, fail_reason=?, terminal_state=?,
                                   finished_at=?
                WHERE query_id=?
                """,
                (
                    status,
                    batches_total,
                    records_total,
                    result_path,
                    fail_reason,
                    terminal_state,
                    _utcnow(),
                    query_id,
                ),
            )

    def get_query(self, query_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queries WHERE query_id=?", (query_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_queries(self, connector: Optional[str] = None) -> List[dict]:
        with self._lock:
            if connector:
                rows = self._conn.execute(
                    "SELECT * FROM queries WHERE connector=? ORDER BY created_at", (connector,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM queries ORDER BY created_at"
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 导出
    def export_manifest(self, out_path: str) -> Dict[str, Any]:
        """导出全部查询的清单（含成功结果路径与失败原因），返回清单对象。"""
        queries = self.list_queries()
        manifest = {
            "exported_at": _utcnow(),
            "database": os.path.abspath(self.db_path),
            "results_dir": os.path.abspath(self.results_dir),
            "query_count": len(queries),
            "queries": [],
        }
        for q in queries:
            item = {
                "query_id": q["query_id"],
                "connector": q["connector"],
                "source": q["source"],
                "fields": json.loads(q["fields_json"]),
                "filters": json.loads(q["filters_json"]),
                "params": json.loads(q["params_json"]),
                "capabilities_version": q["capabilities_ver"],
                "status": q["status"],
                "terminal_state": q["terminal_state"],
                "batches_total": q["batches_total"],
                "batches_seen": q["batches_seen"],
                "records_total": q["records_total"],
                "records_saved": q["records_saved"],
                "fail_reason": q["fail_reason"],
                "result_path": q["result_path"],
                "created_at": q["created_at"],
                "finished_at": q["finished_at"],
            }
            if q["result_path"] and os.path.exists(q["result_path"]):
                item["records"] = self._read_jsonl(q["result_path"])
            else:
                item["records"] = []
            manifest["queries"].append(item)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return manifest

    @staticmethod
    def _read_jsonl(path: str) -> List[dict]:
        out: List[dict] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
