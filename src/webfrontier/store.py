"""SQLite-backed frontier, ledger, link graph, details and request log.

The database is the only place where the full set of discovered URLs lives.
The crawler keeps at most one claimed row per worker in memory, so RSS does not
grow with the size of the frontier.  All state transitions of a page are
committed atomically together with its details and newly discovered links,
which is what makes a pause/crash safe: a leftover ``in_flight`` row is
requeued on restart and is never mistaken for a success.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .urls import (
    DiscoveredLink,
    Scope,
    URLIdentity,
    URLIdentityError,
    parse_identity,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=30000;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robots (
    origin TEXT PRIMARY KEY,
    state TEXT NOT NULL,          -- ready | blocked | deferred
    source TEXT NOT NULL,         -- parsed | absent | unfetchable
    status INTEGER,
    rules_json TEXT NOT NULL,
    body TEXT,
    matched_group TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical TEXT NOT NULL UNIQUE,
    request_url TEXT NOT NULL,
    origin TEXT NOT NULL,
    depth INTEGER NOT NULL,
    state TEXT NOT NULL,          -- pending | retry_wait | in_flight | done
    disposition TEXT,
    detail TEXT,
    http_status INTEGER,
    final_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    discovered_count INTEGER NOT NULL DEFAULT 1,
    source_canonical TEXT,
    source_url TEXT,
    source_text TEXT,
    entry_url TEXT,
    redirect_chain TEXT,
    not_before REAL NOT NULL DEFAULT 0,
    lease TEXT,
    seq INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS urls_pick ON urls(origin, state, not_before, seq);
CREATE INDEX IF NOT EXISTS urls_state ON urls(state);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_canonical TEXT NOT NULL,
    source_url TEXT NOT NULL,
    target_canonical TEXT NOT NULL,
    target_request_url TEXT NOT NULL,
    raw_href TEXT NOT NULL,
    text TEXT NOT NULL,
    base_url TEXT NOT NULL,
    kind TEXT NOT NULL,           -- page | attachment
    occurrences INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_canonical, target_canonical, raw_href)
);
CREATE INDEX IF NOT EXISTS links_target ON links(target_canonical);
CREATE INDEX IF NOT EXISTS links_source ON links(source_canonical);

CREATE TABLE IF NOT EXISTS details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical TEXT NOT NULL UNIQUE,
    item_id TEXT,
    title TEXT NOT NULL,
    fetch_url TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    source_canonical TEXT,
    source_url TEXT,
    entry_url TEXT,
    redirect_chain TEXT,
    first_seen REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS details_item ON details(item_id) WHERE item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detail_canonical TEXT NOT NULL,
    canonical TEXT NOT NULL,
    request_url TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    registered_at REAL NOT NULL,
    UNIQUE(detail_canonical, canonical, request_url)
);
CREATE INDEX IF NOT EXISTS attachments_canon ON attachments(canonical);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    origin TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,           -- page | robots
    method TEXT NOT NULL,
    status INTEGER,
    outcome TEXT NOT NULL,
    redirect_from TEXT,
    elapsed_ms REAL,
    bytes_read INTEGER,
    content_type TEXT,
    worker TEXT
);
CREATE INDEX IF NOT EXISTS requests_origin ON requests(origin, ts);
CREATE INDEX IF NOT EXISTS requests_url ON requests(url);

CREATE TABLE IF NOT EXISTS dispositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical TEXT NOT NULL,
    request_url TEXT NOT NULL,
    disposition TEXT NOT NULL,
    detail TEXT,
    http_status INTEGER,
    final_url TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dispositions_canon ON dispositions(canonical);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pending INTEGER,
    inflight INTEGER,
    retry_wait INTEGER,
    done INTEGER,
    page_requests INTEGER,
    rss_kb INTEGER,
    peak_rss_kb INTEGER
);
"""

TERMINAL_DISPOSITIONS = {
    "success",
    "binary_linked",
    "not_html",
    "oversize",
    "robots_denied",
    "robots_unavailable",
    "out_of_scope",
    "too_deep",
    "over_budget",
    "parse_failed",
    "retries_exhausted",
    "http_error",
    "too_many_redirects",
    "redirect_loop",
    "redirect_alias",
    "invalid_url",
}


