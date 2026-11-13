"""单路采集客户端：HTTP/SSE 连接、续接与退避重连。

设计约束：

- 每次连接都把**数据库里已提交的** Last-Event-ID 放进请求头；没有可信断点时
  不带该头。解析器内部的 id 缓冲不参与续接决策。
- 连接失败 / 对端正常结束都按同一套指数退避重连，避免忙循环；有最大尝试次数、
  总时限和外部停止文件三重停止条件。
- 410 表示可信断点已在保留窗口之外：记录 retention_gap 并停止，绝不"从当前
  数据继续还宣称连续"。
- 存储层返回 conflict/rotation/gap/malformed/blocked 时立即结束本次连接并退出
  重连循环，等人工恢复。
- 测试钩子（仅环境变量触发，正常运行不存在）：
  COLLECTOR_CRASH_AFTER_EVENT=N 在序号 N 的完整事件**接收后、提交前**自杀；
  COLLECTOR_CRASH_AFTER_COMMIT=N 在序号 N 的事务**提交成功后**自杀。
  两者分别对应题面要求的两个崩溃时点。
"""

from __future__ import annotations

import http.client
import json
import os
import random
import signal
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .sse import SSEParser
from .storage import SaveResult, Storage

STOP_OUTCOMES = {"conflict", "rotation", "gap", "malformed", "blocked"}


@dataclass
class CollectorConfig:
    url: str
    db_path: str
    initial_backoff: float = 1.0       # 首次重连等待秒数
    max_backoff: float = 30.0         # 退避上限
    backoff_factor: float = 2.0
    max_attempts: int | None = None   # 连接尝试次数上限（None=不限，配合 max_run_seconds）
    max_run_seconds: float | None = None
    stop_file: str | None = None      # 该文件出现即优雅停止
    read_size: int = 4096
    # 测试钩子，默认关闭。
    crash_after_event: int | None = None
    crash_after_commit: int | None = None

    @classmethod
    def from_env(cls, url: str, db_path: str, **kw) -> "CollectorConfig":
        def _int(name: str) -> int | None:
            raw = os.environ.get(name)
            return int(raw) if raw else None
        return cls(
            url=url,
            db_path=db_path,
            crash_after_event=_int("COLLECTOR_CRASH_AFTER_EVENT"),
            crash_after_commit=_int("COLLECTOR_CRASH_AFTER_COMMIT"),
            **kw,
        )


class CollectorStopped(Exception):
    """外部停止（信号/停止文件/达到上限）。"""


@dataclass
class RunSummary:
    attempts: int = 0
    events_inserted: int = 0
    duplicates: int = 0
    stop_reason: str = ""
    last_error: str | None = None
    # 本次运行中各流是否曾处于阻塞态（供 CLI 决定退出码）。
    blocked: bool = False
    request_ids: list[int] = field(default_factory=list)


