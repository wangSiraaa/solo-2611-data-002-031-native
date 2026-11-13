"""Crawl engine: per-origin worker pools, robots gating, redirects, retries.

Design notes
------------
* One worker pool per configured origin, sized by that origin's concurrency.
  A slow origin can therefore never occupy another origin's slots; the only
  shared locks are the short SQLite write transactions and per-origin gates.
* Every redirect hop is checked against the configured origin/path scope and
  the *target* origin's robots rules *before* the request is sent.
* The page budget is consumed per page request, including redirect hops.
* Retryable failures (connect errors, timeouts, 429, 500/502/503/504) are put
  back into ``retry_wait`` with a bounded backoff; the row is not ``done``.
* A paused or killed run leaves ``pending`` rows only.  Rows still marked
  ``in_flight`` at start-up are requeued explicitly.
"""
from __future__ import annotations

import json
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import TaskConfig
from .http import FetchResult, HttpClient
from .robots import RobotsRules, absent_rules, parse_robots
from .store import Store
from .urls import (
    URLIdentity,
    URLIdentityError,
    media_type,
    parse_html,
    parse_identity,
    resolve_link,
    HTML_MEDIA_TYPES,
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


def _rss_kb() -> int:
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _peak_rss_kb() -> int:
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return _rss_kb()


@dataclass
class RunResult:
    complete: bool
    stopped_reason: str
    paused: bool
    interrupted: bool
    unprocessed: int
    budget_stopped: bool
    details: dict[str, Any]


class RobotsManager:
    """Fetch robots.txt once per origin and freeze the rules for the task.

    A worker that needs an unknown origin fetches it; other workers wait on
    that origin's event.  Failures are retried with the same backoff policy as
    pages, then the origin is blocked (never interpreted as "allow").
    """

    def __init__(self, config: TaskConfig, store: Store, stop: threading.Event) -> None:
        self.config = config
        self.store = store
        self.stop = stop
        self.limits = config.limits
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self._events: dict[str, threading.Event] = {}
        for origin in config.origin_values:
            self._states[origin] = {"state": "unknown", "event": threading.Event()}
            # Rules fetched by an earlier run of the same frontier are reused
            # unchanged: RFC 9309 caching, and "frozen for the task".
            row = store.robots_row(origin)
            if row is not None and row["state"] == "ready":
                from .robots import rules_from_json

                rules = rules_from_json(
                    row["rules_json"], row["matched_group"] or "", row["status"]
                )
                self._states[origin] = {
                    "state": "ready",
                    "rules": rules,
                    "event": threading.Event(),
                    "cached": True,
                }
                self._states[origin]["event"].set()
            elif row is not None and row["state"] == "blocked":
                # A site skipped because robots.txt was unreachable stays
                # skipped for this task; it is never re-interpreted as allow.
                reason = (
                    f"robots.txt unavailable after {row['attempts']} attempts in an "
                    "earlier run of this task; site skipped"
                )
                self._states[origin] = {
                    "state": "blocked",
                    "reason": reason,
                    "event": threading.Event(),
                }
                self._states[origin]["event"].set()
        self._gates: dict[str, Any] = {}

    def set_gates(self, gates: dict[str, Any]) -> None:
        self._gates = gates

    def result(self, origin: str) -> tuple[RobotsRules | None, str | None]:
        """Return (rules, blocked_reason); rules is None when the site is blocked."""
        with self._lock:
            entry = self._states[origin]
            event = entry["event"]
            if entry["state"] == "ready":
                return entry["rules"], None
            if entry["state"] == "blocked":
                return None, entry.get("reason")
            if entry["state"] == "fetching":
                owner = False
            else:
                entry["state"] = "fetching"
                owner = True
        if not owner:
            event.wait()
            with self._lock:
                entry = self._states[origin]
                if entry["state"] == "ready":
                    return entry["rules"], None
                return None, entry.get("reason")
        rules, reason = self._fetch(origin)
        with self._lock:
            entry = self._states[origin]
            if rules is not None:
                entry["state"] = "ready"
                entry["rules"] = rules
            else:
                entry["state"] = "blocked"
                entry["reason"] = reason
            entry["event"].set()
        return rules, reason

    def is_ready_or_blocked(self, origin: str) -> bool:
        with self._lock:
            return self._states[origin]["state"] in ("ready", "blocked")

    def _fetch(self, origin: str) -> tuple[RobotsRules | None, str | None]:
        url = origin + "/robots.txt"
        gate = self._gates.get(origin)
        client = HttpClient(
            max_body_bytes=self.limits.robots_max_bytes,
            timeout=self.limits.timeout_seconds,
            gates=self._gates,
            scope_origins=self.config.scope_origins,
            worker_name=f"robots:{origin}",
            request_log=self.store.log_request,
        )
        attempts = 0
        last_status: int | None = None
        last_error: str | None = None
        try:
            while attempts < self.limits.robots_max_attempts and not self.stop.is_set():
                attempts += 1
                last_error = None
                last_status = None
                # robots.txt may redirect; follow at most five hops, checking
                # scope and per-origin rules for every hop before requesting.
                current_url = url
                current_origin = origin
                hops = 0
                while True:
                    if current_origin not in self.config.scope_origins:
                        return None, f"robots redirect left the allowed scope: {current_url}"
                    if gate is None:
                        gate = self._gates.get(current_origin)
                    result = client.request(
                        url=current_url,
                        origin=current_origin,
                        kind="robots",
                        redirect_from=None,
                        stop=self.stop,
                        max_body_bytes=self.limits.robots_max_bytes,
                    )
                    last_status = result.status
                    if result.error:
                        last_error = result.error
                        break
                    if result.status in REDIRECT_STATUS and result.location:
                        hops += 1
                        if hops > 5:
                            return None, "robots.txt exceeded five redirects"
                        target = resolve_link(current_url, result.location)
                        if target is None:
                            return None, "robots.txt redirect target is not http(s)"
                        try:
                            ident = parse_identity(target)
                        except URLIdentityError:
                            return None, "robots.txt redirect target is invalid"
                        if ident.origin not in self.config.scope_origins:
                            return None, (
                                f"robots.txt redirect to {ident.origin} refused before request"
                            )
                        current_url = ident.request_url
                        current_origin = ident.origin
                        gate = self._gates.get(current_origin)
                        continue
                    break
                if last_error is None and last_status is not None:
                    if 200 <= last_status < 300:
                        body = result.body.decode("utf-8", "replace")
                        rules = parse_robots(body, self._token())
                        self.store.robots_write(
                            origin,
                            state="ready",
                            source="parsed",
                            status=last_status,
                            rules=[
                                {"allow": r.allow, "pattern": r.pattern}
                                for r in rules.rules
                            ],
                            body=body[: self.limits.robots_max_bytes],
                            matched_group=rules.matched_group,
                            attempts=attempts,
                        )
                        return rules, None
                    if 400 <= last_status < 500 and last_status != 429:
                        rules = absent_rules(last_status)
                        self.store.robots_write(
                            origin,
                            state="ready",
                            source="absent",
                            status=last_status,
                            rules=[],
                            body="",
                            attempts=attempts,
                        )
                        return rules, None
                    if last_status == 429 or last_status >= 500:
                        last_error = f"HTTP {last_status}"
                    else:
                        # Other statuses carry no usable robots semantics; RFC
                        # treats 4xx as unavailable, anything else is unfetchable.
                        last_error = f"HTTP {last_status}"
                if attempts < self.limits.robots_max_attempts:
                    delay = self._backoff(attempts)
                    if self.stop.wait(delay):
                        break
        finally:
            client.close()
        reason = (
            f"robots.txt unavailable after {attempts} attempts "
            f"({last_error or 'connection failure'}); site skipped, never treated as allow"
        )
        self.store.robots_write(
            origin,
            state="blocked",
            source="unfetchable",
            status=last_status,
            rules=[],
            body="",
            attempts=attempts,
        )
        return None, reason

    def _backoff(self, attempt: int) -> float:
        backoffs = self.limits.retry_backoff_seconds
        idx = min(attempt - 1, len(backoffs) - 1)
        return float(backoffs[idx])

    def _token(self) -> str:
        first = self.config.user_agent.split()[0] if self.config.user_agent else "bot"
        return first.split("/", 1)[0]


class Crawler:
    def __init__(self, config: TaskConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self.limits = config.limits
        self.stop = threading.Event()
        self.budget_exhausted = threading.Event()
        self.paused = threading.Event()
        self.interrupted = threading.Event()
        self.gates = {
            o.origin: _gate(o.min_interval) for o in config.origins
        }
        self.robots = RobotsManager(config, store, self.stop)
        self.robots.set_gates(self.gates)
        self._workers: list[threading.Thread] = []
        self._wake = threading.Condition()
        self._budget_lock = threading.Lock()
        self._budget_denied = False
        self._run_started = time.monotonic()
        self._pages_since_sample = 0
        self._sample_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------
    def seed(self, reset: bool = False) -> None:
        if reset:
            self.store.meta_set("started", "1")
        for url in self.config.all_seeds():
            self.store.add_seed(
                url, scope=self.config.scope, max_depth=self.limits.max_depth
            )
        self.store.meta_set("user_agent", self.config.user_agent)
        self.store.meta_set("config_path", str(self.config.path))
        self.store.meta_set("limits", json.dumps(self.config.raw.get("limits", {})))
        self.store.meta_set("origins", json.dumps(self.config.origin_values))

    def install_signals(self) -> None:
        def handle(signum, frame):  # noqa: ARG001
            self.interrupted.set()
            self.stop.set()

        try:
            signal.signal(signal.SIGINT, handle)
            signal.signal(signal.SIGTERM, handle)
        except ValueError:
            pass  # not in main thread

    def request_pause(self) -> None:
        self.store.meta_set("pause_requested", "1")

    def run(self) -> RunResult:
        self.install_signals()
        self.store.meta_set("running", "1")
        self.store.meta_set("pause_requested", "0")
        self.store.meta_set("finished", "0")
        # "budget_stopped" describes the *current* run; a resume that raises the
        # budget starts clean, while persisted page_requests keep counting.
        self.store.meta_set("budget_stopped", "0")
        interrupted_rows = self.store.requeue_interrupted()
        if interrupted_rows:
            self.store.meta_set("requeued_interrupted", interrupted_rows)
        self.store.sample_metrics(_rss_kb(), _peak_rss_kb())

        self._workers = []
        for origin_cfg in self.config.origins:
            for slot in range(origin_cfg.concurrency):
                name = f"{origin_cfg.origin}#{slot}"
                t = threading.Thread(
                    target=self._worker,
                    args=(origin_cfg.origin, name),
                    name=name,
                    daemon=True,
                )
                t.start()
                self._workers.append(t)

        reason, unprocessed, budget_stopped = self._monitor()
        self.stop.set()
        with self._wake:
            self._wake.notify_all()
        for t in self._workers:
            t.join(timeout=30.0)
        # Workers may have left a lease behind if interrupted mid-request.
        leftover = self.store.requeue_interrupted()

        if budget_stopped:
            unprocessed = self.store.mark_remaining(
                "over_budget",
                f"page budget {self.limits.page_budget} exhausted before this URL was requested",
            )
            reason = (
                f"page budget {self.limits.page_budget} exhausted; "
                f"{unprocessed} discovered URLs left unprocessed (marked over_budget)"
            )
        self.store.sample_metrics(_rss_kb(), _peak_rss_kb())
        self.store.meta_set("running", "0")
        self.store.meta_set("finished", "1")
        self.store.meta_set("last_stop_reason", reason)

        counts = self.store.state_counts()
        complete = (
            not budget_stopped
            and counts["pending"] == 0
            and counts["in_flight"] == 0
            and counts["retry_wait"] == 0
        )
        return RunResult(
            complete=complete,
            stopped_reason=reason,
            paused=self.paused.is_set(),
            interrupted=self.interrupted.is_set() or leftover > 0,
            unprocessed=unprocessed if budget_stopped else 0,
            budget_stopped=budget_stopped,
            details={
                "requeued_interrupted": interrupted_rows + leftover,
                "budget_stop_marked": budget_stopped,
                "state_counts": self.store.state_counts(),
                "dispositions": self.store.disposition_counts(),
                "details": self.store.count("details"),
                "requests": self.store.count("requests"),
                "page_requests": self.store.budget_used(),
            },
        )

    def _monitor(self) -> tuple[str, int, bool]:
        while True:
            if self.stop.is_set():
                if self.interrupted.is_set():
                    return "interrupted; frontier preserved", 0, False
                return "stopped", 0, False
            if self.store.meta_int("pause_requested") == 1:
                self.paused.set()
                self.stop.set()
                return "paused; frontier preserved", 0, False
            if self.budget_exhausted.is_set():
                self.store.meta_set("budget_stopped", "1")
                self.stop.set()
                # Count, but do not mutate yet: in-flight workers get to finish
                # or requeue their own rows first, then run() marks the rest.
                counts = self.store.state_counts()
                n = counts["pending"] + counts["retry_wait"]
                return (
                    f"page budget {self.limits.page_budget} exhausted; {n} queued URLs " +
                    "remain (final over_budget count is reported in run details)",
                    n,
                    True,
                )
            if (
                self.limits.max_runtime_seconds is not None
                and time.monotonic() - self._run_started > self.limits.max_runtime_seconds
            ):
                self.store.meta_set("budget_stopped", "1")
                self.stop.set()
                counts = self.store.state_counts()
                n = counts["pending"] + counts["retry_wait"]
                return (
                    f"max runtime {self.limits.max_runtime_seconds}s reached; {n} queued URLs " +
                    "remain (final over_budget count is reported in run details)",
                    n,
                    True,
                )
            counts = self.store.state_counts()
            active = counts["pending"] + counts["in_flight"] + counts["retry_wait"]
            if active == 0:
                self.store.sample_metrics(_rss_kb(), _peak_rss_kb())
                return "all discovered URLs reached a terminal state", 0, False
            with self._wake:
                self._wake.wait(timeout=0.1)
        # unreachable

    # -- workers ------------------------------------------------------
    def _worker(self, origin: str, name: str) -> None:
        client = HttpClient(
            max_body_bytes=self.limits.max_body_bytes,
            timeout=self.limits.timeout_seconds,
            gates=self.gates,
            scope_origins=self.config.scope_origins,
            worker_name=name,
            request_log=self.store.log_request,
        )
        try:
            while not self.stop.is_set():
                if self.robots.stop is not None and self.stop.is_set():
                    break
                rules, blocked_reason = self.robots.result(origin)
                if rules is None:
                    self.store.mark_origin_unavailable(
                        origin, blocked_reason or "robots unavailable"
                    )
                    self.store.robots_write(
                        origin,
                        state="blocked",
                        source="unfetchable",
                        status=None,
                        rules=[],
                        body="",
                        attempts=self.limits.robots_max_attempts,
                    )
                    self.store.meta_set(f"robots_blocked:{origin}", blocked_reason or "unavailable")
                    with self._wake:
                        self._wake.notify_all()
                    break
                row = self.store.claim_next(origin, name, time.time(), rules)
                if row is None:
                    if not self._origin_has_work(origin):
                        break
                    with self._wake:
                        self._wake.wait(timeout=0.05)
                    continue
                try:
                    self._process(row, rules, client)
                except Exception as exc:  # never lose a claimed row to a bug
                    self.store.finish(
                        row,
                        "parse_failed" if "parse" in str(exc).lower() else "http_error",
                        f"internal error while processing: {type(exc).__name__}: {exc}",
                        None,
                        None,
                        time.time(),
                    )
                self._sample_if_due()
                with self._wake:
                    self._wake.notify_all()
        finally:
            client.close()
            with self._wake:
                self._wake.notify_all()

    def _origin_has_work(self, origin: str) -> bool:
        row = self.store.conn.execute(
            "SELECT 1 FROM urls WHERE origin=? AND state IN ('pending','retry_wait') LIMIT 1",
            (origin,),
        ).fetchone()
        return row is not None

    def _sample_if_due(self) -> None:
        with self._sample_lock:
            self._pages_since_sample += 1
            if self._pages_since_sample >= self.limits.metrics_every:
                self._pages_since_sample = 0
                self.store.sample_metrics(_rss_kb(), _peak_rss_kb())

    def _consume_budget(self) -> bool:
        if self.budget_exhausted.is_set():
            return False
        with self._budget_lock:
            if self.store.consume_page_budget(self.limits.page_budget):
                return True
            self.budget_exhausted.set()
            return False

    # -- page processing ---------------------------------------------
    def _process(self, row: dict, rules: RobotsRules, client: HttpClient) -> None:
        chain: list[dict] = json.loads(row["redirect_chain"]) if row["redirect_chain"] else []
        visited = {row["canonical"]}
        current_row = row
        while True:
            if self.stop.is_set():
                self.store.release_pending(current_row, "interrupted; requeued from in_flight")
                return
            if not self._consume_budget():
                self.store.finish(
                    current_row,
                    "over_budget",
                    f"page budget {self.limits.page_budget} exhausted before request",
                    None,
                    None,
                    time.time(),
                )
                return
            result = client.request(
                url=current_row["request_url"],
                origin=current_row["origin"],
                kind="page",
                redirect_from=chain[-1]["from"] if chain else None,
                stop=self.stop,
            )
            if result.error_class == "stopped":
                self.store.release_pending(current_row, "stopped before request; requeued")
                return
            if result.aborted:
                self.store.release_pending(current_row, "interrupted mid-response; requeued")
                return

            if result.error:
                self._handle_transient(
                    current_row, result, f"{result.error_class}: {result.error}"
                )
                return

            status = result.status or 0
            if status in REDIRECT_STATUS:
                hop = self._redirect_href(result)
                if hop is None:
                    self.store.finish(
                        current_row, "http_error",
                        f"HTTP {status} without a Location header", status, None, time.time(),
                    )
                    return
                target_raw = resolve_link(current_row["request_url"], hop)
                if target_raw is None:
                    self.store.finish(
                        current_row, "out_of_scope",
                        f"redirect to non-http(s) target {hop!r}", status, None, time.time(),
                    )
                    return
                try:
                    target_ident = parse_identity(target_raw)
                except URLIdentityError as exc:
                    self.store.finish(
                        current_row, "out_of_scope",
                        f"redirect target cannot be normalised: {exc}", status, None, time.time(),
                    )
                    return
                # (1) scope check before any request to the target.
                if not self.config.scope.allows(target_ident):
                    self._record_refused_target(
                        current_row, target_ident, "out_of_scope",
                        f"redirect to {target_ident.canonical} refused before request "
                        f"(outside configured scope)",
                        status,
                    )
                    return
                # (2) loop / chain-length checks.
                if target_ident.canonical in visited:
                    self.store.finish(
                        current_row, "redirect_loop",
                        f"redirect loop: {target_ident.canonical} already visited in this chain",
                        status, None, time.time(),
                    )
                    return
                if len(chain) >= self.limits.max_redirects:
                    self.store.finish(
                        current_row, "too_many_redirects",
                        f"redirect chain longer than max_redirects={self.limits.max_redirects}",
                        status, None, time.time(),
                    )
                    return
                # (3) target site's robots rules before requesting it.
                target_rules, blocked = self.robots.result(target_ident.origin)
                if target_rules is None:
                    self._record_refused_target(
                        current_row, target_ident, "robots_unavailable",
                        f"redirect target {target_ident.canonical} skipped: "
                        f"{blocked or 'robots.txt unavailable'}",
                        status,
                    )
                    return
                if not target_rules.allowed(target_ident):
                    self._record_refused_target(
                        current_row, target_ident, "robots_denied",
                        f"redirect target {target_ident.canonical} disallowed by robots.txt",
                        status,
                    )
                    return
                chain = chain + [
                    {
                        "from": current_row["canonical"],
                        "to": target_ident.canonical,
                        "status": status,
                    }
                ]
                kind, next_row = self.store.claim_redirect_target(
                    current_row, target_ident, target_ident.request_url, chain,
                    time.time(), client.worker_name,
                )
                if kind == "exists":
                    return  # alias recorded; the tracked target owns extraction
                if kind == "finished":
                    # The target row was terminal (e.g. robots-denied).  Do not
                    # fetch it again; the alias chain stops here.
                    return
                visited.add(target_ident.canonical)
                current_row = next_row
                continue

            self._handle_response(current_row, result, chain)
            return

    def _redirect_href(self, result: FetchResult) -> str | None:
        location = result.location
        if not location:
            return None
        return location.strip() or None

    def _record_refused_target(
        self,
        row: dict,
        ident: URLIdentity,
        disposition: str,
        detail: str,
        status: int,
    ) -> None:
        """Record a refused redirect target without ever requesting it."""
        self.store.record_refused_redirect(
            source_row=row,
            target_canonical=ident.canonical,
            target_request_url=ident.request_url,
            disposition=disposition,
            detail=detail,
            http_status=status,
        )

    def _handle_transient(self, row: dict, result: FetchResult, detail: str) -> None:
        attempts = int(row["attempts"])
        if attempts >= self.limits.max_attempts:
            self.store.finish(
                row, "retries_exhausted",
                f"{detail} (after {attempts} attempts)", result.status, None, time.time(),
            )
            return
        delay = self._backoff(attempts, result.retry_after)
        self.store.schedule_retry(
            row,
            not_before=time.time() + delay,
            detail=f"{detail}; retry {attempts}/{self.limits.max_attempts} in {delay:.2f}s",
            http_status=result.status,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        backoffs = self.limits.retry_backoff_seconds
        base = float(backoffs[min(attempt - 1, len(backoffs) - 1)])
        if retry_after is not None:
            base = max(base, min(retry_after, 30.0))
        return base

    def _handle_response(
        self, row: dict, result: FetchResult, chain: list[dict]
    ) -> None:
        status = result.status or 0
        final_url = row["request_url"]
        if result.truncated:
            self.store.finish(
                row, "oversize",
                f"body exceeded max_body_bytes={self.limits.max_body_bytes}; not parsed",
                status, final_url, time.time(),
            )
            return
        if status in RETRYABLE_STATUS:
            self._handle_transient(row, result, f"HTTP {status}")
            return
        if not (200 <= status < 300):
            self.store.finish(
                row, "http_error", f"HTTP {status}", status, final_url, time.time(),
            )
            return
        ctype = media_type(result.content_type)
        if ctype not in HTML_MEDIA_TYPES:
            self.store.finish(
                row, "not_html",
                f"content-type {result.content_type or 'missing'} is not HTML; not expanded",
                status, final_url, time.time(),
            )
            return
        try:
            parsed = parse_html(final_url, result.body)
        except ValueError as exc:
            self.store.finish(
                row, "parse_failed", str(exc), status, final_url, time.time(),
            )
            return
        try:
            final_ident = parse_identity(final_url)
        except URLIdentityError:
            final_ident = parse_identity(row["canonical"])
        self.store.save_page(
            row,
            final_canonical=final_ident.canonical,
            final_request_url=final_url,
            http_status=status,
            title=parsed.title,
            item_id=parsed.item_id,
            links=parsed.links,
            attachments=parsed.attachments,
            max_depth=self.limits.max_depth,
            scope=self.config.scope,
            redirect_chain=chain,
            now=time.time(),
        )


def _gate(min_interval: float):
    from .http import OriginGate

    return OriginGate(min_interval)
