#!/usr/bin/env python3
"""Bounded, source-isolated GET-only forwarding proxy.

Design in one paragraph
-----------------------
A single asyncio event loop owns all state.  Every logical request gets one
absolute deadline at TCP accept time (config ``total_budget_ms``); queueing,
connecting, and reading the whole upstream response all happen under that one
deadline.  Admission is per source: a source has a hard in-flight cap and a
hard waiting-queue cap.  The scheduler is round-robin over sources and only
hands a source a slot when that source is below its own cap; therefore the
per-source caps are hard reservations and a backed-up source can never consume
another source's capacity.  A request is dispatched only if it is still alive
and still has budget.  Expired or cancelled requests are removed from the
queue synchronously (no await) and can never be sent upstream afterwards.
Slot release is guarded by the ``Request.slot`` flag and happens exactly once
even when completion, deadline and client cancellation race.

Only ``GET /item?source=<configured-source>`` (or the ``X-Source`` header) is
accepted.  Absolute URLs, unknown sources and other paths are rejected before
any upstream connection is made.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_HEAD_BYTES = 32 * 1024
MAX_LINE_BYTES = 8 * 1024
MAX_BODY_BYTES = 64 * 1024
CLIENT_WRITE_GRACE_S = 0.25

TERMINAL_REASONS = (
    "success",
    "queue_full",
    "deadline",
    "client_cancelled",
    "upstream_error",
    "shutdown",
)

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class BadRequest(Exception):
    pass


class SourceState:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        origin = cfg["origin"]
        parts = urlsplit(origin)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"source {name!r}: only http://host:port origins are supported")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.max_inflight = int(cfg["max_inflight"])
        self.queue_limit = int(cfg["queue_limit"])
        if self.max_inflight < 1:
            raise ValueError(f"source {name!r}: max_inflight must be >= 1")
        if self.queue_limit < 0:
            raise ValueError(f"source {name!r}: queue_limit must be >= 0")
        self.queue: deque[Request] = deque()
        self.inflight = 0
        self.max_inflight_observed = 0
        self.metrics = {
            "received": 0,
            "dispatched": 0,       # request bytes actually written to the origin
            "slot_assignments": 0,
            "ended": {r: 0 for r in TERMINAL_REASONS},
        }


class Request:
    __slots__ = (
        "rid", "source", "reader", "writer", "connected_at", "deadline",
        "state", "slot", "finished", "terminal", "cancel_reason", "waiter",
        "task", "client_gone", "upstream_writer", "dead", "run_task",
    )

    def __init__(self, rid: str, source: str, reader, writer, connected_at, budget_s):
        self.rid = rid
        self.source = source
        self.reader = reader
        self.writer = writer
        self.connected_at = connected_at
        self.deadline = connected_at + budget_s
        self.state = "new"        # new -> queued -> inflight -> finished
        self.slot = False         # owns one per-source + one global in-flight slot
        self.finished = False
        self.terminal = None
        self.cancel_reason = None  # set by the client watcher or shutdown
        self.waiter = None
        self.task = None
        self.client_gone = False
        self.upstream_writer = None
        self.dead = False
        self.run_task = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class Proxy:
    def __init__(self, config: dict, host: str, port: int,
                 stats_file: Path | None, event_file: Path | None,
                 shutdown_grace_s: float):
        self.sources: dict[str, SourceState] = {}
        for name, cfg in config["sources"].items():
            self.sources[name] = SourceState(name, cfg)
        if not self.sources:
            raise ValueError("config has no sources")
        self.source_order = list(self.sources)
        self.global_max = int(config["global_max_inflight"])
        if self.global_max < 1:
            raise ValueError("global_max_inflight must be >= 1")
        self.budget_s = float(config["total_budget_ms"]) / 1000.0
        if self.budget_s <= 0:
            raise ValueError("total_budget_ms must be positive")
        # The whole isolation guarantee is "each source is guaranteed its own
        # in-flight slots".  That is only true if the global cap can hold the
        # sum of the per-source caps; otherwise a source with a large cap could
        # still occupy every global slot.  Refuse to start rather than silently
        # degrade the isolation promised in the diagnostics.
        demand = sum(s.max_inflight for s in self.sources.values())
        if demand > self.global_max:
            raise ValueError(
                f"global_max_inflight={self.global_max} cannot hold the sum of "
                f"per-source max_inflight={demand}; a source could then consume "
                f"another source's reserved slots")
        self.allowed_path = config["allowed_path"]
        self.host = host
        self.port = port
        self.stats_file = stats_file
        self.event_file = event_file
        self.shutdown_grace_s = shutdown_grace_s

        self.global_inflight = 0
        self.global_metrics = {
            "received": 0,
            "dispatched": 0,
            "ended": {r: 0 for r in TERMINAL_REASONS},
            "bad_requests": 0,
            "delivery_failed": 0,   # upstream success but client write failed
        }
        self.rr_next = 0                       # round-robin cursor
        self.shutting_down = False
        self.shutdown_event = asyncio.Event()
        self.server = None
        self.tasks: set[asyncio.Task] = set()      # connection handlers
        self.runs: set[asyncio.Task] = set()       # per-request work tasks
        self.handler_recs: dict[asyncio.Task, Request | None] = {}
        self.requests: dict[str, Request] = {}
        self.client_writers: set = set()
        self.upstream_writers: set = set()
        self.started_at = time.monotonic()
        self._event_fh = None
        if event_file:
            event_file.parent.mkdir(parents=True, exist_ok=True)
            self._event_fh = open(event_file, "a", buffering=1)

    # ------------------------------------------------------------------ log
    def event(self, kind: str, **fields):
        if self._event_fh is None:
            return
        rec = {
            "wall": time.time(),
            "monotonic": time.monotonic(),
            "event": kind,
            **fields,
        }
        self._event_fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._event_fh.flush()

    def snapshot(self) -> dict:
        srcs = {}
        for name, s in self.sources.items():
            srcs[name] = {
                "origin": f"http://{s.host}:{s.port}",
                "max_inflight": s.max_inflight,
                "queue_limit": s.queue_limit,
                "received": s.metrics["received"],
                "dispatched": s.metrics["dispatched"],
                "inflight": s.inflight,
                "queued": self._live_queue_len(s),
                "slot_assignments": s.metrics["slot_assignments"],
                "max_inflight_observed": s.max_inflight_observed,
                "ended": dict(s.metrics["ended"]),
            }
        return {
            "wall": time.time(),
            "monotonic": time.monotonic(),
            "uptime_s": round(time.monotonic() - self.started_at, 3),
            "global_max_inflight": self.global_max,
            "global_inflight": self.global_inflight,
            "global": {
                "received": self.global_metrics["received"],
                "dispatched": self.global_metrics["dispatched"],
                "ended": dict(self.global_metrics["ended"]),
                "bad_requests": self.global_metrics["bad_requests"],
                "delivery_failed": self.global_metrics["delivery_failed"],
            },
            "sources": srcs,
        }

    def save_stats(self):
        if self.stats_file is None:
            return
        try:
            self.stats_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.stats_file.with_suffix(self.stats_file.suffix + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=2))
            os.replace(tmp, self.stats_file)
        except OSError:
            pass

    @staticmethod
    def _live_queue_len(src: SourceState) -> int:
        return sum(1 for r in src.queue if not r.finished and not getattr(r, "dead", False))

    # -------------------------------------------------------------- scheduler
    def submit(self, rec: Request) -> str:
        """Admission decision, synchronous.

        Returns "dispatch" (slot already handed to rec), "queued" (waiter set),
        "full" (reject), or "deadline" (budget already exhausted).
        """
        src = self.sources[rec.source]
        if rec.remaining() <= 0:
            return "deadline"
        if rec.client_gone or self.shutting_down:
            return "full"

        can_run_now = (
            src.inflight < src.max_inflight
            and self.global_inflight < self.global_max
            and not src.queue          # never let a newcomer jump the FIFO
        )
        if can_run_now:
            self._assign(src, rec)
            return "dispatch"

        if len(src.queue) < src.queue_limit:
            rec.state = "queued"
            rec.waiter = asyncio.get_running_loop().create_future()
            src.queue.append(rec)
            self.event("queued", rid=rec.rid, source=rec.source,
                       queue_len=len(src.queue))
            self.save_stats()
            return "queued"

        return "full"

    def _assign(self, src: SourceState, rec: Request):
        # Synchronous critical section: no await, so deadline/cancel races
        # cannot observe a half-updated slot.
        rec.slot = True
        rec.state = "inflight"
        src.inflight += 1
        self.global_inflight += 1
        src.max_inflight_observed = max(src.max_inflight_observed, src.inflight)
        src.metrics["slot_assignments"] += 1
        self.event("slot_assigned", rid=rec.rid, source=src.name,
                   source_inflight=src.inflight,
                   global_inflight=self.global_inflight,
                   remaining_ms=round(rec.remaining() * 1000, 1))
        if rec.waiter is not None and not rec.waiter.done():
            rec.waiter.set_result("dispatch")
        self.save_stats()

    def release_slot(self, src: SourceState, rec: Request):
        # Exactly-once release: the slot flag is flipped before counters and
        # before pumping, and there is no await in this section.
        if not rec.slot:
            return
        rec.slot = False
        src.inflight -= 1
        self.global_inflight -= 1
        self.event("slot_released", rid=rec.rid, source=src.name,
                   source_inflight=src.inflight,
                   global_inflight=self.global_inflight)
        self.pump()

    def pump(self):
        """Round-robin dispatch of queued work.

        Never dispatches while shutting down.

        One dispatch per source per pass, so a large backlog on one source
        cannot starve another source that also has work.  Per-source caps are
        checked independently; with the shipped config each source has a hard
        reservation of one global slot (1 + 1 == global_max_inflight).
        """
        if self.shutting_down:
            return
        n = len(self.source_order)
        while self.global_inflight < self.global_max:
            progressed = False
            for offset in range(n):
                idx = (self.rr_next + offset) % n
                src = self.sources[self.source_order[idx]]
                if src.inflight >= src.max_inflight:
                    continue
                # Drop dead/expired heads before choosing the FIFO head.
                while src.queue and (src.queue[0].finished or getattr(src.queue[0], "dead", False)):
                    src.queue.popleft()
                if not src.queue:
                    continue
                rec = src.queue[0]
                if rec.client_gone or rec.finished:
                    src.queue.popleft()
                    self.finalize(rec, "client_cancelled")
                    progressed = True
                    break
                if rec.remaining() <= 0:
                    # Safety net: this request must never be sent upstream.
                    src.queue.popleft()
                    rec.state = "expired"
                    rec.dead = True
                    self.event("queue_expired", rid=rec.rid, source=src.name,
                               remaining_ms=round(rec.remaining() * 1000, 1))
                    if rec.waiter is not None and not rec.waiter.done():
                        rec.waiter.set_result("deadline")
                    progressed = True
                    break
                src.queue.popleft()
                self.rr_next = (idx + 1) % n
                self._assign(src, rec)
                progressed = True
                break                       # give the next source a turn
            if not progressed:
                break
        self.save_stats()

    # ------------------------------------------------------------- lifecycle
    def finalize(self, rec: Request, reason: str):
        """Record one terminal state and remove local bookkeeping.

        Idempotent; terminal reason is set by the first caller only.
        """
        if rec.finished:
            return
        rec.finished = True
        rec.dead = True
        rec.terminal = reason
        if rec.waiter is not None and not rec.waiter.done():
            # Wake a queued request with its terminal decision instead of
            # letting it wait for the budget timeout.
            rec.waiter.set_result(reason)
        src = self.sources[rec.source]
        if rec.state == "queued":
            try:
                src.queue.remove(rec)
            except ValueError:
                pass
        src.metrics["ended"][reason] += 1
        self.global_metrics["ended"][reason] += 1
        self.event("finished", rid=rec.rid, source=rec.source, reason=reason,
                   elapsed_ms=round((time.monotonic() - rec.connected_at) * 1000, 1))
        # Release the slot only after the state flip, exactly once.
        self.release_slot(src, rec)
        self.requests.pop(rec.rid, None)
        self.save_stats()

    # --------------------------------------------------------------- upstream
    async def forward(self, rec: Request):
        """Connect, send the request, and read the complete small response."""
        src = self.sources[rec.source]
        writer_up = None
        try:
            reader_up, writer_up = await asyncio.open_connection(
                src.host, src.port, limit=MAX_HEAD_BYTES)
            self.upstream_writers.add(writer_up)
            rec.upstream_writer = writer_up
            payload = (
                f"GET {self.allowed_path} HTTP/1.1\r\n"
                f"Host: {src.host}:{src.port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("latin1")
            writer_up.write(payload)
            await writer_up.drain()
            # "Truly sent" is counted only after the bytes reached the socket.
            src.metrics["dispatched"] += 1
            self.global_metrics["dispatched"] += 1
            self.event("upstream_write", rid=rec.rid, source=rec.source)

            status_line = await reader_up.readline()
            if not status_line or len(status_line) > MAX_LINE_BYTES:
                return "upstream_error", 502, self._reason_body(rec, "upstream_error")
            parts = status_line.decode("latin1").rstrip("\r\n").split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                return "upstream_error", 502, self._reason_body(rec, "upstream_error")
            status = int(parts[1])

            headers = {}
            total = len(status_line)
            while True:
                line = await reader_up.readline()
                if not line:
                    return "upstream_error", 502, self._reason_body(rec, "upstream_error")
                total += len(line)
                if total > MAX_HEAD_BYTES:
                    return "upstream_error", 502, self._reason_body(rec, "upstream_error")
                if line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers[k.strip().lower()] = v.strip()

            clen = headers.get(b"content-length")
            if clen is not None:
                try:
                    n = int(clen)
                except ValueError:
                    return "upstream_error", 502, self._reason_body(rec, "upstream_error")
                if n < 0 or n > MAX_BODY_BYTES:
                    return "upstream_error", 502, self._reason_body(rec, "upstream_error")
                body = await reader_up.readexactly(n)
            else:
                body = await reader_up.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    return "upstream_error", 502, self._reason_body(rec, "upstream_error")

            if 200 <= status < 300:
                ctype = headers.get(b"content-type", b"application/json").decode("latin1")
                return "success", status, body, ctype
            return "upstream_error", 502, self._reason_body(rec, "upstream_error")
        except (OSError, ValueError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as exc:
            self.event("upstream_error", rid=rec.rid, source=rec.source,
                       detail=type(exc).__name__)
            return "upstream_error", 502, self._reason_body(rec, "upstream_error")
        finally:
            if writer_up is not None:
                self.upstream_writers.discard(writer_up)
                rec.upstream_writer = None
                writer_up.close()       # socket close is synchronous scheduling

    @staticmethod
    def _reason_body(rec: Request, reason: str) -> bytes:
        return json.dumps({"reason": reason, "source": rec.source,
                           "request_id": rec.rid}).encode()

    # --------------------------------------------------------------- request
    async def run_request(self, rec: Request):
        status = 504
        reason = "deadline"
        body = b""
        ctype = "application/json"
        try:
            # ONE deadline for the whole logical request: queue wait, connect,
            # request write and full response read all share it.  There is no
            # second budget after dequeueing.
            async with asyncio.timeout(max(rec.remaining(), 1e-6)):
                decision = self.submit(rec)
                if decision == "full":
                    reason, status = "queue_full", 503
                    body = self._reason_body(rec, "queue_full")
                    return self._finish(rec, reason, status, body, ctype)
                if decision == "deadline":
                    reason, status = "deadline", 504
                    body = self._reason_body(rec, "deadline")
                    return self._finish(rec, reason, status, body, ctype)
                if decision == "queued":
                    outcome = await rec.waiter
                    if outcome != "dispatch":
                        # Woken by finalize()/watch_client with a reason, or the
                        # deadline fired while queued.  In every case this
                        # request was removed from the queue and must not be
                        # sent upstream now.
                        reason = outcome if outcome in TERMINAL_REASONS else "deadline"
                        status = 503 if reason == "shutdown" else 504
                        body = self._reason_body(rec, reason)
                        return self._finish(rec, reason, status, body, ctype)
                reason, status, body, *rest = await self.forward(rec)
                if rest:
                    ctype = rest[0]
        except TimeoutError:
            # Budget exhausted while queued, connecting, writing or reading.
            reason = rec.cancel_reason or "deadline"
            status = 504
            body = self._reason_body(rec, "deadline")
        except asyncio.CancelledError:
            # Client disconnect or process shutdown: abort the in-flight read
            # (forward()'s finally closes the upstream socket) and free the slot.
            if self.shutting_down:
                reason, status = "shutdown", 503
                body = self._reason_body(rec, "shutdown")
            else:
                reason = rec.cancel_reason or "client_cancelled"
                status = None
                body = b""
        except Exception as exc:  # defensive: never leave a request without a terminal state
            self.event("internal_error", rid=rec.rid, source=rec.source,
                       detail=type(exc).__name__)
            reason, status = "upstream_error", 502
            body = self._reason_body(rec, "upstream_error")
        return self._finish(rec, reason, status, body, ctype)

    def _finish(self, rec, reason, status, body, ctype):
        if rec.finished:
            return None
        self.finalize(rec, reason)
        if status is None:      # cancelled/shutdown: nothing sensible to send
            return None
        return status, reason, body, ctype

    async def watch_client(self, rec: Request):
        """Detect a client disconnect while the request is queued/in flight."""
        try:
            while True:
                chunk = await rec.reader.read(1024)
                if chunk == b"":
                    rec.client_gone = True
                    rec.cancel_reason = "client_cancelled"
                    self.event("client_disconnect", rid=rec.rid, source=rec.source)
                    if rec.run_task is not None and not rec.run_task.done():
                        rec.run_task.cancel()
                    return
                # A GET client has no body; ignore any pipelined bytes and
                # keep watching for EOF.
        except (ConnectionError, OSError):
            rec.client_gone = True
            rec.cancel_reason = "client_cancelled"
            if rec.run_task is not None and not rec.run_task.done():
                rec.run_task.cancel()
        except asyncio.CancelledError:
            return

    # --------------------------------------------------------------- parsing
    async def read_head(self, reader):
        line = await reader.readline()
        if not line:
            raise BadRequest("empty request")
        if len(line) > MAX_LINE_BYTES:
            raise BadRequest("request line too long")
        try:
            method, target, version = line.decode("latin1").rstrip("\r\n").split(" ")
        except ValueError:
            raise BadRequest("malformed request line")
        if version not in ("HTTP/1.1", "HTTP/1.0"):
            raise BadRequest("unsupported HTTP version")
        headers = {}
        total = len(line)
        while True:
            hl = await reader.readline()
            if not hl:
                raise BadRequest("truncated headers")
            total += len(hl)
            if total > MAX_HEAD_BYTES:
                raise BadRequest("headers too large")
            if hl in (b"\r\n", b"\n"):
                break
            if b":" not in hl:
                raise BadRequest("malformed header")
            k, v = hl.split(b":", 1)
            headers[k.strip().lower().decode("latin1")] = v.strip().decode("latin1")
        return method, target, headers

    def resolve_source(self, target: str, headers: dict) -> str:
        if not target.startswith("/"):
            raise BadRequest("only origin-form request targets are accepted (no absolute URLs)")
        split = urlsplit(target)
        if split.scheme or split.netloc:
            raise BadRequest("absolute URLs are not allowed")
        if split.path != self.allowed_path:
            raise BadRequest(f"only {self.allowed_path} is allowed")
        q = parse_qs(split.query)
        qsrc = q.get("source", [None])[-1] if q.get("source") else None
        hsrc = headers.get("x-source")
        if qsrc is not None and hsrc is not None and qsrc != hsrc:
            raise BadRequest("conflicting source values")
        name = qsrc if qsrc is not None else hsrc
        if name is None:
            raise BadRequest("source is required (query source= or X-Source header)")
        if name not in self.sources:
            raise BadRequest(f"unknown source {name!r}")
        return name

    # ------------------------------------------------------------ connection
    async def handle_connection(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.client_writers.add(writer)
        try:
            try:
                connected_at = time.monotonic()
                # Header read also runs under the same budget as the request.
                async with asyncio.timeout(self.budget_s):
                    method, target, headers = await self.read_head(reader)
            except TimeoutError:
                self.global_metrics["bad_requests"] += 1
                self.event("bad_request", detail="header_timeout")
                await self.send_response(writer, 408, "bad_request",
                                         json.dumps({"reason": "bad_request"}).encode())
                return
            except (BadRequest, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError, ConnectionError, OSError, ValueError) as exc:
                self.global_metrics["bad_requests"] += 1
                self.event("bad_request", detail=str(exc) or type(exc).__name__)
                await self.send_response(writer, 400, "bad_request",
                                         json.dumps({"reason": "bad_request"}).encode())
                return
            if method != "GET":
                self.global_metrics["bad_requests"] += 1
                self.event("bad_request", detail="method")
                await self.send_response(writer, 405, "bad_request",
                                         json.dumps({"reason": "method_not_allowed"}).encode())
                return
            try:
                source = self.resolve_source(target, headers)
            except BadRequest as exc:
                self.global_metrics["bad_requests"] += 1
                self.event("bad_request", detail=str(exc))
                await self.send_response(writer, 400, "bad_request",
                                         json.dumps({"reason": "bad_request",
                                                     "detail": str(exc)}).encode())
                return

            rec = Request(str(uuid.uuid4()), source, reader, writer,
                          connected_at, self.budget_s)
            rec.task = task
            self.requests[rec.rid] = rec
            src = self.sources[source]
            src.metrics["received"] += 1
            self.global_metrics["received"] += 1
            self.event("received", rid=rec.rid, source=source,
                       remaining_ms=round(rec.remaining() * 1000, 1))
            self.save_stats()

            watcher = asyncio.create_task(self.watch_client(rec))
            # The request body runs in its own tracked task so shutdown can
            # cancel the work (freeing the slot and the upstream socket) while
            # this handler stays alive long enough to report a terminal reply.
            run = asyncio.create_task(self.run_request(rec))
            self.runs.add(run)
            self.handler_recs[task] = rec
            rec.run_task = run
            try:
                result = await run
            except asyncio.CancelledError:
                # We are being cancelled (shutdown or a client watcher that
                # cancelled this handler).  Cancel the work too and unwind;
                # finalize() below is idempotent if the run already finished.
                if not run.done():
                    run.cancel()
                result = None
            finally:
                self.runs.discard(run)
            if result is None and rec.finished and rec.terminal in ("shutdown", "queue_full"):
                # Make the terminal state visible to the client instead of a
                # bare disconnect.
                await self.send_response(
                    writer, 503, rec.terminal,
                    self._reason_body(rec, rec.terminal), rid=rec.rid)
            elif result is not None:
                status, reason, body, ctype = result
                ok = await self.send_response(writer, status, reason, body, ctype,
                                              rid=rec.rid)
                if not ok and reason == "success":
                    self.global_metrics["delivery_failed"] += 1
                    self.event("delivery_failed", rid=rec.rid, source=source)
                    self.save_stats()
        finally:
            self.handler_recs.pop(task, None)
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):
                    pass
            # A handler cancelled before creating its record still owns these.
            self.client_writers.discard(writer)
            self.tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def send_response(self, writer, status: int, reason: str, body: bytes,
                            content_type: str = "application/json",
                            rid: str | None = None) -> bool:
        text = STATUS_TEXT.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Proxy-Outcome: {reason}\r\n"
            + (f"X-Request-Id: {rid}\r\n" if rid else "")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin1")
        try:
            writer.write(head + body)
            await asyncio.wait_for(writer.drain(), CLIENT_WRITE_GRACE_S)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return False

    # ---------------------------------------------------------------- serve
    def _on_signal(self, _sig):
        if not self.shutdown_event.is_set():
            self.event("shutdown_signal")
            self.shutdown_event.set()

    async def serve(self):
        self.server = await asyncio.start_server(
            self.handle_connection, self.host, self.port, limit=MAX_HEAD_BYTES)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except NotImplementedError:       # pragma: no cover
                pass
        bound = self.server.sockets[0].getsockname() if self.server.sockets else (self.host, self.port)
        print(f"proxy listening on http://{bound[0]}:{bound[1]} "
              f"(global_max_inflight={self.global_max}, budget={self.budget_s * 1000:.0f}ms)",
              flush=True)
        self.event("listening", host=bound[0], port=bound[1])
        self.save_stats()
        await self.shutdown_event.wait()
        await self.shutdown()

    async def shutdown(self):
        self.shutting_down = True
        self.event("shutdown_begin", active_tasks=len(self.tasks),
                   global_inflight=self.global_inflight)
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=0.25)
            except (asyncio.TimeoutError, Exception):
                pass

        # 1. Cancel the per-request work (this aborts the upstream read and
        #    frees the slot).  Queued entries are removed, never dispatched.
        for rec in list(self.requests.values()):
            if rec.finished:
                continue
            rec.cancel_reason = "shutdown"
            if rec.run_task is not None and not rec.run_task.done():
                rec.run_task.cancel()
        runs = [t for t in self.runs if not t.done()]
        if runs:
            await asyncio.wait(runs, timeout=self.shutdown_grace_s)
        # 2. Any request still open gets a synchronous terminal state.
        for rec in list(self.requests.values()):
            if not rec.finished:
                self.finalize(rec, "shutdown")
        # 3. Now cancel the connection handlers, giving them a moment to send
        #    the 503 terminal response for their already-finalized request.
        tasks = [t for t in self.tasks if not t.done() and t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.shutdown_grace_s)

        # 4. Force-close anything still holding a socket.
        for w in list(self.upstream_writers):
            try:
                w.close()
            except Exception:
                pass
        self.upstream_writers.clear()
        for w in list(self.client_writers):
            try:
                w.close()
            except Exception:
                pass
        self.client_writers.clear()
        await asyncio.sleep(0)
        self.save_stats()
        self.event("shutdown_end", global_inflight=self.global_inflight,
                   remaining_requests=len(self.requests))
        if self._event_fh is not None:
            self._event_fh.close()
            self._event_fh = None
        print(json.dumps({"shutdown": True, "stats": self.snapshot()},
                         separators=(",", ":")), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="GET-only isolated forwarding proxy")
    default_config = Path(__file__).with_name("config.json")
    if not default_config.exists():
        default_config = Path(__file__).with_name("fixtures") / "config.json"
    ap.add_argument("--config", default=str(default_config))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--stats-file", default=None)
    ap.add_argument("--event-log", default=None)
    ap.add_argument("--shutdown-grace-ms", type=int, default=300)
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    proxy = Proxy(
        config, args.host, args.port,
        Path(args.stats_file) if args.stats_file else None,
        Path(args.event_log) if args.event_log else None,
        args.shutdown_grace_ms / 1000.0,
    )
    try:
        asyncio.run(proxy.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
