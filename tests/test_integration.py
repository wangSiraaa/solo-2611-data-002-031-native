"""Integration tests: real crawling against an ephemeral synthetic site group.

Each test binds the whole synthetic group on free ports, builds a matching task
configuration, runs the real crawler, and then checks the *server's* access log,
so nothing is validated from the crawler's own bookkeeping alone.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from webfrontier.config import OriginConfig, Limits, TaskConfig
from webfrontier.engine import Crawler
from webfrontier.site import RoundConfig, SiteSpec, start_group
from webfrontier.store import Store
from webfrontier.urls import Scope


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SITE_ROLES = [
    ("site-a", "A"),
    ("site-b", "B"),
    ("site-c", "C"),
    ("site-d", "D"),
    ("site-e", "E"),
]


@pytest.fixture(scope="module")
def group(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("group")
    ports = {name: free_port() for name, _ in SITE_ROLES}
    trap_port = free_port()
    specs = [SiteSpec(name, ports[name], role) for name, role in SITE_ROLES]
    specs.append(SiteSpec("trap", trap_port, "TRAP"))
    log = tmp / "access.log"
    servers, world = start_group(
        log_path=log,
        sites=specs,
        config=RoundConfig(slow_seconds=0.4, slow_release_after=0.2, rate_limit_every=3),
        ready_file=None,
    )
    time.sleep(0.1)
    yield {
        "ports": ports,
        "trap_port": trap_port,
        "log": log,
        "world": world,
        "servers": servers,
        "origins": {role: f"http://127.0.0.1:{ports[name]}" for name, role in SITE_ROLES},
    }
    for srv in servers:
        srv.shutdown()


def build_config(group, tmp: Path) -> TaskConfig:
    origins = []
    for name, role in SITE_ROLES:
        origin = group["origins"][role]
        origins.append(
            OriginConfig(
                origin=origin,
                path_prefixes=["/"],
                seeds=[origin + "/"],
                concurrency=2 if role in ("A", "B") else 1,
                min_interval=0.05,
            )
        )
    scope = Scope([(o.origin, "/") for o in origins])
    limits = Limits(
        max_depth=3,
        page_budget=500,
        max_redirects=4,
        per_origin_concurrency=2,
        min_request_interval=0.05,
        max_body_bytes=262144,
        timeout_seconds=4.0,
        max_attempts=3,
        retry_backoff_seconds=[0.1, 0.2, 0.4],
        metrics_every=10,
    )
    return TaskConfig(
        path=tmp / "config.json",
        database=tmp / "crawl.db",
        origins=origins,
        limits=limits,
        user_agent="CoopDirectoryBot/1.0 (+local synthetic directory crawler)",
        scope=scope,
        raw={},
    )


def read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_crawl(config: TaskConfig):
    store = Store(config.database, reset=True)
    crawler = Crawler(config, store)
    crawler.seed()
    result = crawler.run()
    return store, result


@pytest.fixture(scope="module")
def crawled(group, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("crawl")
    config = build_config(group, tmp)
    store, result = run_crawl(config)
    yield group, config, store, result
    store.close()


def test_crawl_completes_with_no_open_states(crawled):
    _group, _config, store, result = crawled
    assert result.complete
    counts = store.state_counts()
    assert counts["pending"] == 0 and counts["in_flight"] == 0 and counts["retry_wait"] == 0


def test_disallowed_and_out_of_scope_never_requested(crawled):
    group, _config, _store, _result = crawled
    log = read_log(group["log"])
    for path in [
        "/private/hidden",
        "/quarantine/rack.html",
        "/public/secret",
        "/go/secret",
        "/blocked/hidden",
    ]:
        assert [r for r in log if r["path"] == path] == [], path
    assert [r for r in log if r["port"] == group["trap_port"]] == []


def test_server_observed_interval(crawled):
    group, config, _store, _result = crawled
    log = read_log(group["log"])
    by_origin: dict[str, list[float]] = {}
    for record in log:
        by_origin.setdefault(record["origin"], []).append(record["ts"])
    for origin, stamps in by_origin.items():
        stamps.sort()
        required = config.origin_config(origin).min_interval
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert not gaps or min(gaps) >= required - 0.002


def test_slow_site_does_not_block_other_origins(crawled):
    group, _config, _store, _result = crawled
    log = read_log(group["log"])
    slow = [r["ts"] for r in log if r["path"].startswith("/slow/")]
    assert slow
    lo, hi = min(slow), max(slow)
    during = [
        r for r in log
        if lo <= r["ts"] <= hi and not r["path"].startswith("/slow") and r["site"] != "site-c"
    ]
    assert during


def test_details_deduplicated_and_complete(crawled):
    _group, _config, store, _result = crawled
    rows = store.conn.execute("SELECT item_id, canonical FROM details").fetchall()
    ids = [r["item_id"] for r in rows]
    expected = {f"A-{i}" for i in range(1, 9)} | {"B-1", "D-1"} | {f"C-{i}" for i in range(1, 7)}
    assert set(ids) == expected
    assert len(ids) == len(set(ids))
    for row in store.conn.execute("SELECT canonical, fetch_url, source_canonical, entry_url FROM details"):
        assert row["fetch_url"].startswith("http://127.0.0.1:")
        assert row["source_canonical"] is not None or row["entry_url"]


def test_disposition_coverage(crawled):
    _group, _config, store, _result = crawled
    dispositions = store.disposition_counts()
    for key in (
        "success", "robots_denied", "out_of_scope", "redirect_alias",
        "redirect_loop", "retries_exhausted", "not_html", "oversize",
        "binary_linked", "too_deep", "parse_failed", "too_many_redirects",
    ):
        assert dispositions.get(key, 0) >= 1, f"missing disposition {key}: {dispositions}"


def test_binary_links_registered_not_fetched(crawled):
    group, _config, store, _result = crawled
    att = store.conn.execute(
        "SELECT canonical FROM attachments WHERE canonical LIKE '%/files/%' LIMIT 1"
    ).fetchone()
    assert att is not None
    log = read_log(group["log"])
    assert not [r for r in log if r["path"].startswith("/files/")]
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM urls WHERE canonical LIKE '%/files/%' AND disposition='binary_linked'"
    ).fetchone()["n"] >= 1


def test_redirect_alias_prevents_second_detail_extraction(crawled):
    group, _config, store, _result = crawled
    # 60 aliases all redirect to a handful of A details; each detail has one row
    assert store.conn.execute("SELECT COUNT(*) AS n FROM details WHERE item_id LIKE 'A-%'").fetchone()["n"] == 8
    log = read_log(group["log"])
    assert len([r for r in log if r["path"] == "/detail/A-1"]) >= 1
    # The alias URLs themselves were fetched once each, not their targets again
    go_hits = [r for r in log if r["path"].startswith("/go/")]
    assert len(go_hits) == len({r["path"] for r in go_hits})


def test_pause_resume_preserves_completed_pages(crawled):
    group, config, store, _result = crawled
    # A second run on the finished frontier must be a no-op: no new requests.
    log_before = len(read_log(group["log"]))
    store2 = Store(config.database)
    crawler = Crawler(config, store2)
    result = crawler.run()
    assert result.complete
    assert crawler.store.count("urls") == store.count("urls")
    log_after = read_log(group["log"])
    new_requests = [r for r in log_after[log_before:] if r["site"] != "trap"]
    assert new_requests == [], "a finished frontier must not trigger new HTTP requests"
    store2.close()


def test_requeue_inflight_never_success(tmp_path):
    store = Store(tmp_path / "x.db")
    with store.tx() as c:
        c.execute(
            "INSERT INTO urls(canonical,request_url,origin,depth,state,seq,updated_at,"
            "not_before,attempts,retries,discovered_count,lease) "
            "VALUES('http://h/a','http://h/a','http://h',0,'in_flight',1,0,0,1,0,1,'dead')"
        )
    n = store.requeue_interrupted()
    assert n == 1
    row = store.conn.execute("SELECT state, disposition, lease FROM urls").fetchone()
    assert row["state"] == "pending"
    assert row["disposition"] is None
    assert row["lease"] is None
    store.close()
