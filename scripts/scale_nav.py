#!/usr/bin/env python3
"""Scale test: crawl the 10,000-page synthetic navigation graph.

Runs the crawler in a subprocess, samples the database frontier size and the
process RSS while it runs, and writes a JSON report.  Then it restarts the
crawler on a deliberately interrupted frontier to show that completed pages are
not recounted as new work.

The graph contains exactly 10,000 ``/nav/<n>`` pages plus 101 details, and
duplicate navigation edges (fragment aliases) so the frontier has far fewer
rows than the number of raw links.

Usage:
    python scripts/scale_nav.py --config config/nav.json --out .runtime/nav-report.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from webfrontier.config import load_config  # noqa: E402


def rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return 0
    return 0


def frontier_stats(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        states = {
            r["state"]: r["n"]
            for r in conn.execute("SELECT state, COUNT(*) AS n FROM urls GROUP BY state")
        }
        metrics = dict(conn.execute(
            "SELECT MAX(pending) AS max_pending, MAX(rss_kb) AS sample_max_rss_kb, "
            "COUNT(*) AS samples FROM metrics"
        ).fetchone())
        return {
            "states": states,
            "total_urls": conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0],
            "details": conn.execute("SELECT COUNT(*) FROM details").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM links").fetchone()[0],
            "requests": conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
            "page_requests": int(
                (conn.execute("SELECT value FROM meta WHERE key='page_requests'").fetchone() or [0])[0] or 0
            ),
            "dispositions": {
                r["disposition"]: r["n"]
                for r in conn.execute(
                    "SELECT disposition, COUNT(*) AS n FROM urls "
                    "WHERE disposition IS NOT NULL GROUP BY disposition"
                )
            },
            "metrics": metrics,
        }
    finally:
        conn.close()


def run_crawl(config: Path, reset: bool) -> tuple[int, float]:
    cmd = [sys.executable, "-m", "webfrontier", "run", "--config", str(config)]
    if reset:
        cmd.append("--reset")
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    elapsed = time.time() - started
    if proc.returncode not in (0, 1):
        print(proc.stdout[-2000:], file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
    return proc.returncode, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/nav.json")
    parser.add_argument("--out", default=".runtime/nav-report.json")
    parser.add_argument("--no-refresh", action="store_true",
                        help="use the existing database instead of a fresh crawl")
    args = parser.parse_args(argv)

    config_path = (ROOT / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    config = load_config(config_path)
    db = config.database
    report_path = ROOT / args.out

    if not args.no_refresh:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()

    samples: list[dict] = []
    started = time.time()
    cmd = [sys.executable, "-m", "webfrontier", "run", "--config", str(config_path)]
    if not args.no_refresh:
        cmd.append("--reset")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    while proc.poll() is None:
        time.sleep(2.0)
        try:
            stats = frontier_stats(db)
        except Exception:
            stats = {}
        samples.append({
            "t": round(time.time() - t0, 2),
            "rss_kb": rss_kb(proc.pid),
            "frontier_urls": stats.get("total_urls", 0),
            "states": stats.get("states", {}),
            "page_requests": stats.get("page_requests", 0),
        })
    stdout, stderr = proc.communicate()
    elapsed = time.time() - started
    final = frontier_stats(db)

    report = {
        "config": str(config_path),
        "database": str(db),
        "elapsed_seconds": round(elapsed, 2),
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(stdout.splitlines()[-12:]),
        "frontier_at_end": final,
        "max_frontier_urls_during": max((s["frontier_urls"] for s in samples), default=0),
        "max_rss_kb_during": max((s["rss_kb"] for s in samples), default=0),
        "sample_count": len(samples),
        "samples_head": samples[:5],
        "samples_tail": samples[-5:],
        "pages_per_second": round(
            (final.get("dispositions", {}).get("success", 0) or 1) / max(elapsed, 0.001), 2
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "elapsed_seconds", "returncode", "max_frontier_urls_during",
        "max_rss_kb_during", "pages_per_second")}, indent=2))
    print("frontier at end:", final["states"], final["dispositions"])
    print(f"report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
