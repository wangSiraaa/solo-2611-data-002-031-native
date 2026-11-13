"""SQLite 持久化层。

可信断点的语义：**只有已经随业务记录在同一个事务里提交的 SSE id 才是可信
断点**。解析器看到 id 行、半个 JSON 都不会写库，因此重启后用
``Last-Event-ID`` 恢复时，重放的最多是最后一个完整事件，绝不会跳过任何一条。

单路采集只有一个写线程；状态查询可能从另一个进程只读打开，因此使用 WAL。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 每条流一行：状态、可信断点、统计与阻塞原因。
CREATE TABLE IF NOT EXISTS streams (
    stream_id        TEXT PRIMARY KEY,
    state            TEXT NOT NULL,            -- running/stopped_conflict/stopped_rotation/stopped_gap/stopped_error
    last_event_id    TEXT,                     -- 可信断点（已提交的最后一个 SSE id）
    last_sequence    INTEGER,                  -- 已提交的最大序号
    duplicate_count  INTEGER NOT NULL DEFAULT 0,
    reason           TEXT,                     -- 阻塞原因（人类可读）
    updated_at       TEXT NOT NULL
);

-- 业务记录。同流同序号唯一，冲突时绝不覆盖，原始行保持不动。
CREATE TABLE IF NOT EXISTS readings (
    stream_id    TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    event_id     TEXT NOT NULL,
    event_type   TEXT,
    raw_payload  TEXT NOT NULL,               -- 原始 data 文本（完整事件）
    payload_json TEXT NOT NULL,               -- 规范化 JSON（排序键、无空白），用于内容比对
    content_hash TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (stream_id, sequence)
);

-- 所有"不能自动继续"的事件都进这里：冲突、换流、保留窗口缺口、序号缺口、坏数据。
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,            -- conflict/rotation/retention_gap/gap/malformed
    stream_id       TEXT,
    new_stream_id   TEXT,
    sequence        INTEGER,
    event_id        TEXT,
    incoming_hash   TEXT,
    existing_hash   TEXT,
    incoming_payload TEXT,
    detail          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution      TEXT
);

-- 请求核对材料：每次 HTTP 连接的请求头（含 Last-Event-ID）与响应结果。
CREATE TABLE IF NOT EXISTS request_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    url           TEXT NOT NULL,
    request_headers TEXT NOT NULL,            -- JSON
    status_code   INTEGER,
    reason        TEXT,
    ended_at      TEXT,
    events_saved  INTEGER NOT NULL DEFAULT 0,
    dupes_saved   INTEGER NOT NULL DEFAULT 0
);

-- 人工恢复动作的审计记录。
CREATE TABLE IF NOT EXISTS recovery_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    stream_id   TEXT,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_hash(payload_text: str) -> tuple[str, str]:
    """返回 (规范化 JSON, sha256)。

    上游重发时缩进/空白可能不同，因此先解析再按排序键重新序列化后比对；
    解析失败则退化为按原文比对（malformed 会在调用方拦截）。
    """
    try:
        obj = json.loads(payload_text)
        canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except json.JSONDecodeError:
        canonical = payload_text
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


@dataclass
class SaveResult:
    """save_event 的结果，调用方据此决定是否继续该流。"""

    outcome: str                 # inserted / duplicate / conflict / rotation / gap / malformed
    stream_id: str
    sequence: int | None
    event_id: str | None
    detail: str = ""
    incident_id: int | None = None


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _apply_pragmas(self) -> None:
        # FULL 保证提交返回后内容已落盘（断电也不丢已提交事务）。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _init_schema(self) -> None:
        with self._tx() as cur:
            cur.executescript(SCHEMA)
            cur.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    # ------------------------------------------------------------------ #
    # 流状态
    # ------------------------------------------------------------------ #
    def get_stream(self, stream_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM streams WHERE stream_id = ?", (stream_id,)
        ).fetchone()

    def all_streams(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM streams ORDER BY stream_id"))

    def _upsert_running(
        self,
        cur: sqlite3.Cursor,
        stream_id: str,
        last_event_id: str,
        last_sequence: int,
    ) -> None:
        cur.execute(
            """
            INSERT INTO streams(stream_id, state, last_event_id, last_sequence,
                                duplicate_count, reason, updated_at)
            VALUES(?, 'running', ?, ?, 0, NULL, ?)
            ON CONFLICT(stream_id) DO UPDATE SET
                state='running',
                last_event_id=excluded.last_event_id,
                last_sequence=excluded.last_sequence,
                reason=NULL,
                updated_at=excluded.updated_at
            """,
            (stream_id, last_event_id, last_sequence, utcnow()),
        )

    def checkpoint_for(self) -> str | None:
        """客户端启动时使用的可信断点：取当前运行中流的已提交 Last-Event-ID。

        若所有流都处于停止态（冲突/换流/缺口），返回 None 且由调用方先检查
        状态——连接前 client 会拒绝在未人工恢复时重连。
        """
        row = self.conn.execute(
            "SELECT last_event_id FROM streams WHERE state='running' "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row["last_event_id"] if row and row["last_event_id"] else None

    # ------------------------------------------------------------------ #
    # 事件持久化（业务记录 + 可信断点 = 一个事务）
    # ------------------------------------------------------------------ #
    def save_event(
        self,
        event_id: str | None,
        event_type: str | None,
        data: str,
        retry_ms: int | None,
    ) -> SaveResult:
        now = utcnow()
        # 1) 先做不依赖事务的解析与 id 自洽校验。
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            return self._record_malformed(event_id, data, f"JSON 解析失败: {exc}")
        if not isinstance(payload, dict):
            return self._record_malformed(event_id, data, "data 不是 JSON 对象")

        stream_id = payload.get("stream_id")
        sequence = payload.get("sequence")
        if not isinstance(stream_id, str) or not stream_id:
            return self._record_malformed(event_id, data, "缺少合法 stream_id")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            return self._record_malformed(event_id, data, "缺少合法 sequence(>=1 的整数)")

        # SSE id 必须是 "流标识:序号"，且与 JSON 体内字段一致，防止错流错序入账。
        expected_id = f"{stream_id}:{sequence}"
        if event_id != expected_id:
            return self._record_malformed(
                event_id, data, f"SSE id 与载荷不一致（期望 {expected_id}）"
            )

        canonical, digest = canonical_hash(data)

        with self._tx() as cur:
            known = cur.execute(
                "SELECT state FROM streams WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            other = cur.execute(
                "SELECT stream_id FROM streams WHERE stream_id != ? LIMIT 1",
                (stream_id,),
            ).fetchone()

            # 2) 换流：已有别的流，且当前流不是已知流 -> 不能沿用旧流序号。
            if known is None and other is not None:
                detail = (
                    f"上游切换到新流 {stream_id}（事件 {event_id}）；"
                    f"旧流 {other['stream_id']} 的序号空间不可沿用，已停止等待人工确认"
                )
                cur.execute(
                    """
                    INSERT INTO incidents(kind, stream_id, new_stream_id, sequence,
                                          event_id, incoming_hash, existing_hash,
                                          incoming_payload, detail, created_at)
                    VALUES('rotation', ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        other["stream_id"], stream_id, sequence, event_id,
                        digest, data, detail, now,
                    ),
                )
                incident_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
                cur.execute(
                    "UPDATE streams SET state='stopped_rotation', reason=?, updated_at=? "
                    "WHERE stream_id=?",
                    (detail, now, other["stream_id"]),
                )
                return SaveResult("rotation", stream_id, sequence, event_id,
                                  detail, incident_id)

            # 3) 已停止的流不允许自动写入任何东西。
            if known is not None and known["state"] != "running":
                return SaveResult(
                    "blocked", stream_id, sequence, event_id,
                    f"流处于 {known['state']}，需人工恢复后才能继续",
                )

            existing = cur.execute(
                "SELECT content_hash, raw_payload FROM readings "
                "WHERE stream_id = ? AND sequence = ?",
                (stream_id, sequence),
            ).fetchone()

            if existing is not None:
                if existing["content_hash"] == digest:
                    # 4a) 同流同序号且内容相同 -> 去重；不写第二行，不推进断点。
                    cur.execute(
                        "UPDATE streams SET duplicate_count = duplicate_count + 1, "
                        "updated_at=? WHERE stream_id=?",
                        (now, stream_id),
                    )
                    return SaveResult(
                        "duplicate", stream_id, sequence, event_id,
                        "内容一致的重发事件，已去重",
                    )
                # 4b) 同流同序号但内容不同。若该序号的冲突已被人工裁决
                # （保留原始），之后任何重发都按"已知差异的重复"处理，不再阻塞。
                resolved_conflict = cur.execute(
                    "SELECT id FROM incidents WHERE kind='conflict' AND stream_id=? "
                    "AND sequence=? AND resolved_at IS NOT NULL LIMIT 1",
                    (stream_id, sequence),
                ).fetchone()
                if resolved_conflict is not None:
                    cur.execute(
                        "UPDATE streams SET duplicate_count = duplicate_count + 1, "
                        "updated_at=? WHERE stream_id=?",
                        (now, stream_id),
                    )
                    return SaveResult(
                        "duplicate", stream_id, sequence, event_id,
                        "冲突序号已人工裁决（保留原始），重发按重复计数",
                    )
                # 未裁决冲突：原始记录不动，流立即停止。
                detail = (
                    f"序号 {sequence} 冲突：已存内容与新到内容哈希不同；"
                    f"保留原始记录，停止该流"
                )
                cur.execute(
                    """
                    INSERT INTO incidents(kind, stream_id, new_stream_id, sequence,
                                          event_id, incoming_hash, existing_hash,
                                          incoming_payload, detail, created_at)
                    VALUES('conflict', ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id, sequence, event_id, digest,
                        existing["content_hash"], data, detail, now,
                    ),
                )
                incident_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
                cur.execute(
                    "UPDATE streams SET state='stopped_conflict', reason=?, updated_at=? "
                    "WHERE stream_id=?",
                    (detail, now, stream_id),
                )
                return SaveResult("conflict", stream_id, sequence, event_id,
                                  detail, incident_id)

            # 5) 新序号。防御性检查序号连续性（上游保证递增；缺号即显式缺口）。
            last = cur.execute(
                "SELECT last_sequence FROM streams WHERE stream_id=?", (stream_id,)
            ).fetchone()
            if last is not None and last["last_sequence"] is not None:
                expected_seq = last["last_sequence"] + 1
                if sequence != expected_seq:
                    detail = (
                        f"序号缺口：期望 {expected_seq}，实际收到 {sequence}（事件 {event_id}）"
                    )
                    cur.execute(
                        """
                        INSERT INTO incidents(kind, stream_id, new_stream_id, sequence,
                                              event_id, incoming_hash, existing_hash,
                                              incoming_payload, detail, created_at)
                        VALUES('gap', ?, NULL, ?, ?, ?, NULL, ?, ?, ?)
                        """,
                        (stream_id, sequence, event_id, digest, data, detail, now),
                    )
                    incident_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
                    cur.execute(
                        "UPDATE streams SET state='stopped_gap', reason=?, updated_at=? "
                        "WHERE stream_id=?",
                        (detail, now, stream_id),
                    )
                    return SaveResult("gap", stream_id, sequence, event_id,
                                      detail, incident_id)

            # 6) 正常入账：业务记录与可信断点在同一事务提交。
            cur.execute(
                """
                INSERT INTO readings(stream_id, sequence, event_id, event_type,
                                     raw_payload, payload_json, content_hash, received_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (stream_id, sequence, event_id, event_type, data, canonical,
                 digest, now),
            )
            self._upsert_running(cur, stream_id, event_id, sequence)
            return SaveResult("inserted", stream_id, sequence, event_id)

    def _record_malformed(self, event_id: str | None, data: str, detail: str) -> SaveResult:
        now = utcnow()
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO incidents(kind, stream_id, new_stream_id, sequence,
                                      event_id, incoming_hash, existing_hash,
                                      incoming_payload, detail, created_at)
                VALUES('malformed', NULL, NULL, NULL, ?, NULL, NULL, ?, ?, ?)
                """,
                (event_id, data, detail, now),
            )
            incident_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
        return SaveResult("malformed", "", None, event_id, detail, incident_id)

    # ------------------------------------------------------------------ #
    # 410 保留窗口缺口
    # ------------------------------------------------------------------ #
    def record_retention_gap(self, last_event_id: str | None, body: str) -> int:
        """上游 410：可信断点已落在保留窗口之外，显式记缺口并停止，禁止偷偷续接。"""
        now = utcnow()
        detail = (
            f"上游返回 410（保留窗口之外存在缺口），当前可信断点 {last_event_id!r}；"
            f"响应体: {body[:500]}"
        )
        with self._tx() as cur:
            row = cur.execute(
                "SELECT stream_id FROM streams WHERE state='running' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            stream_id = row["stream_id"] if row else None
            cur.execute(
                """
                INSERT INTO incidents(kind, stream_id, new_stream_id, sequence,
                                      event_id, incoming_hash, existing_hash,
                                      incoming_payload, detail, created_at)
                VALUES('retention_gap', ?, NULL, NULL, ?, NULL, NULL, ?, ?, ?)
                """,
                (stream_id, last_event_id, body, detail, now),
            )
            incident_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
            if stream_id is not None:
                cur.execute(
                    "UPDATE streams SET state='stopped_gap', reason=?, updated_at=? "
                    "WHERE stream_id=?",
                    (detail, now, stream_id),
                )
        return incident_id

    # ------------------------------------------------------------------ #
    # 人工恢复入口
    # ------------------------------------------------------------------ #
    def recover_gap(
        self,
        incident_id: int,
        *,
        accept_restart: bool,
        abandon: bool,
        note: str,
    ) -> None:
        """retention_gap / gap 的人工处置。

        - accept_restart=True：接受缺口、放弃连续性声明，从上游当前可用位置重新
          开始（重连时不带 Last-Event-ID；重发的旧序号由去重逻辑拦截）。
        - abandon=True：保持停止，仅记录人工裁决。
        """
        if accept_restart == abandon:
            raise ValueError("必须且只能选择一种处置：--accept-restart 或 --abandon")
        now = utcnow()
        with self._tx() as cur:
            inc = cur.execute(
                "SELECT * FROM incidents WHERE id=? AND kind IN ('retention_gap','gap')",
                (incident_id,),
            ).fetchone()
            if inc is None:
                raise ValueError(f"incident {incident_id} 不存在或不是缺口类型")
            if inc["resolved_at"] is not None:
                raise ValueError(f"incident {incident_id} 已处置过")
            if abandon:
                resolution = "abandoned: " + note
                cur.execute(
                    "UPDATE incidents SET resolved_at=?, resolution=? WHERE id=?",
                    (now, resolution, incident_id),
                )
            else:
                resolution = "accepted_restart: " + note
                cur.execute(
                    "UPDATE incidents SET resolved_at=?, resolution=? WHERE id=?",
                    (now, resolution, incident_id),
                )
                if inc["stream_id"]:
                    # 清空该流可信断点：下一次连接不带 Last-Event-ID，从上游最早
                    # 可用事件重新拉取；已落库记录保留，重复部分自动去重。
                    cur.execute(
                        "UPDATE streams SET state='running', last_event_id=NULL, "
                        "last_sequence=NULL, reason=NULL, updated_at=? WHERE stream_id=?",
                        (now, inc["stream_id"]),
                    )
            cur.execute(
                "INSERT INTO recovery_log(action, stream_id, detail, created_at) "
                "VALUES('recover_gap', ?, ?, ?)",
                (inc["stream_id"], f"incident={incident_id} {resolution}", now),
            )

    def adopt_stream(self, incident_id: int, note: str) -> str:
        """换流人工确认：启用新流，序号从 1 重新开始；旧流保留为停止态。"""
        now = utcnow()
        with self._tx() as cur:
            inc = cur.execute(
                "SELECT * FROM incidents WHERE id=? AND kind='rotation'",
                (incident_id,),
            ).fetchone()
            if inc is None:
                raise ValueError(f"incident {incident_id} 不存在或不是换流类型")
            if inc["resolved_at"] is not None:
                raise ValueError(f"incident {incident_id} 已处置过")
            new_stream = inc["new_stream_id"]
            cur.execute(
                "UPDATE incidents SET resolved_at=?, resolution=? WHERE id=?",
                (now, "adopted: " + note, incident_id),
            )
            # 新流是全新序号空间：不复制旧流断点。
            cur.execute(
                """
                INSERT INTO streams(stream_id, state, last_event_id, last_sequence,
                                    duplicate_count, reason, updated_at)
                VALUES(?, 'running', NULL, NULL, 0, NULL, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    state='running', reason=NULL, updated_at=excluded.updated_at
                """,
                (new_stream, now),
            )
            cur.execute(
                "INSERT INTO recovery_log(action, stream_id, detail, created_at) "
                "VALUES('adopt_stream', ?, ?, ?)",
                (new_stream, f"incident={incident_id} {note}", now),
            )
            return new_stream

    def resolve_conflict(self, incident_id: int, note: str) -> None:
        """冲突人工确认：策略固定为保留原始记录；确认后解除阻塞以便继续拉新事件。

        冲突事件本身（不同内容的重发）保留在 incidents 中，不覆盖 readings。
        """
        now = utcnow()
        with self._tx() as cur:
            inc = cur.execute(
                "SELECT * FROM incidents WHERE id=? AND kind='conflict'",
                (incident_id,),
            ).fetchone()
            if inc is None:
                raise ValueError(f"incident {incident_id} 不存在或不是冲突类型")
            if inc["resolved_at"] is not None:
                raise ValueError(f"incident {incident_id} 已处置过")
            cur.execute(
                "UPDATE incidents SET resolved_at=?, resolution=? WHERE id=?",
                (now, "kept_original: " + note, incident_id),
            )
            # 断点仍是冲突序号之前那条已提交记录；重连后上游若再重发该冲突
            # 事件，save_event 会查到本 resolved incident，按重复计数而不是再次
            # 阻塞；其后的新序号可正常入账。
            cur.execute(
                "UPDATE streams SET state='running', reason=NULL, updated_at=? "
                "WHERE stream_id=?",
                (now, inc["stream_id"]),
            )
            cur.execute(
                "INSERT INTO recovery_log(action, stream_id, detail, created_at) "
                "VALUES('resolve_conflict', ?, ?, ?)",
                (inc["stream_id"], f"incident={incident_id} kept_original {note}", now),
            )

    # ------------------------------------------------------------------ #
    # 请求日志（核对材料）
    #
    # 与业务记录共用同一个连接，但用独立事务立即提交：单路采集只有一个写
    # 线程，同一连接上的事务互不嵌套。请求开始就提交一次，即便随后进程被
    # SIGKILL，"发起过什么请求、带了什么头"也已落盘；业务事务的原子性仍由
    # save_event 自己的 BEGIN/COMMIT 保证，两者不共享事务。
    # ------------------------------------------------------------------ #
    def log_request_start(self, url: str, headers: dict[str, str]) -> int:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO request_log(started_at, url, request_headers, "
                "status_code, reason, ended_at) VALUES(?, ?, ?, NULL, NULL, NULL)",
                (utcnow(), url, json.dumps(headers, ensure_ascii=False, indent=2)),
            )
            return cur.lastrowid

    def log_request_end(
        self,
        request_id: int,
        status_code: int | None,
        reason: str,
        events_saved: int,
        dupes_saved: int,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE request_log SET status_code=?, reason=?, ended_at=?, "
                "events_saved=?, dupes_saved=? WHERE id=?",
                (status_code, reason, utcnow(), events_saved, dupes_saved, request_id),
            )

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        streams = [dict(r) for r in self.all_streams()]
        open_incidents = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, kind, stream_id, new_stream_id, sequence, event_id, "
                "detail, created_at FROM incidents WHERE resolved_at IS NULL "
                "ORDER BY id"
            )
        ]
        counts = {
            "readings": self.conn.execute(
                "SELECT COUNT(*) c FROM readings"
            ).fetchone()["c"],
            "incidents": self.conn.execute(
                "SELECT COUNT(*) c FROM incidents"
            ).fetchone()["c"],
            "open_incidents": len(open_incidents),
            "requests": self.conn.execute(
                "SELECT COUNT(*) c FROM request_log"
            ).fetchone()["c"],
        }
        return {
            "db_path": self.path,
            "generated_at": utcnow(),
            "streams": streams,
            "open_incidents": open_incidents,
            "counts": counts,
        }

    def readings(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT stream_id, sequence, event_id, event_type, raw_payload, "
                "received_at FROM readings ORDER BY stream_id, sequence LIMIT ?",
                (limit,),
            )
        )