class Store:
    """Thread-safe wrapper: SQLite connections are not shareable across threads.

    A dedicated connection is opened per thread and all writes are serialised
    by a process-wide lock, which keeps BEGIN IMMEDIATE deadlocks away while
    still allowing concurrent reads from one thread at a time.
    """

    def __init__(self, path: str | Path, *, reset: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if reset and self.path.exists():
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    p.unlink()
        self._local = threading.local()
        self._write_lock = threading.RLock()
        conn = self._conn()
        conn.executescript(SCHEMA)
        self._seq = self._read_seq()
        self._seq_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        with self._write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self._conn()

    def _read_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM urls").fetchone()
        return int(row["s"])

    def next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # -- meta ---------------------------------------------------------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def meta_int(self, key: str, default: int = 0) -> int:
        value = self.meta_get(key)
        return int(value) if value is not None else default

    def meta_incr(self, key: str, amount: int = 1) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)",
                (key, str(amount), amount),
            )

    # -- restart safety ----------------------------------------------
    def requeue_interrupted(self) -> int:
        """Leftover in_flight leases are never successes: put them back."""
        with self.tx() as c:
            n = c.execute(
                "UPDATE urls SET state='pending', lease=NULL, not_before=0, "
                "detail='requeued after restart (was in_flight)', updated_at=? "
                "WHERE state='in_flight'",
                (time.time(),),
            ).rowcount
            return n

    # -- robots -------------------------------------------------------
    def robots_row(self, origin: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM robots WHERE origin=?", (origin,)
        ).fetchone()

    def robots_write(
        self,
        origin: str,
        *,
        state: str,
        source: str,
        status: int | None,
        rules: list[dict] | None = None,
        body: str = "",
        matched_group: str = "",
        attempts: int = 0,
        next_try: float = 0.0,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO robots(origin,state,source,status,rules_json,body,matched_group,"
                "attempts,next_try,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(origin) DO UPDATE SET state=excluded.state, source=excluded.source, "
                "status=excluded.status, rules_json=excluded.rules_json, body=excluded.body, "
                "matched_group=excluded.matched_group, attempts=excluded.attempts, "
                "next_try=excluded.next_try, fetched_at=excluded.fetched_at",
                (
                    origin, state, source, status,
                    json.dumps(rules or [], ensure_ascii=False),
                    body, matched_group, attempts, next_try, time.time(),
                ),
            )

    # -- seeds --------------------------------------------------------
    def add_seed(
        self, url: str, *, scope: Scope, max_depth: int
    ) -> str:
        try:
            ident = parse_identity(url)
        except URLIdentityError as exc:
            raise ValueError(f"seed {url!r} is not a valid http(s) URL: {exc}") from exc
        wire = url.split("#", 1)[0]
        now = time.time()
        state, disposition, detail = "pending", None, "seed"
        if not scope.allows(ident):
            state, disposition = "done", "out_of_scope"
            detail = "seed origin/path prefix is outside the configured scope"
        elif 0 > max_depth:
            state, disposition = "done", "too_deep"
            detail = "seed depth exceeds max_depth"
        with self.tx() as c:
            existing = c.execute(
                "SELECT id FROM urls WHERE canonical=?", (ident.canonical,)
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE urls SET discovered_count=discovered_count+1, updated_at=? WHERE id=?",
                    (now, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO urls(canonical,request_url,origin,depth,state,disposition,detail,"
                    "http_status,final_url,attempts,retries,discovered_count,source_canonical,"
                    "source_url,source_text,entry_url,redirect_chain,not_before,lease,seq,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,NULL,NULL,0,0,1,NULL,NULL,NULL,?,NULL,0,NULL,?,?)",
                    (
                        ident.canonical, wire, ident.origin, 0, state, disposition, detail,
                        wire, self.next_seq(), now,
                    ),
                )
                if disposition:
                    c.execute(
                        "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                        "http_status,final_url,ts) VALUES(?,?,?,?,NULL,NULL,?)",
                        (ident.canonical, wire, disposition, detail, now),
                    )
        return ident.canonical

    # -- claim --------------------------------------------------------
    def url_state(self, canonical: str) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM urls WHERE canonical=?", (canonical,)
        ).fetchone()
        return row["state"] if row else None

    def _next_row_for_origin(self, origin: str, now: float, after_id: int = 0):
        return self.conn.execute(
            "SELECT * FROM urls WHERE origin=? AND state IN ('pending','retry_wait') "
            "AND not_before<=? AND id>? ORDER BY seq LIMIT 1",
            (origin, now, after_id),
        ).fetchone()

    def claim_next(self, origin: str, lease: str, now: float, rules) -> dict | None:
        """Claim one processable row for origin.

        Rows whose path robots.txt disallows are terminated without any HTTP
        request and without consuming a page attempt.  This guarantees the
        server never receives a request for a disallowed path.
        """
        cursor = 0
        while True:
            row = self._next_row_for_origin(origin, now, cursor)
            if row is None:
                return None
            cursor = int(row["id"])
            try:
                ident = parse_identity(row["canonical"])
            except URLIdentityError:
                self._finish_row(
                    row, "invalid_url", "canonical identity became unparseable",
                    None, None, now,
                )
                continue
            if rules is not None and not rules.allowed(ident):
                self._finish_row(
                    row, "robots_denied",
                    f"robots.txt disallows {ident.path_query}", None, row["request_url"], now,
                )
                self.meta_incr("robots_denied_count", 1)
                continue
            with self.tx() as c:
                cur = c.execute(
                    "UPDATE urls SET state='in_flight', lease=?, attempts=attempts+1, "
                    "retries=CASE WHEN state='retry_wait' THEN retries+1 ELSE retries END, "
                    "updated_at=? WHERE id=? AND state IN ('pending','retry_wait')",
                    (lease, now, row["id"]),
                )
                if cur.rowcount != 1:
                    continue
            claimed = dict(row)
            claimed["state"] = "in_flight"
            claimed["lease"] = lease
            claimed["attempts"] = int(row["attempts"]) + 1
            return claimed

    def _finish_row(
        self, row, disposition: str, detail: str, http_status: int | None,
        final_url: str | None, now: float,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE urls SET state='done', disposition=?, detail=?, http_status=?, "
                "final_url=?, lease=NULL, updated_at=? WHERE id=?",
                (disposition, detail, http_status, final_url, now, row["id"]),
            )
            c.execute(
                "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
                (row["canonical"], row["request_url"], disposition, detail, http_status, final_url, now),
            )

    def finish(
        self, row, disposition: str, detail: str, http_status: int | None,
        final_url: str | None, now: float,
    ) -> None:
        self._finish_row(row, disposition, detail, http_status, final_url, now)

    def schedule_retry(
        self, row, *, not_before: float, detail: str, http_status: int | None,
    ) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE urls SET state='retry_wait', not_before=?, detail=?, http_status=?, "
                "lease=NULL, updated_at=? WHERE id=?",
                (not_before, detail, http_status, now, row["id"]),
            )
            c.execute(
                "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                "http_status,final_url,ts) VALUES(?,?,?,?,?,NULL,?)",
                (row["canonical"], row["request_url"], "retry_wait", detail, http_status, now),
            )
            c.execute(
                "INSERT INTO meta(key,value) VALUES('retries_scheduled','1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
            )

    def release_pending(self, row, note: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE urls SET state='pending', lease=NULL, not_before=0, detail=?, "
                "updated_at=? WHERE id=? AND state='in_flight'",
                (note, time.time(), row["id"]),
            )

    # -- redirects ----------------------------------------------------
    def claim_redirect_target(
        self,
        source_row: dict,
        ident: URLIdentity,
        request_url: str,
        chain: list[dict],
        now: float,
        lease: str,
    ) -> tuple[str, dict | None]:
        """Follow one redirect hop.

        Returns ``("claimed", row)`` when this worker must fetch the target, or
        ``("exists", row)`` when the target identity is already tracked.  In
        both cases the redirecting row becomes a terminal alias, which is how a
        detail reached through several entry points is extracted only once.
        """
        chain_json = json.dumps(chain, ensure_ascii=False)
        with self.tx() as c:
            existing = c.execute(
                "SELECT * FROM urls WHERE canonical=?", (ident.canonical,)
            ).fetchone()
            alias_detail = f"redirect target {ident.canonical}"
            if existing is None:
                c.execute(
                    "INSERT INTO urls(canonical,request_url,origin,depth,state,disposition,detail,"
                    "http_status,final_url,attempts,retries,discovered_count,source_canonical,"
                    "source_url,source_text,entry_url,redirect_chain,not_before,lease,seq,updated_at) "
                    "VALUES(?,?,?,?,'in_flight',NULL,NULL,NULL,NULL,1,0,1,?,?,?,?,?,0,?,?,?)",
                    (
                        ident.canonical, request_url, ident.origin, source_row["depth"],
                        source_row["source_canonical"], source_row["source_url"],
                        source_row["source_text"],
                        source_row.get("entry_url") or source_row["request_url"],
                        chain_json, lease, self.next_seq(), now,
                    ),
                )
                new_row = dict(c.execute(
                    "SELECT * FROM urls WHERE canonical=?", (ident.canonical,)
                ).fetchone())
                c.execute(
                    "UPDATE urls SET state='done', disposition='redirect_alias', detail=?, "
                    "final_url=?, lease=NULL, updated_at=? WHERE id=?",
                    (alias_detail, request_url, now, source_row["id"]),
                )
                c.execute(
                    "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                    "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
                    (
                        source_row["canonical"], source_row["request_url"],
                        "redirect_alias", alias_detail, None, request_url, now,
                    ),
                )
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('redirect_hops','1') "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
                )
                return "claimed", new_row

            if existing["state"] == "done":
                # Terminal target (robots-denied, binary, out of scope, ...).
                # Record the alias and stop: it must never be fetched again.
                c.execute(
                    "UPDATE urls SET state='done', disposition='redirect_alias', detail=?, "
                    "final_url=?, lease=NULL, updated_at=? WHERE id=?",
                    (
                        f"{alias_detail} (terminal target, {existing['disposition']})",
                        request_url, now, source_row["id"],
                    ),
                )
                c.execute(
                    "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                    "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
                    (
                        source_row["canonical"], source_row["request_url"], "redirect_alias",
                        f"{alias_detail} (terminal target, {existing['disposition']})",
                        None, request_url, now,
                    ),
                )
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('redirect_hops','1') "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
                )
                return "finished", dict(existing)

            c.execute(
                "UPDATE urls SET state='done', disposition='redirect_alias', detail=?, "
                "final_url=?, lease=NULL, updated_at=? WHERE id=?",
                (
                    f"{alias_detail} (already tracked, state {existing['state']})",
                    request_url, now, source_row["id"],
                ),
            )
            c.execute(
                "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
                (
                    source_row["canonical"], source_row["request_url"], "redirect_alias",
                    f"{alias_detail} (already tracked)", None, request_url, now,
                ),
            )
            if (
                existing["state"] in ("pending", "retry_wait")
                and int(source_row["depth"]) < int(existing["depth"])
            ):
                c.execute(
                    "UPDATE urls SET depth=?, updated_at=? WHERE id=?",
                    (source_row["depth"], now, existing["id"]),
                )
            c.execute(
                "INSERT INTO meta(key,value) VALUES('redirect_hops','1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
            )
            return "exists", dict(existing)

    # -- success ------------------------------------------------------
    def record_refused_redirect(
        self,
        *,
        source_row: dict,
        target_canonical: str,
        target_request_url: str,
        disposition: str,
        detail: str,
        http_status: int,
    ) -> None:
        """Terminate the redirecting URL when its target is refused.

        The target itself is registered as a terminal ``out_of_scope`` /
        ``robots_denied`` URL so the ledger explains why it was never fetched.
        No request is ever sent to the target.
        """
        now = time.time()
        with self.tx() as c:
            self._finish_row_in(
                c,
                source_row,
                disposition,
                detail,
                http_status,
                target_request_url,
                now,
            )
            try:
                ident = parse_identity(target_canonical)
            except URLIdentityError:
                return
            existing = c.execute(
                "SELECT id FROM urls WHERE canonical=?", (ident.canonical,)
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO urls(canonical,request_url,origin,depth,state,disposition,"
                    "detail,http_status,final_url,attempts,retries,discovered_count,"
                    "source_canonical,source_url,source_text,entry_url,redirect_chain,"
                    "not_before,lease,seq,updated_at) "
                    "VALUES(?,?,?,?,'done',?,?,NULL,?,0,0,1,?,?,?,?,NULL,0,NULL,?,?)",
                    (
                        ident.canonical, target_request_url, ident.origin,
                        int(source_row["depth"]) + 1, disposition, detail,
                        target_request_url,
                        source_row["canonical"], source_row["request_url"],
                        "redirect target", target_request_url, self.next_seq(), now,
                    ),
                )
                c.execute(
                    "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                    "http_status,final_url,ts) VALUES(?,?,?,?,NULL,?,?)",
                    (ident.canonical, target_request_url, disposition, detail,
                     target_request_url, now),
                )

    def _finish_row_in(
        self,
        c: sqlite3.Connection,
        row: dict,
        disposition: str,
        detail: str,
        http_status: int | None,
        final_url: str | None,
        now: float,
    ) -> None:
        c.execute(
            "UPDATE urls SET state='done', disposition=?, detail=?, http_status=?, "
            "final_url=?, lease=NULL, updated_at=? WHERE id=?",
            (disposition, detail, http_status, final_url, now, row["id"]),
        )
        c.execute(
            "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
            "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
            (row["canonical"], row["request_url"], disposition, detail, http_status,
             final_url, now),
        )

    # -- success ------------------------------------------------------
    def save_page(
        self,
        row: dict,
        *,
        final_canonical: str,
        final_request_url: str,
        http_status: int,
        title: str,
        item_id: str | None,
        links: list[DiscoveredLink],
        attachments: list[DiscoveredLink],
        max_depth: int,
        scope: Scope,
        redirect_chain: list[dict],
        now: float,
    ) -> dict:
        """One transaction: page success + details + edges + discoveries.

        ``item_id`` is declared by the page.  When it is absent the page is
        still a successful crawl and its links are followed, but it is not a
        directory *detail* record and gets no row in ``details``.
        """
        stats = {
            "links": 0,
            "new_urls": 0,
            "existing_urls": 0,
            "upgraded_urls": 0,
            "new_edges": 0,
            "duplicate_discoveries": 0,
            "detail_inserted": False,
            "duplicate_details": 0,
        }
        chain_json = json.dumps(redirect_chain, ensure_ascii=False) if redirect_chain else None
        with self.tx() as c:
            c.execute(
                "UPDATE urls SET state='done', disposition='success', detail=?, http_status=?, "
                "final_url=?, lease=NULL, updated_at=? WHERE id=?",
                ("page parsed", http_status, final_request_url, now, row["id"]),
            )
            c.execute(
                "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                "http_status,final_url,ts) VALUES(?,?,?,?,?,?,?)",
                (
                    row["canonical"], row["request_url"], "success", "page parsed",
                    http_status, final_request_url, now,
                ),
            )
            if item_id is not None:
                # A detail row exists only for pages that declare a stable
                # item_id.  The unique index on item_id means a second entry
                # point for the same record does not create a duplicate row.
                cur = c.execute(
                    "INSERT OR IGNORE INTO details(canonical,item_id,title,fetch_url,http_status,"
                    "source_canonical,source_url,entry_url,redirect_chain,first_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        final_canonical, item_id, title, final_request_url, http_status,
                        row.get("source_canonical"), row.get("source_url"),
                        row.get("entry_url") or row["request_url"], chain_json, now,
                    ),
                )
                stats["detail_inserted"] = cur.rowcount > 0
                if cur.rowcount == 0:
                    stats["duplicate_details"] = 1

            for link, kind in [(l, "page") for l in links] + [(a, "attachment") for a in attachments]:
                stats["links"] += 1
                cur = c.execute(
                    "INSERT INTO links(source_canonical,source_url,target_canonical,"
                    "target_request_url,raw_href,text,base_url,kind,occurrences) "
                    "VALUES(?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(source_canonical,target_canonical,raw_href) "
                    "DO UPDATE SET occurrences=occurrences+1",
                    (
                        final_canonical, final_request_url, link.canonical, link.request_url,
                        link.raw_href, link.text, link.base_url, kind,
                    ),
                )
                if cur.rowcount and kind == "page":
                    stats["new_edges"] += 1

                try:
                    ident = parse_identity(link.canonical)
                except URLIdentityError:
                    state, disposition, detail = "done", "invalid_url", "identity parse failed"
                    c.execute(
                        "INSERT INTO urls(canonical,request_url,origin,depth,state,disposition,"
                        "detail,http_status,final_url,attempts,retries,discovered_count,"
                        "source_canonical,source_url,source_text,entry_url,redirect_chain,"
                        "not_before,lease,seq,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,NULL,NULL,0,0,1,?,?,?,NULL,NULL,0,NULL,?,?)",
                        (
                            link.canonical, link.request_url, "invalid", max_depth + 1,
                            state, disposition, detail, final_canonical, final_request_url,
                            link.text, self.next_seq(), now,
                        ),
                    )
                    c.execute(
                        "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                        "http_status,final_url,ts) VALUES(?,?,?,?,NULL,NULL,?)",
                        (link.canonical, link.request_url, disposition, detail, now),
                    )
                    continue

                new_depth = int(row["depth"]) + 1
                state, disposition, detail = "pending", None, None
                if not scope.allows(ident):
                    state, disposition = "done", "out_of_scope"
                    detail = f"{ident.origin}{ident.path} outside configured scope"
                elif new_depth > max_depth:
                    state, disposition = "done", "too_deep"
                    detail = f"depth {new_depth} > max_depth {max_depth}"
                elif not link.is_page_hint:
                    state, disposition = "done", "binary_linked"
                    detail = "binary-looking link registered without a page request"

                existing = c.execute(
                    "SELECT * FROM urls WHERE canonical=?", (ident.canonical,)
                ).fetchone()
                outcome = "new"
                if existing is not None:
                    c.execute(
                        "UPDATE urls SET discovered_count=discovered_count+1, updated_at=? WHERE id=?",
                        (now, existing["id"]),
                    )
                    outcome = "existing"
                    if (
                        existing["state"] == "done"
                        and existing["disposition"] == "too_deep"
                        and new_depth <= max_depth
                        and scope.allows(ident)
                    ):
                        c.execute(
                            "UPDATE urls SET state='pending', disposition=NULL, detail=NULL, "
                            "depth=?, source_canonical=?, source_url=?, source_text=?, "
                            "updated_at=? WHERE id=?",
                            (new_depth, final_canonical, final_request_url, link.text, now, existing["id"]),
                        )
                        outcome = "upgraded"
                else:
                    c.execute(
                        "INSERT INTO urls(canonical,request_url,origin,depth,state,disposition,"
                        "detail,http_status,final_url,attempts,retries,discovered_count,"
                        "source_canonical,source_url,source_text,entry_url,redirect_chain,"
                        "not_before,lease,seq,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,NULL,NULL,0,0,1,?,?,?,?,NULL,0,NULL,?,?)",
                        (
                            ident.canonical, link.request_url, ident.origin, new_depth,
                            state, disposition, detail, final_canonical, final_request_url,
                            link.text, link.request_url, self.next_seq(), now,
                        ),
                    )
                    if disposition:
                        c.execute(
                            "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                            "http_status,final_url,ts) VALUES(?,?,?,?,NULL,NULL,?)",
                            (ident.canonical, link.request_url, disposition, detail, now),
                        )

                if outcome == "new":
                    stats["new_urls"] += 1
                elif outcome == "upgraded":
                    stats["upgraded_urls"] += 1
                else:
                    stats["existing_urls"] += 1
                    stats["duplicate_discoveries"] += 1

                if kind == "attachment" or not link.is_page_hint:
                    c.execute(
                        "INSERT OR IGNORE INTO attachments(detail_canonical,canonical,request_url,"
                        "text,kind,registered_at) VALUES(?,?,?,?, 'binary', ?)",
                        (final_canonical, link.canonical, link.request_url, link.text, now),
                    )

            if stats["links"]:
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('new_discoveries',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)",
                    (stats["new_urls"], stats["new_urls"]),
                )
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('duplicate_discoveries',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)",
                    (stats["duplicate_discoveries"], stats["duplicate_discoveries"]),
                )
        return stats

    # -- request log --------------------------------------------------
    def log_request(
        self,
        *,
        origin: str,
        url: str,
        kind: str,
        status: int | None,
        outcome: str,
        redirect_from: str | None,
        started: float,
        elapsed_ms: float | None,
        bytes_read: int | None,
        content_type: str | None,
        worker: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO requests(ts,origin,url,kind,method,status,outcome,redirect_from,"
                "elapsed_ms,bytes_read,content_type,worker) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    started, origin, url, kind, "GET", status, outcome, redirect_from,
                    elapsed_ms, bytes_read, content_type, worker,
                ),
            )

    # -- budget / stopping -------------------------------------------
    def consume_page_budget(self, limit: int) -> bool:
        with self.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key='page_requests'").fetchone()
            used = int(row["value"]) if row else 0
            if used >= limit:
                return False
            c.execute(
                "INSERT INTO meta(key,value) VALUES('page_requests',?) "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)",
                ("1",),
            )
            return True

    def budget_used(self) -> int:
        return self.meta_int("page_requests", 0)

    def mark_remaining(self, disposition: str, reason: str) -> int:
        now = time.time()
        with self.tx() as c:
            rows = c.execute(
                "SELECT id, canonical, request_url FROM urls "
                "WHERE state IN ('pending','retry_wait','in_flight')"
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE urls SET state='done', disposition=?, detail=?, lease=NULL, "
                    "updated_at=? WHERE id=?",
                    (disposition, reason, now, r["id"]),
                )
                c.execute(
                    "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                    "http_status,final_url,ts) VALUES(?,?,?,?,NULL,NULL,?)",
                    (r["canonical"], r["request_url"], disposition, reason, now),
                )
            if rows:
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('budget_stopped_urls',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(len(rows)),),
                )
            return len(rows)

    def mark_origin_unavailable(self, origin: str, reason: str) -> int:
        now = time.time()
        with self.tx() as c:
            rows = c.execute(
                "SELECT id, canonical, request_url FROM urls WHERE origin=? "
                "AND state IN ('pending','retry_wait','in_flight')",
                (origin,),
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE urls SET state='done', disposition='robots_unavailable', detail=?, "
                    "lease=NULL, updated_at=? WHERE id=?",
                    (reason, now, r["id"]),
                )
                c.execute(
                    "INSERT INTO dispositions(canonical,request_url,disposition,detail,"
                    "http_status,final_url,ts) VALUES(?,?,?,?,NULL,NULL,?)",
                    (r["canonical"], r["request_url"], "robots_unavailable", reason, now),
                )
            return len(rows)

    # -- counts -------------------------------------------------------
    def state_counts(self) -> dict[str, int]:
        counts = {
            r["state"]: int(r["n"])
            for r in self.conn.execute("SELECT state, COUNT(*) AS n FROM urls GROUP BY state")
        }
        for key in ("pending", "in_flight", "retry_wait", "done"):
            counts.setdefault(key, 0)
        return counts

    def disposition_counts(self) -> dict[str, int]:
        return {
            r["disposition"]: int(r["n"])
            for r in self.conn.execute(
                "SELECT disposition, COUNT(*) AS n FROM urls WHERE state='done' "
                "AND disposition IS NOT NULL GROUP BY disposition ORDER BY n DESC"
            )
        }

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        if table not in {"urls", "details", "links", "attachments", "requests", "dispositions", "metrics"}:
            raise ValueError(table)
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def has_potential_work(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM urls WHERE state IN ('pending','in_flight','retry_wait') LIMIT 1"
        ).fetchone()
        return row is not None

    def has_ready_work(self, now: float) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM urls WHERE state='in_flight' OR (state IN ('pending','retry_wait') "
            "AND not_before<=?) LIMIT 1",
            (now,),
        ).fetchone()
        return row is not None

    def earliest_not_before(self) -> float | None:
        row = self.conn.execute(
            "SELECT MIN(not_before) AS t FROM urls WHERE state='retry_wait'"
        ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def pending_eligible(self, origin: str, now: float) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM urls WHERE origin=? AND state IN ('pending','retry_wait') "
            "AND not_before<=? LIMIT 1",
            (origin, now),
        ).fetchone() is not None

    # -- metrics ------------------------------------------------------
    def sample_metrics(self, rss_kb: int, peak_rss_kb: int) -> None:
        counts = self.state_counts()
        with self.tx() as c:
            c.execute(
                "INSERT INTO metrics(ts,pending,inflight,retry_wait,done,page_requests,rss_kb,peak_rss_kb) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    time.time(), counts["pending"], counts["in_flight"], counts["retry_wait"],
                    counts["done"], self.budget_used(), rss_kb, peak_rss_kb,
                ),
            )
            c.execute(
                "DELETE FROM metrics WHERE id <= (SELECT MAX(id)-20000 FROM metrics)"
            )

    def metrics_summary(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT MAX(pending) AS max_pending, MAX(inflight) AS max_inflight, "
            "MAX(retry_wait) AS max_retry_wait, MAX(rss_kb) AS max_rss_kb, "
            "MAX(peak_rss_kb) AS peak_rss_kb, MAX(page_requests) AS max_page_requests, "
            "COUNT(*) AS samples FROM metrics"
        ).fetchone()
        return dict(row) if row else {}
