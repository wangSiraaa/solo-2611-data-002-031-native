"""HTTP session with explicit cookie, redirect and streaming handling.

Why not `requests`?  We need to (a) treat a 200 login page as a failed
session, (b) follow redirects while forwarding the right cookies, and
(c) stream a download to a temporary file so a truncated response is caught
before it can become a "successful" report.  Doing this with stdlib keeps
every rule visible.
"""

from __future__ import annotations

import http.client
import ssl
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path


class NetworkError(RuntimeError):
    pass


@dataclass
class Cookie:
    name: str
    value: str
    domain: str
    path: str
    secure: bool = False

    def matches(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if not _domain_match(host, self.domain):
            return False
        if not _path_match(parts.path or "/", self.path):
            return False
        if self.secure and parts.scheme != "https":
            return False
        return True

    def header(self) -> str:
        return f"{self.name}={self.value}"


def _domain_match(host: str, domain: str) -> bool:
    if domain.startswith("."):
        return host == domain[1:] or host.endswith(domain)
    return host == domain


def _path_match(request_path: str, cookie_path: str) -> bool:
    if not cookie_path:
        return True
    if request_path == cookie_path:
        return True
    return request_path.startswith(cookie_path.rstrip("/") + "/")


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, list[str]]
    body: bytes = b""
    body_path: Path | None = None
    declared_length: int | None = None
    received_length: int = 0
    truncated: bool = False
    history: list[tuple[int, str, str]] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v[0]
        return None

    @property
    def content_type(self) -> str:
        return (self.header("Content-Type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        if self.body:
            return self.body.decode("utf-8", errors="replace")
        if self.body_path is not None and self.body_path.exists():
            return self.body_path.read_text(encoding="utf-8", errors="replace")
        return ""

    def read_bytes(self) -> bytes:
        if self.body:
            return self.body
        if self.body_path is not None and self.body_path.exists():
            return self.body_path.read_bytes()
        return b""


class CookieJar:
    """Small RFC 6265-ish jar: scoping by domain and path, deletion on Max-Age<=0."""

    def __init__(self):
        self._cookies: list[Cookie] = []

    def add_from_headers(self, headers: list[tuple[str, str]], url: str):
        host = urllib.parse.urlsplit(url).hostname or ""
        for name, value in headers:
            if name.lower() == "set-cookie":
                self._store(value, host)

    def _store(self, set_cookie: str, default_domain: str):
        parts = [p.strip() for p in set_cookie.split(";")]
        if not parts or "=" not in parts[0]:
            return
        cname, cval = parts[0].split("=", 1)
        cname, cval = cname.strip(), cval.strip()
        cookie = Cookie(name=cname, value=cval, domain=default_domain, path="/")
        delete = False
        for attr in parts[1:]:
            if "=" in attr:
                k, v = (x.strip() for x in attr.split("=", 1))
            else:
                k, v = attr.strip(), ""
            k = k.lower()
            if k == "path" and v:
                cookie.path = v
            elif k == "domain" and v:
                cookie.domain = v
            elif k == "secure":
                cookie.secure = True
            elif k == "max-age":
                try:
                    if int(v) <= 0:
                        delete = True
                except ValueError:
                    pass
        self._cookies = [c for c in self._cookies
                         if not (c.name == cname and c.domain == cookie.domain
                                 and c.path == cookie.path)]
        if not delete:
            self._cookies.append(cookie)

    def header_for(self, url: str) -> str:
        found = sorted((c for c in self._cookies if c.matches(url)),
                       key=lambda c: len(c.path), reverse=True)
        return "; ".join(c.header() for c in found)

    def names_for(self, url: str) -> list[str]:
        return [c.name for c in self._cookies if c.matches(url)]

    def clear(self):
        self._cookies.clear()

    def __len__(self):
        return len(self._cookies)


class Session:
    def __init__(self, timeout: float = 20.0, max_redirects: int = 6,
                 user_agent: str = "report-helper/1.0"):
        self.jar = CookieJar()
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self._conn: http.client.HTTPConnection | None = None
        self._conn_key: tuple | None = None
        self.last_event: dict | None = None
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._conn_key = None

    def reset(self):
        self.close()
        self.jar.clear()
        self.last_event = None

    def _connection(self, url: str):
        parts = urllib.parse.urlsplit(url)
        key = (parts.scheme, parts.hostname, parts.port)
        if self._conn is None or self._conn_key != key:
            self.close()
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                self._conn = http.client.HTTPSConnection(
                    parts.hostname, parts.port, timeout=self.timeout, context=ctx)
            else:
                self._conn = http.client.HTTPConnection(
                    parts.hostname, parts.port, timeout=self.timeout)
            self._conn_key = key
        return self._conn

    # ------------------------------------------------------------ request
    def request(self, method: str, url: str, body: bytes | None = None,
                headers: dict | None = None, follow_redirects: bool = True,
                stream_to: Path | None = None) -> Response:
        history: list[tuple[int, str, str]] = []
        current_url, current_method = url, method
        current_body = body
        for hop in range(self.max_redirects + 1):
            resp = self._once(current_method, current_url,
                              current_body if current_method in ("POST", "PUT") else None,
                              headers if hop == 0 else None,
                              stream_to if hop == 0 else None)
            if resp.status in (301, 302, 303, 307, 308) and \
                    resp.header("Location") and follow_redirects:
                location = urllib.parse.urljoin(current_url,
                                                resp.header("Location"))
                history.append((resp.status, current_url, location))
                with self._lock:
                    self.events[-1]["redirect_to"] = location
                if resp.status in (301, 302, 303) and current_method == "POST":
                    current_method, current_body = "GET", None
                current_url = location
                continue
            resp.history = history
            return resp
        raise NetworkError(f"too many redirects starting at {url}")

    def _once(self, method: str, url: str, body: bytes | None,
              extra_headers: dict | None, stream_to: Path | None) -> Response:
        parts = urllib.parse.urlsplit(url)
        target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        conn = self._connection(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/csv;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "identity",
        }
        cookie_header = self.jar.header_for(url)
        if cookie_header:
            headers["Cookie"] = cookie_header
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        if extra_headers:
            headers.update(extra_headers)
        self.last_event = {
            "method": method, "url": url,
            "request_body": _redact_body(body),
            "cookie_present": bool(cookie_header),
            "redirect_from": None,
        }
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            self.last_event.update({"status": None, "error": str(exc)})
            raise NetworkError(f"{method} {url}: {exc}") from exc

        resp_headers = resp.getheaders()
        try:
            declared = int(resp.getheader("Content-Length") or -1)
        except ValueError:
            declared = None
        if declared is not None and declared < 0:
            declared = None
        received = 0
        truncated = False
        payload = b""
        sink = None
        try:
            if stream_to is not None:
                stream_to.parent.mkdir(parents=True, exist_ok=True)
                sink = stream_to.open("wb")
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if sink is not None:
                    sink.write(chunk)
                else:
                    payload += chunk
                self.chunk_events = getattr(self, "chunk_events", 0) + 1
        except (http.client.IncompleteRead, OSError,
                http.client.HTTPException) as exc:
            truncated = True
            if isinstance(exc, http.client.IncompleteRead) and exc.partial:
                received += len(exc.partial)
                if sink is not None:
                    sink.write(exc.partial)
                else:
                    payload += exc.partial
        finally:
            if sink is not None:
                sink.close()
        if declared is not None and received < declared:
            truncated = True
        self.jar.add_from_headers(resp_headers, url)
        result = Response(url=url, status=resp.status,
                          headers=_multi_dict(resp_headers),
                          body=payload, body_path=stream_to,
                          declared_length=declared, received_length=received,
                          truncated=truncated)
        self.last_event.update({
            "status": resp.status,
            "content_type": result.content_type,
            "bytes": received,
            "declared_bytes": declared,
            "truncated": truncated,
            "location": result.header("Location"),
        })
        self.events.append(dict(self.last_event))
        return result

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post_form(self, url: str, fields: list[tuple[str, str]], **kw) -> Response:
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        return self.request("POST", url, body=body, **kw)

    def stage_events(self, since: int) -> tuple[list[dict], int]:
        """Return HTTP events recorded since `since` (index) and the new index."""
        with self._lock:
            events = list(self.events[since:])
            return events, len(self.events)


def _multi_dict(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in headers:
        out.setdefault(k, []).append(v)
    return out


def _redact_body(body: bytes | None) -> str:
    if not body:
        return ""
    try:
        text = body.decode("utf-8")
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return "<unparseable>"
    safe = []
    for k, v in pairs:
        if k.lower() in ("password", "passwd", "pwd"):
            safe.append(f"{k}=***")
        else:
            safe.append(f"{k}={v}")
    return "&".join(safe)[:2000]
