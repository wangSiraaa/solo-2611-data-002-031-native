"""Storage tests: atomic publish, concurrency, idempotent retry, verify."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from schedc import store
from schedc.cli import sample_document
from schedc.compiler import compile_rules
from schedc.models import CompileError, load_document, parse_instant
from tests.conftest import DB_AVAILABLE, TEST_DSN

pytestmark = pytest.mark.skipif(not DB_AVAILABLE,
                                reason="test database not available")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WINDOW = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")


def publish_sample(c, request_id, expected_version, start=WINDOW[0],
                   end=WINDOW[1], doc=None):
    rules, exceptions = load_document(doc or sample_document())
    result = compile_rules(rules, exceptions,
                           parse_instant(start), parse_instant(end))
    return store.publish(c, rules=rules, exceptions=exceptions, result=result,
                         expected_version=expected_version,
                         request_id=request_id)


# ------------------------------------------------------------- basic flow

def test_publish_and_query_current(conn):
    out = publish_sample(conn, "req-v1", 0)
    assert out["version"] == 1 and not out["replayed"]
    doc = store.get_version(conn)
    assert doc["version"] == 1
    assert doc["plan_count"] == 813
    assert doc["exclusion_count"] == 2
    assert len(doc["rules"]) == 9


def test_query_historical_version(conn):
    publish_sample(conn, "req-1", 0)
    publish_sample(conn, "req-2", 1, end="2026-06-01T00:00:00Z")
    assert store.get_version(conn)["version"] == 2
    v1 = store.get_version(conn, 1)
    assert v1["version"] == 1 and v1["plan_count"] == 813
    v2 = store.get_version(conn, 2)
    assert 0 < v2["plan_count"] < 813
    with pytest.raises(store.NotFound):
        store.get_version(conn, 99)


# ------------------------------------------------------- version conflicts

def test_stale_expected_version_is_rejected(conn):
    publish_sample(conn, "req-v1", 0)
    with pytest.raises(store.VersionConflict, match="version changed"):
        publish_sample(conn, "req-v2", 0)
    assert store.get_version(conn)["version"] == 1


def test_concurrent_publish_same_base_only_one_wins(conn):
    publish_sample(conn, "req-base", 0)          # current = 1
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(i):
        c = store.connect(TEST_DSN)
        try:
            barrier.wait(timeout=10)
            res = publish_sample(c, f"req-race-{i}", 1)
            outcomes.append(("ok", res["version"]))
        except store.VersionConflict:
            outcomes.append(("conflict", None))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(o[0] for o in outcomes) == ["conflict", "ok"]
    assert store.get_version(conn)["version"] == 2
    assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 2


# ------------------------------------------------------------- idempotency

def test_retry_with_same_request_id_and_content_returns_original(conn):
    r1 = publish_sample(conn, "req-1", 0)
    # even a nonsensical expected_version is ignored on a true replay
    r2 = publish_sample(conn, "req-1", 999)
    assert r2["replayed"] and r2["version"] == r1["version"]
    assert store.get_version(conn)["version"] == r1["version"]
    assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 1


def test_same_request_id_with_different_content_is_rejected(conn):
    publish_sample(conn, "req-1", 0)
    with pytest.raises(store.RequestConflict, match="different content"):
        publish_sample(conn, "req-1", 1, end="2026-06-01T00:00:00Z")
    assert store.get_version(conn)["version"] == 1


# ------------------------------------------------- failure must not damage

def test_failed_compile_preserves_existing_version(conn):
    publish_sample(conn, "req-v1", 0)
    bad_doc = {"rules": [{
        "rule_id": "ny-err", "timezone": "America/New_York",
        "local_time": "02:30:00",
        "start_date": "2026-03-01", "end_date": "2026-03-15",
        "frequency": {"type": "daily"},
        "on_nonexistent": "error", "on_repeated": "first"}]}
    rules, exceptions = load_document(bad_doc)
    with pytest.raises(CompileError):
        compile_rules(rules, exceptions, parse_instant(WINDOW[0]),
                      parse_instant(WINDOW[1]))
    assert store.get_version(conn)["version"] == 1
    assert conn.execute("SELECT count(*) FROM versions").fetchone()[0] == 1
    assert store.verify(conn)["ok"]


def test_kill_mid_publish_leaves_no_partial_data(conn, tmp_path):
    publish_sample(conn, "req-v1", 0)
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(sample_document()))
    env = dict(os.environ, SCHEDC_DSN=TEST_DSN, SCHEDC_FAULT_AFTER_PLANS="100")
    proc = subprocess.run(
        [sys.executable, "-m", "schedc", "publish",
         "--rules", str(rules_file),
         "--start", "2026-01-01T00:00:00Z", "--end", "2026-06-01T00:00:00Z",
         "--expected-version", "1", "--request-id", "req-killed"],
        env=env, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert proc.returncode != 0
    assert "FAULT INJECTED" in proc.stderr

    # the process died after inserting 100 of ~328 plans: everything of
    # that attempt must have been rolled back
    assert store.get_version(conn)["version"] == 1
    assert conn.execute(
        "SELECT count(*) FROM versions WHERE request_id = 'req-killed'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 813
    assert store.verify(conn)["ok"]


# ------------------------------------------------------------------ verify

def test_verify_passes_on_a_clean_version(conn):
    publish_sample(conn, "req-v1", 0)
    report = store.verify(conn)
    assert report["ok"], report
    assert all(c["ok"] for c in report["checks"])


def test_verify_detects_tampered_plan(conn):
    publish_sample(conn, "req-v1", 0)
    conn.execute("UPDATE plans SET utc_time = utc_time + interval '1 hour' "
                 "WHERE plan_id = (SELECT plan_id FROM plans LIMIT 1)")
    report = store.verify(conn)
    assert not report["ok"]
    failed = {c["name"] for c in report["checks"] if not c["ok"]}
    assert {"plan_id_stable", "local_utc_roundtrip",
            "recompile_deterministic"} & failed


def test_verify_detects_missing_plan(conn):
    publish_sample(conn, "req-v1", 0)
    conn.execute("DELETE FROM plans WHERE plan_id = "
                 "(SELECT plan_id FROM plans LIMIT 1)")
    report = store.verify(conn)
    assert not report["ok"]
    failed = {c["name"] for c in report["checks"] if not c["ok"]}
    assert "counts_match" in failed
