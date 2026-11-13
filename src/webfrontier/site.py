"""Synthetic, fully local website group used to exercise the crawler.

Nothing here contacts a real site: every server binds 127.0.0.1 on a port from
the site configuration, and every generated link stays on those local origins
except for two deliberate traps:

* port 8899 is *outside* the crawl configuration and logs every request it
  receives, so a scope violation would be visible in its access log;
* site A's robots.txt disallows /private/ and site B redirects to it, so a
  robots violation would be visible in site A's access log.

The access log is appended as JSON lines by all servers, including robots.txt
requests, which lets the verification scripts check request ordering and the
minimum per-origin interval against what the server actually observed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

USER_AGENT = "CoopDirectoryBot/1.0 (+local synthetic directory crawler)"


@dataclass
class SiteSpec:
    name: str
    port: int
    role: str
    origin: str = ""

    def __post_init__(self) -> None:
        if not self.origin:
            self.origin = f"http://127.0.0.1:{self.port}"


@dataclass
class RoundConfig:
    """Synthetic behaviours toggled by configuration."""

    slow_seconds: float = 2.5
    slow_release_after: float = 1.2
    rate_limit_every: int = 3
    generation: int = 1
    extras: dict = field(default_factory=dict)


class AccessLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


def _page(title: str, body: str, *, item_id: str | None = None, head_extra: str = "") -> bytes:
    meta = f'<meta name="item_id" content="{html.escape(item_id)}">' if item_id else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>{meta}{head_extra}"
        "</head><body>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>"
    ).encode("utf-8")


def _links(items: list[tuple[str, str]], args: dict | None = None) -> str:
    out = []
    for href, text in items:
        if args:
            for role, origin in args.items():
                href = href.replace("{" + role + "}", origin)
        out.append(f'<p><a href="{html.escape(href)}">{html.escape(text)}</a></p>')
    return "".join(out)


def _item_id(path: str) -> str | None:
    """Synthetic details expose one stable item_id per logical record.

    The directory deliberately exposes the same record under several URLs
    (fragment aliases, repeated query parameters, redirect aliases), so the
    item_id must not depend on the URL spelling: A-1 and A-1?src=index share
    item_id "A-1" and must collapse into one detail record.
    """
    seg = path.rsplit("/", 1)[-1]
    if seg.startswith("A-"):
        return "A-" + seg.split("A-", 1)[1].split("?", 1)[0]
    if seg.startswith("BIG-"):
        return "BIG-" + seg.split("BIG-", 1)[1].split("?", 1)[0]
    if seg.startswith("B-"):
        return "B-" + seg.split("B-", 1)[1].split("?", 1)[0]
    if seg.startswith("C-"):
        return "C-" + seg.split("C-", 1)[1].split("?", 1)[0]
    if seg.startswith("D-"):
        return "D-" + seg.split("D-", 1)[1].split("?", 1)[0]
    return None


class World:
    def __init__(self, config: RoundConfig, log_path: Path) -> None:
        self.config = config
        self.log = AccessLog(log_path)
        self.slow_gate = threading.Event()
        self.released_at: float | None = None
        self.rate_counts: dict[str, int] = {}
        self.rate_lock = threading.Lock()
        self.oversize_body = _page("oversize", "<p>" + ("x" * 2_000_000) + "</p>")
        self.nav_count = 10_000
        self.detail_a = 8
        # Origins of the default group; overridable so tests can bind arbitrary
        # ports.  Placeholders are substituted in generated links/redirects.
        self.origins = dict(config.extras.get("origins", {
            "A": "http://127.0.0.1:8801",
            "B": "http://127.0.0.1:8802",
            "C": "http://127.0.0.1:8803",
            "D": "http://127.0.0.1:8804",
            "E": "http://127.0.0.1:8805",
            "TRAP": "http://127.0.0.1:8899",
        }))
        self.origin_args = {
            "A": self.origins["A"],
            "B": self.origins["B"],
            "C": self.origins["C"],
            "D": self.origins["D"],
            "E": self.origins["E"],
            "TRAP": self.origins["TRAP"],
        }
        self.detail_b = 4
        self.listing_pages = 4
        self.aliases = 60

    # -- helpers ------------------------------------------------------
    def _release_later(self, delay: float) -> None:
        def release() -> None:
            if not self.slow_gate.is_set():
                time.sleep(delay)
                self.released_at = time.time()
                self.slow_gate.set()

        threading.Thread(target=release, daemon=True).start()

    def _rate_ok(self, key: str) -> bool:
        with self.rate_lock:
            count = self.rate_counts.get(key, 0)
            self.rate_counts[key] = count + 1
        return count < self.config.rate_limit_every

    def _first_n(self, key: str, n: int) -> bool:
        """True for the first ``n`` requests for ``key``, deterministic."""
        with self.rate_lock:
            count = self.rate_counts.get(key, 0)
            self.rate_counts[key] = count + 1
        return count < n

    # -- site A: directory, details, redirects, forbidden paths ----------
    def robots_a(self) -> str:
        """Robots fixture that exercises grouping, longest match and anchors.

        * ``*`` group disallows /private/ and /public/ but MUST NOT be applied
          because a group for our product token exists.
        * ``OtherBot`` must never be selected.
        * ``CoopDirectoryBot``: /private/ denied, one exact path re-allowed by
          a longer Allow rule; /quarantine/ denied by prefix; ``/*/secret$``
          denied only for the exact end-anchored path.
        """
        return (
            "# synthetic site A\n"
            "User-agent: *\n"
            "Disallow: /private/\n"
            "Disallow: /public/\n"
            "Disallow: /\n"
            "\n"
            "User-agent: OtherBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: CoopDirectoryBot\n"
            "Disallow: /private/\n"
            "Allow: /private/public-note.txt$\n"
            "Disallow: /quarantine/\n"
            "Disallow: /*/secret$\n"
        )

    def site_a(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_a().encode(), headers
        if path in ("/", "/index.html"):
            items = [("/page/1", "listing page 1")]
            for pid in range(1, self.detail_a + 1):
                items.append((f"/detail/A-{pid}", f"detail A-{pid}"))
                # duplicate navigation with a fragment and a repeated query
                items.append((f"/detail/A-{pid}#top", f"again A-{pid} (fragment)"))
                items.append((f"/detail/A-{pid}?src=index&src=index", f"again A-{pid} (query)"))
            for n in range(1, self.aliases + 1):
                items.append((f"/go/{n}", f"alias {n} -> shared detail"))
            items += [
                ("/loop/a", "redirect loop entry"),
                ("/public/note.txt", "public note (allowed by product-token group)"),
                ("/private/public-note.txt", "exact allow overrides private prefix"),
                ("/private/hidden", "robots-disallowed link on A"),
                ("/quarantine/rack.html", "quarantine prefix (disallowed)"),
                ("/public/secret", "end-anchored wildcard, disallowed"),
                ("/public/secret/more", "end anchor means this longer path is allowed"),
                ("/go/secret", "end-anchored wildcard, disallowed"),
                ("/files/A-1.pdf", "attachment A-1"),
                ("/files/A-2.pdf", "attachment A-2"),
                ("/search?q=alpha&q=alpha&b=2", "search duplicate query"),
                ("/missing/nothing-here", "404 link"),
                ("{B}/", "site B directory"),
                ("{E}/", "site E loop"),
            ]
            body = _links(items, self.origin_args)
            return 200, headers["Content-Type"], _page("Site A directory", body), headers
        if path.startswith("/page/"):
            try:
                n = int(path.rsplit("/", 1)[1])
            except ValueError:
                return 404, headers["Content-Type"], _page("Not found", "<p>bad page</p>"), headers
            items = []
            if n < self.listing_pages:
                items.append((f"/page/{n + 1}", f"next page {n + 1}"))
            for pid in range(1, self.detail_a + 1):
                if (pid + n) % 3 == 0:
                    items.append((f"/detail/A-{pid}", f"detail A-{pid}"))
            body = _links(items, self.origin_args)
            return 200, headers["Content-Type"], _page(f"Listing {n}", body), headers
        if path.startswith("/detail/A-"):
            pid = _item_id(path) or ""
            body = (
                _links([
                    ("/page/1", "back to listing"),
                    (f"/detail/{pid}", "self link"),
                    ("/files/A-1.pdf", "attachment A-1"),
                    ("/files/A-2.pdf", "attachment A-2"),
                ])
                + "<p>Stable record for " + html.escape(pid) + "</p>"
            )
            # Replace the self link with a fragment alias, never a new URL.
            body = body.replace(f'href="/detail/{pid}"', f'href="/detail/{pid}#self"', 1)
            return 200, headers["Content-Type"], _page(f"Detail {pid}", body, item_id=pid), headers
        if path.startswith("/go/"):
            alias = path.split("/", 2)[2]
            # Deterministic (stable across processes): no hash randomisation.
            idx = (int(alias) * 37) % self.detail_a + 1
            target = f"/detail/A-{idx}"
            return 302, headers["Content-Type"], b"", {"Location": target}
        if path == "/loop/a":
            return 302, headers["Content-Type"], b"", {"Location": "/loop/b"}
        if path == "/loop/b":
            return 302, headers["Content-Type"], b"", {"Location": "/loop/a"}
        if path == "/public/note.txt":
            return 200, "text/plain; charset=utf-8", b"public note\n", headers
        if path == "/private/public-note.txt":
            return 200, "text/plain; charset=utf-8", b"exactly allowed note\n", headers
        if path.startswith("/private/"):
            # Must never be reached by the crawler; keeps a visible 200.
            return 200, headers["Content-Type"], _page("PRIVATE (violation)", "<p>private</p>"), headers
        if path.startswith("/quarantine/"):
            return 200, headers["Content-Type"], _page("QUARANTINE (violation)", "<p>q</p>"), headers
        if path.startswith("/files/"):
            return 200, "application/pdf", b"%PDF-1.4 synthetic attachment\n", headers
        if path == "/search":
            q = parse_qs(query, keep_blank_values=True)
            body = _links([
                (f"/detail/A-1?keep={quote(str(q))}", "search result"),
                ("/search?q=alpha&q=alpha&b=2", "same search again"),
            ])
            return 200, headers["Content-Type"], _page("Search", body), headers
        return 404, headers["Content-Type"], _page("Not found", "<p>no such path</p>"), headers

    # -- site B: cross-site links and redirects -------------------------
    def robots_b(self) -> str:
        return (
            "User-agent: *\n"
            "Disallow: /blocked/\n"
            "Allow: /\n"
        )

    def site_b(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_b().encode(), headers
        if path in ("/", "/index.html"):
            items = [
                ("{A}/", "site A"),
                ("{A}/detail/A-2?via=B", "site A detail via B"),
                ("/cross/good", "cross-site redirect to allowed detail"),
                ("/cross/bad", "cross-site redirect to robots-disallowed path"),
                ("/cross/outside", "cross-site redirect outside configured origins"),
                ("/detail/B-1", "site B detail"),
                ("/blocked/hidden", "robots-disallowed link on B"),
            ]
            return 200, headers["Content-Type"], _page("Site B directory", _links(items, self.origin_args)), headers
        if path == "/detail/B-1":
            return 200, headers["Content-Type"], _page(
                "Detail B-1",
                _links([("{A}/detail/A-1", "cross link to A-1")], self.origin_args),
                item_id="B-1",
            ), headers
        if path == "/cross/good":
            return 302, headers["Content-Type"], b"", {"Location": "{A}/detail/A-3"}
        if path == "/cross/bad":
            return 302, headers["Content-Type"], b"", {"Location": "{A}/private/hidden"}
        if path == "/cross/outside":
            return 302, headers["Content-Type"], b"", {"Location": "{TRAP}/trap/secret"}
        if path.startswith("/blocked/"):
            return 200, headers["Content-Type"], _page("BLOCKED (violation)", "<p>b</p>"), headers
        return 404, headers["Content-Type"], _page("Not found", "<p>b 404</p>"), headers

    # -- site C: slow responses and a controllable barrier ---------------
    def robots_c(self) -> str:
        return "User-agent: *\nAllow: /\n"

    def site_c(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_c().encode(), headers
        if path == "/":
            items = [(f"/slow/{i}", f"slow item {i}") for i in range(1, 7)]
            items.append(("{A}/detail/A-4", "A detail from slow site"))
            items.append(("{D}/detail/D-1", "D detail from slow site"))
            return 200, headers["Content-Type"], _page("Slow directory", _links(items, self.origin_args)), headers
        if path == "/_ctl/release":
            self.released_at = self.released_at or time.time()
            self.slow_gate.set()
            return 200, "text/plain", b"released\n", headers
        if path.startswith("/slow/"):
            if not self.slow_gate.is_set():
                self._release_later(self.config.slow_release_after)
                if not self.slow_gate.wait(self.config.slow_seconds):
                    time.sleep(self.config.slow_seconds)
            n = path.rsplit("/", 1)[1]
            return 200, headers["Content-Type"], _page(
                f"Slow item {n}", "<p>slow response</p>", item_id=f"C-{n}"
            ), headers
        return 404, headers["Content-Type"], _page("Not found", "<p>c 404</p>"), headers

    # -- site D: error pages, 429, binary and oversize -------------------
    def robots_d(self) -> str:
        return "User-agent: *\nDisallow: /private/\n"

    def site_d(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_d().encode(), headers
        if path == "/":
            items = [
                ("/err/500", "always 500"),
                ("/err/503-once", "503 then success"),
                ("/rate/limited", "429 then success"),
                ("/binary/a.bin", "binary attachment"),
                ("/text/plain.txt", "text/plain response"),
                ("/oversize", "oversize HTML"),
                ("/missing-page", "404 page"),
                ("/bad-encoding", "declared HTML with invalid UTF-8"),
                ("/bad-markup", "declared HTML that the parser rejects"),
                ("/chain/0", "redirect chain longer than max_redirects"),
                ("/detail/D-1", "site D detail"),
            ]
            return 200, headers["Content-Type"], _page("Site D", _links(items, self.origin_args)), headers
        if path == "/err/500":
            return 500, headers["Content-Type"], _page("Server error", "<p>500</p>"), headers
        if path == "/err/503-once":
            if self._first_n("503-once", 2):
                return 503, headers["Content-Type"], _page("Unavailable", "<p>503</p>"), {"Retry-After": "1"}
            return 200, headers["Content-Type"], _page("Recovered", "<p>ok</p>"), headers
        if path == "/rate/limited":
            if self._first_n("limited", 2):
                return 429, headers["Content-Type"], _page("Too many", "<p>429</p>"), {"Retry-After": "1"}
            return 200, headers["Content-Type"], _page(
                "Rate recovered", '<a href="/detail/D-1">detail D-1</a>'
            ), headers
        if path.startswith("/binary/"):
            return 200, "application/octet-stream", b"\x00\x01binary payload", headers
        if path == "/text/plain.txt":
            return 200, "text/plain; charset=utf-8", b"plain text, no links\n", headers
        if path == "/oversize":
            return 200, headers["Content-Type"], self.oversize_body, headers
        if path == "/bad-encoding":
            return 200, headers["Content-Type"], b"<html><title>bad \xff\xfe bytes</title></html>", headers
        if path == "/bad-markup":
            # Declared HTML but far too deeply nested for a safe DOM build;
            # selectolax resists most inputs, so this is a deliberate invalid
            # UTF-16 byte sequence labelled as HTML.
            return 200, headers["Content-Type"], b"\x00\x00<html>\x00", headers
        if path.startswith("/chain/"):
            try:
                n = int(path.rsplit("/", 1)[1])
            except ValueError:
                n = 0
            if n >= 8:
                return 200, headers["Content-Type"], _page("Chain end", "<p>end</p>"), headers
            return 302, headers["Content-Type"], b"", {"Location": f"/chain/{n + 1}"}
        if path == "/detail/D-1":
            return 200, headers["Content-Type"], _page(
                "Detail D-1",
                _links([("{C}/", "slow site")], self.origin_args),
                item_id="D-1",
            ), headers
        return 404, headers["Content-Type"], _page("Not found", "<p>d 404</p>"), headers

    # -- site E: redirect loops ------------------------------------------
    def robots_e(self) -> str:
        return "User-agent: *\n"

    def site_e(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_e().encode(), headers
        if path == "/":
            return 200, headers["Content-Type"], _page(
                "Loop site", _links([("/a", "two-hop loop"), ("/self", "self loop")])
            ), headers
        if path == "/a":
            return 302, headers["Content-Type"], b"", {"Location": "/b"}
        if path == "/b":
            return 302, headers["Content-Type"], b"", {"Location": "/a"}
        if path == "/self":
            return 302, headers["Content-Type"], b"", {"Location": "/self"}
        return 404, headers["Content-Type"], _page("Not found", "<p>e 404</p>"), headers

    # -- site F: 10k page navigation graph -------------------------------
    def robots_nav(self) -> str:
        return "User-agent: *\nAllow: /\n"

    def site_nav(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if path == "/robots.txt":
            return 200, "text/plain; charset=utf-8", self.robots_nav().encode(), headers
        if path in ("/", "/index.html"):
            return 200, headers["Content-Type"], _page(
                "Big navigation",
                _links([("/nav/1", "nav 1")] + [(f"/detail/BIG-{i}", f"detail BIG-{i}") for i in range(1, 11)]),
            ), headers
        if path.startswith("/nav/"):
            try:
                n = int(path.rsplit("/", 1)[1])
            except ValueError:
                return 404, headers["Content-Type"], _page("Not found", "<p>nav 404</p>"), headers
            if not (1 <= n <= self.nav_count):
                return 404, headers["Content-Type"], _page("Not found", "<p>nav 404</p>"), headers
            items: list[tuple[str, str]] = []
            step = 1
            while step <= 1024:
                nxt = n + step
                if nxt <= self.nav_count:
                    items.append((f"/nav/{nxt}", f"nav {nxt} (+{step})"))
                    # duplicate navigation edge, must collapse to one URL
                    items.append((f"/nav/{nxt}#dup", f"nav {nxt} duplicate"))
                step *= 2
            if n % 100 == 0:
                items.append((f"/detail/BIG-{n // 100}", f"detail BIG-{n // 100}"))
            if n == self.nav_count:
                items.append((f"/detail/BIG-{self.nav_count // 100 + 1}", "last detail"))
            return 200, headers["Content-Type"], _page(f"Nav {n}", _links(items, self.origin_args)), headers
        if path.startswith("/detail/BIG-"):
            pid = _item_id(path) or ""
            return 200, headers["Content-Type"], _page(
                f"Detail {pid}",
                _links([("/nav/1", "back")]),
                item_id=pid,
            ), headers
        return 404, headers["Content-Type"], _page("Not found", "<p>nav 404</p>"), headers

    # -- site F: robots.txt always fails (5xx) ---------------------------
    def site_robots_fail(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if path == "/robots.txt":
            return 500, headers["Content-Type"], b"robots backend broken\n", headers
        # Every other path is a trap: the crawler must never request it, because
        # robots.txt is unreachable and that means complete disallow.
        return 200, "text/html; charset=utf-8", _page(
            "ROBOTS-UNREACHABLE SITE (violation)", "<p>must never be crawled</p>"
        ), headers

    # -- trap server: outside configured origins ------------------------
    def site_trap(self, path: str, query: str) -> tuple[int, str, bytes, dict]:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        return 200, headers["Content-Type"], _page(
            "OUT OF SCOPE TRAP", "<p>this origin must never be crawled</p>"
        ), headers

    # -- dispatch -----------------------------------------------------
    def _fill(self, items):
        out = []
        for href, text in items:
            out.append((href.format(**self.origin_args), text))
        return out

    def dispatch(self, site: SiteSpec, path: str, query: str) -> tuple[int, str, bytes, dict]:
        handler = {
            "A": self.site_a,
            "B": self.site_b,
            "C": self.site_c,
            "D": self.site_d,
            "E": self.site_e,
            "NAV": self.site_nav,
            "ROBOTSFAIL": self.site_robots_fail,
            "TRAP": self.site_trap,
        }[site.role]
        status, ctype, body, extra = handler(path, query)
        # Substitute only the known "{ROLE}" placeholders; arbitrary braces in
        # page content (search queries) must stay untouched.
        for role, origin in self.origin_args.items():
            token = "{" + role + "}"
            if token.encode() in body:
                body = body.replace(token.encode(), origin.encode())
            if extra:
                extra = {
                    k: (v.replace(token, origin) if isinstance(v, str) else v)
                    for k, v in extra.items()
                }
        return status, ctype, body, extra


class Handler(BaseHTTPRequestHandler):
    server_version = "SyntheticDirectory/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        world: World = self.server.world  # type: ignore[attr-defined]
        spec: SiteSpec = self.server.spec  # type: ignore[attr-defined]
        started = time.time()
        parts = urlsplit(self.path)
        try:
            status, ctype, body, extra_headers = world.dispatch(spec, parts.path, parts.query)
        except Exception as exc:  # server bug must be visible, not hang
            status, ctype, body, extra_headers = (
                500, "text/plain", f"server error: {exc}".encode(), {},
            )
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra_headers.items():
                if key.lower() in ("content-type", "content-length"):
                    continue
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if not parts.path.startswith("/_ctl"):
            world.log.write({
                "ts": started,
                "site": spec.name,
                "role": spec.role,
                "port": spec.port,
                "origin": spec.origin,
                "path": parts.path,
                "query": parts.query,
                "status": status,
                "user_agent": self.headers.get("User-Agent", ""),
                "method": "GET",
                "bytes": len(body),
                "elapsed_ms": round((time.time() - started) * 1000.0, 3),
            })

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep stderr quiet; the JSON access log is the source of truth.
        return


class SiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, spec: SiteSpec, world: World) -> None:
        super().__init__(("127.0.0.1", spec.port), Handler)
        self.spec = spec
        self.world = world


def default_sites(nav_port: int = 8811) -> list[SiteSpec]:
    return [
        SiteSpec("site-a", 8801, "A"),
        SiteSpec("site-b", 8802, "B"),
        SiteSpec("site-c", 8803, "C"),
        SiteSpec("site-d", 8804, "D"),
        SiteSpec("site-e", 8805, "E"),
        SiteSpec("site-nav", nav_port, "NAV"),
        SiteSpec("site-robotsfail", 8806, "ROBOTSFAIL"),
        SiteSpec("trap", 8899, "TRAP"),
    ]


def start_group(
    *,
    log_path: Path,
    sites: list[SiteSpec] | None = None,
    config: RoundConfig | None = None,
    ready_file: Path | None = None,
) -> tuple[list[SiteServer], World]:
    site_list = sites or default_sites()
    # Derive origin placeholders from the actually bound ports so tests can use
    # arbitrary ports without editing generated link targets.
    derived = {}
    for spec in site_list:
        derived.setdefault(spec.role, spec.origin)
    base = config or RoundConfig()
    extras = dict(base.extras)
    extras["origins"] = {**derived, **extras.get("origins", {})}
    world = World(
        RoundConfig(
            slow_seconds=base.slow_seconds,
            slow_release_after=base.slow_release_after,
            rate_limit_every=base.rate_limit_every,
            generation=base.generation,
            extras=extras,
        ),
        log_path,
    )
    servers: list[SiteServer] = []
    for spec in site_list:
        srv = SiteServer(spec, world)
        thread = threading.Thread(target=srv.serve_forever, name=f"site-{spec.name}", daemon=True)
        thread.start()
        servers.append(srv)
    if ready_file is not None:
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text(
            json.dumps(
                {
                    "sites": [
                        {"name": s.name, "role": s.role, "port": s.port, "origin": s.origin}
                        for s in site_list
                    ],
                    "log": str(log_path),
                    "pid": os.getpid(),
                    "started_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return servers, world


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local synthetic site group")
    parser.add_argument("--log", default=".runtime/site_access.log")
    parser.add_argument("--ready-file", default=".runtime/sites-ready.json")
    parser.add_argument("--slow-seconds", type=float, default=2.5)
    parser.add_argument("--slow-release-after", type=float, default=1.2)
    parser.add_argument("--rate-limit-every", type=int, default=3)
    parser.add_argument("--nav-port", type=int, default=8811)
    argparse_args = parser.parse_args(argv)
    servers, _world = start_group(
        log_path=Path(argparse_args.log),
        sites=default_sites(argparse_args.nav_port),
        config=RoundConfig(
            slow_seconds=argparse_args.slow_seconds,
            slow_release_after=argparse_args.slow_release_after,
            rate_limit_every=argparse_args.rate_limit_every,
        ),
        ready_file=Path(argparse_args.ready_file),
    )
    print(f"synthetic site group ready: {len(servers)} servers")
    for srv in servers:
        print(f"  {srv.spec.name} {srv.spec.origin}")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        for srv in servers:
            srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
