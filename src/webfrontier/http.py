"""HTTP layer: bounded body reads, per-origin rate limiting, hard scope guard.

The client deliberately does not follow redirects: every hop is inspected by
the crawler so scope and robots rules can be re-checked before the next
request.  Response bodies are streamed and hard-limited to ``max_body_bytes``
actual bytes, regardless of the Content-Length header.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
import httpx

from .urls import USER_AGENT, URLIdentity, parse_identity


class RequestBlocked(RuntimeError):
    """A request was refused before it hit the network."""


@dataclass
class FetchResult:
    url: str
    status: int | None
    content_type: str
    location: str | None
    body: bytes
    truncated: bool
    elapsed_ms: float | None
    error: str | None = None
    error_class: str | None = None  # connect | timeout | protocol | stopped | other
    retry_after: float | None = None
    aborted: bool = False
    declared_length: int | None = None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        import datetime as _dt

        now = _dt.datetime.now(dt.tzinfo)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def classify_error(exc: Exception) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)):
        return "connect"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timeout"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect"
    return "other"


class OriginGate:
    """Enforces the configured minimum interval between request starts.

    One gate exists per origin, so a slow origin only ever holds its own
    worker slots; workers never wait on a lock shared with other origins.

    ``JITTER_MARGIN`` is added on top of the configured interval.  The gate
    measures starts locally in ``monotonic`` time; connection setup jitter can
    still make two requests arrive at the server slightly closer together, so
    a small margin makes the server-observed interval satisfy the configured
    minimum as well.
    """

    JITTER_MARGIN = 0.05

    def __init__(self, min_interval: float) -> None:
        self.configured_interval = max(0.0, float(min_interval))
        self.min_interval = self.configured_interval + (
            self.JITTER_MARGIN if self.configured_interval > 0 else 0.0
        )
        self._lock = threading.Lock()
        self._last_start = float("-inf")

    def wait_and_mark(self, stop: threading.Event) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._last_start + self.min_interval - now
                if wait <= 0:
                    self._last_start = now
                    return True
            if stop.wait(wait):
                return False


class HttpClient:
    def __init__(
        self,
        *,
        max_body_bytes: int,
        timeout: float,
        gates: dict[str, OriginGate],
        scope_origins: set[str],
        worker_name: str,
        request_log=None,
    ) -> None:
        self.max_body_bytes = int(max_body_bytes)
        self.timeout = float(timeout)
        self.gates = gates
        self.scope_origins = scope_origins
        self.worker_name = worker_name
        self.request_log = request_log
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 10.0)),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )

    def close(self) -> None:
        self._client.close()

    def _guard(self, url: str) -> URLIdentity:
        ident = parse_identity(url)
        if ident.origin not in self.scope_origins:
            raise RequestBlocked(f"origin {ident.origin} is outside the configured scope")
        return ident

    def request(
        self,
        *,
        url: str,
        origin: str,
        kind: str,
        redirect_from: str | None,
        stop: threading.Event,
        log: bool = True,
        max_body_bytes: int | None = None,
    ) -> FetchResult:
        gate = self.gates.get(origin)
        if gate is None:
            raise RequestBlocked(f"no rate gate configured for origin {origin}")
        # Scope is checked before any bytes are sent; callers also check it
        # between redirect hops, this is the last line of defence.
        self._guard(url)
        limit = self.max_body_bytes if max_body_bytes is None else int(max_body_bytes)
        if not gate.wait_and_mark(stop):
            return FetchResult(
                url=url, status=None, content_type="", location=None, body=b"",
                truncated=False, elapsed_ms=None,
                error="stopped before request", error_class="stopped",
            )
        started = time.time()
        t0 = time.monotonic()
        status: int | None = None
        outcome = "error"
        content_type = ""
        location: str | None = None
        bytes_read = 0
        body = bytearray()
        truncated = False
        error = None
        error_class = None
        retry_after: float | None = None
        aborted = False
        declared_length: int | None = None
        try:
            with self._client.stream("GET", url) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                location = response.headers.get("location")
                try:
                    declared_length = int(response.headers.get("content-length", ""))
                except ValueError:
                    declared_length = None
                if status in (429, 503):
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                outcome = "ok"
                for chunk in response.iter_bytes():
                    if stop.is_set():
                        outcome = "aborted"
                        aborted = True
                        break
                    body.extend(chunk)
                    bytes_read += len(chunk)
                    if bytes_read > limit:
                        truncated = True
                        outcome = "truncated"
                        break
                if truncated:
                    # Keep at most the declared limit; never buffer an oversize body.
                    del body[limit:]
                    bytes_read = limit
                elif declared_length is not None and declared_length > limit:
                    truncated = True
                    outcome = "truncated"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            error_class = classify_error(exc)
            outcome = "error"
        except Exception as exc:  # defensive: unexpected client failure
            error = f"{type(exc).__name__}: {exc}"
            error_class = "other"
            outcome = "error"
        elapsed = (time.monotonic() - t0) * 1000.0
        if log and self.request_log is not None:
            self.request_log(
                origin=origin, url=url, kind=kind, status=status,
                outcome=outcome, redirect_from=redirect_from, started=started,
                elapsed_ms=elapsed, bytes_read=bytes_read if status is not None else None,
                content_type=content_type or None, worker=self.worker_name,
            )
        return FetchResult(
            url=url,
            status=status,
            content_type=content_type,
            location=location,
            body=bytes(body),
            truncated=truncated,
            elapsed_ms=elapsed,
            error=error,
            error_class=error_class,
            retry_after=retry_after,
            aborted=aborted,
            declared_length=declared_length,
        )