class Collector:
    def __init__(self, config: CollectorConfig, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage or Storage(config.db_path)
        self._owns_storage = storage is None
        self._shutdown = False
        # 上游最近一次 retry 行给出的重连等待（秒）；SSE 规范要求重连间隔
        # 不小于该值，这里作为退避下限，仍受 max_backoff 上限约束。
        self._server_retry: float | None = None
        parts = urlsplit(config.url)
        if parts.scheme not in ("http", "https"):
            raise ValueError("仅支持 http(s) URL")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._path = parts.path or "/"
        if parts.query:
            self._path += "?" + parts.query

    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        self._shutdown = True

    def _install_signals(self) -> None:
        def _handler(signum, _frame):
            self._shutdown = True
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    # ------------------------------------------------------------------ #
    def run(self) -> RunSummary:
        self._install_signals()
        summary = RunSummary()
        started = time.monotonic()
        attempt = 0
        failures = 0  # 连续失败计数，用于指数退避；成功连上并读到数据后回落

        while not self._shutdown:
            if self.config.stop_file and os.path.exists(self.config.stop_file):
                summary.stop_reason = "stop_file"
                break
            if self.config.max_attempts is not None and attempt >= self.config.max_attempts:
                summary.stop_reason = "max_attempts"
                break
            if self.config.max_run_seconds is not None and \
                    time.monotonic() - started >= self.config.max_run_seconds:
                summary.stop_reason = "max_run_seconds"
                break

            # 存在未处置 incident 且关联流处于停止态时不发起连接。注意：换流
            # 确认后旧流会永久保留为 stopped_rotation（历史流），其 incident 已
            # 处置，不应阻塞新流。
            status = self.storage.status()
            open_stream_ids = {
                i.get("stream_id") for i in status["open_incidents"]
                if i.get("stream_id")
            }
            blocked = [
                s for s in status["streams"]
                if s["state"] != "running" and s["stream_id"] in open_stream_ids
            ]
            if blocked:
                summary.blocked = True
                summary.stop_reason = "awaiting_manual_recovery:" + ",".join(
                    f"{s['stream_id']}({s['state']})" for s in blocked
                )
                break

            attempt += 1
            summary.attempts = attempt
            reason, made_progress = self._one_attempt(summary)
            if self._shutdown:
                summary.stop_reason = summary.stop_reason or "signal"
                break
            if reason in STOP_OUTCOMES:
                summary.blocked = True
                summary.stop_reason = reason
                break
            if reason == "retention_gap":
                summary.blocked = True
                summary.stop_reason = "retention_gap"
                break

            # 正常结束或网络错误：退避后重连。
            if made_progress:
                failures = 0
                delay = self.config.initial_backoff
            else:
                failures += 1
                delay = min(
                    self.config.max_backoff,
                    self.config.initial_backoff * (self.config.backoff_factor ** (failures - 1)),
                )
            # 上游 retry 行是规范层面的重连等待下限（夹在配置上下限之间）。
            if self._server_retry is not None:
                delay = max(delay, min(self._server_retry, self.config.max_backoff))
            # 抖动，避免与对端节奏完全同步；用可中断的睡眠响应停止信号。
            delay = delay * (0.5 + random.random() * 0.5)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if self._shutdown or (
                    self.config.stop_file and os.path.exists(self.config.stop_file)
                ):
                    break
                time.sleep(min(0.2, deadline - time.monotonic()))

        if not summary.stop_reason:
            summary.stop_reason = "shutdown"
        if self._owns_storage:
            self.storage.close()
        return summary

    # ------------------------------------------------------------------ #
    def _one_attempt(self, summary: RunSummary) -> tuple[str, bool]:
        """返回 (结束原因, 本次是否有进展)。"""
        last_event_id = self.storage.checkpoint_for()
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "close",
        }
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        request_id = self.storage.log_request_start(self.config.url, headers)
        summary.request_ids.append(request_id)

        conn_cls = (
            http.client.HTTPSConnection if self._scheme == "https"
            else http.client.HTTPConnection
        )
        conn = conn_cls(self._host, self._port, timeout=30)
        saved = 0
        dupes = 0
        made_progress = False
        try:
            conn.request("GET", self._path, headers=headers)
            resp = conn.getresponse()

            if resp.status == 410:
                body = resp.read().decode("utf-8", errors="replace")
                self.storage.record_retention_gap(last_event_id, body)
                self.storage.log_request_end(request_id, 410, "retention_gap", 0, 0)
                return "retention_gap", False
            if resp.status != 200:
                body = resp.read(2000).decode("utf-8", errors="replace")
                reason = f"HTTP {resp.status}: {body[:200]}"
                self.storage.log_request_end(request_id, resp.status, reason, 0, 0)
                summary.last_error = reason
                return "http_error", False

            parser = SSEParser()
            while not self._shutdown:
                chunk = resp.read(self.config.read_size)
                if not chunk:
                    break
                made_progress = True
                # retry 行可能出现在仅含注释的心跳块中（不派发事件），因此
                # 每轮读取后都从解析器同步一次，而不是只在拿到事件时同步。
                if parser.retry_ms is not None:
                    self._server_retry = parser.retry_ms / 1000.0
                for event in parser.feed(chunk):
                    result = self._handle_event(event, summary)
                    if result.outcome == "inserted":
                        saved += 1
                    elif result.outcome == "duplicate":
                        dupes += 1
                    if result.outcome in STOP_OUTCOMES:
                        self.storage.log_request_end(
                            request_id, resp.status, result.outcome, saved, dupes
                        )
                        return result.outcome, made_progress

            # 对端关闭：严格丢弃未以空行结束的半条消息（不派发即不推进断点）。
            parser.end_stream()
            self.storage.log_request_end(
                request_id, resp.status, "stream_ended", saved, dupes
            )
            return "stream_ended", made_progress
        except (http.client.HTTPException, OSError) as exc:
            # 断流、重置、超时等：已提交的事务不受影响，按退避重连即可。
            reason = f"{type(exc).__name__}: {exc}"
            summary.last_error = reason
            try:
                self.storage.log_request_end(request_id, None, reason, saved, dupes)
            except Exception:
                pass
            return "network_error", made_progress
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    def _handle_event(self, event, summary: RunSummary) -> SaveResult:
        seq = self._event_sequence(event)
        # 崩溃钩子 1：完整事件已解析、尚未提交。此时杀掉进程，数据库里既没有
        # 业务行也没有断点推进，重启后上游重发该事件，正常入账。
        # 只对能解析出有效序号的事件生效，malformed 事件不触发。
        if (
            seq is not None
            and self.config.crash_after_event is not None
            and seq == self.config.crash_after_event
        ):
            self._hard_exit(
                f"测试钩子：事件 {event.event_id} 接收后、提交前终止 (SIGKILL 语义)"
            )

        result = self.storage.save_event(
            event.event_id, event.event, event.data, event.retry_ms
        )
        if result.outcome == "inserted":
            summary.events_inserted += 1
        elif result.outcome == "duplicate":
            summary.duplicates += 1

        # 崩溃钩子 2：事务已提交（业务记录与断点已一致落盘）。此时杀掉进程，
        # 重启后 Last-Event-ID 指向该事件，上游重发时内容一致 -> 去重，不重复入账。
        if (
            result.sequence is not None
            and self.config.crash_after_commit is not None
            and result.sequence == self.config.crash_after_commit
            and result.outcome in ("inserted", "duplicate")
        ):
            self._hard_exit(
                f"测试钩子：事件 {event.event_id} 提交后终止 (SIGKILL 语义)"
            )
        return result

    @staticmethod
    def _event_sequence(event) -> int | None:
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        seq = payload.get("sequence")
        # 显式排除 bool（True 也是 int）。
        if isinstance(seq, int) and not isinstance(seq, bool):
            return seq
        return None

    @staticmethod
    def _hard_exit(message: str) -> None:
        # os._exit 模拟 SIGKILL：不执行任何 Python 清理、atexit、缓冲 flush，
        # 事务的持久性完全由 SQLite 自己保证（已 commit 的不丢，未 commit 的回滚）。
        import sys
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        os._exit(137)
