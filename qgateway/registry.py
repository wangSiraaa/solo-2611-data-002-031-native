"""每请求状态注册表与服务级状态机。

状态：QUEUED -> EXECUTING -> WAITING_FOR_CLIENT -> 终态（FINISHED_*）。
终态原因与帧协议的终态状态一一对应；断连导致终态发不出去时同样落在这里。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import frames

# 进行中状态
QUEUED = "QUEUED"
EXECUTING = "EXECUTING"
WAITING_FOR_CLIENT = "WAITING_FOR_CLIENT"

# 终态（值与 frames 终态 status 保持一致，便于关联）
FINISHED_SUCCESS = frames.T_SUCCESS
FINISHED_ROW_BUDGET = frames.T_ROW_BUDGET
FINISHED_BYTE_BUDGET = frames.T_BYTE_BUDGET
FINISHED_TIME_LIMIT = frames.T_TIME_LIMIT
FINISHED_QUERY_ERROR = frames.T_QUERY_ERROR
FINISHED_CANCELLED = frames.T_CANCELLED
FINISHED_CLIENT_DISCONNECTED = frames.T_CLIENT_DISCONNECTED
FINISHED_SEND_FAILED = frames.T_SEND_FAILED

ACTIVE_STATES = frozenset({QUEUED, EXECUTING, WAITING_FOR_CLIENT})
TERMINAL_STATES = frames.TERMINAL_REASONS


def now() -> float:
    return time.monotonic()


@dataclass
class RequestRecord:
    request_id: str
    query_id: str
    params: dict[str, Any]
    created_wall: float
    created_mono: float = field(default_factory=now)
    state: str = QUEUED
    state_since_mono: float = field(default_factory=now)
    # 各阶段实际耗时（秒）
    queued_seconds: float | None = None
    executing_seconds: float | None = None
    waiting_client_seconds: float = 0.0
    total_seconds: float | None = None
    # 数据库 backend 关联
    backend_pid: int | None = None
    backend_state: str | None = None
    backend_xact_start: str | None = None
    # 进度与背压观测
    rows_sent: int = 0
    bytes_sent: int = 0
    fetch_calls: int = 0
    rows_fetched: int = 0
    inflight_frames: int = 0
    inflight_bytes: int = 0
    peak_inflight_frames: int = 0
    peak_inflight_bytes: int = 0
    # 终态信息
    terminal_reason: str | None = None
    terminal_detail: str | None = None
    sqlstate: str | None = None
    terminal_delivered: bool = False
    last_error: str | None = None
    # 排队位置（提交瞬间）
    queue_depth_at_submit: int = 0

    def mark(self, state: str) -> None:
        current = now()
        if self.state == QUEUED and state != QUEUED:
            self.queued_seconds = current - self.state_since_mono
        if self.state == EXECUTING and state != EXECUTING:
            self.executing_seconds = current - self.state_since_mono
        if self.state == WAITING_FOR_CLIENT and state != WAITING_FOR_CLIENT:
            self.waiting_client_seconds += current - self.state_since_mono
        self.state = state
        self.state_since_mono = current

    def finish(self, reason: str, detail: str | None = None, sqlstate: str | None = None) -> None:
        self.mark(reason)
        self.terminal_reason = reason
        self.terminal_detail = detail
        self.sqlstate = sqlstate
        self.total_seconds = now() - self.created_mono

    def snapshot(self) -> dict[str, Any]:
        current = now()
        waiting = self.waiting_client_seconds
        if self.state == WAITING_FOR_CLIENT:
            waiting += current - self.state_since_mono
        return {
            "request_id": self.request_id,
            "query_id": self.query_id,
            "state": self.state,
            "active": self.state in ACTIVE_STATES,
            "created_wall": self.created_wall,
            "age_seconds": round(current - self.created_mono, 6),
            "queued_seconds": round(self.queued_seconds, 6) if self.queued_seconds is not None else None,
            "executing_seconds": round(self.executing_seconds, 6)
            if self.executing_seconds is not None
            else None,
            "waiting_client_seconds": round(waiting, 6),
            "total_seconds": round(self.total_seconds, 6) if self.total_seconds is not None else None,
            "backend_pid": self.backend_pid,
            "backend_state": self.backend_state,
            "backend_xact_start": self.backend_xact_start,
            "rows_sent": self.rows_sent,
            "bytes_sent": self.bytes_sent,
            "fetch_calls": self.fetch_calls,
            "rows_fetched": self.rows_fetched,
            "inflight_frames": self.inflight_frames,
            "inflight_bytes": self.inflight_bytes,
            "peak_inflight_frames": self.peak_inflight_frames,
            "peak_inflight_bytes": self.peak_inflight_bytes,
            "terminal_reason": self.terminal_reason,
            "terminal_detail": self.terminal_detail,
            "sqlstate": self.sqlstate,
            "terminal_delivered": self.terminal_delivered,
            "last_error": self.last_error,
            "queue_depth_at_submit": self.queue_depth_at_submit,
        }


class Registry:
    def __init__(self) -> None:
        self._records: dict[str, RequestRecord] = {}

    def add(self, record: RequestRecord) -> None:
        self._records[record.request_id] = record

    def get(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    def all(self) -> list[RequestRecord]:
        return list(self._records.values())

    def active(self) -> list[RequestRecord]:
        return [r for r in self._records.values() if r.state in ACTIVE_STATES]

    def snapshot(self) -> dict[str, Any]:
        records = sorted(self._records.values(), key=lambda r: r.created_mono)
        by_state: dict[str, int] = {}
        for rec in records:
            by_state[rec.state] = by_state.get(rec.state, 0) + 1
        return {"requests": [r.snapshot() for r in records], "counts_by_state": by_state}
