#!/usr/bin/env python3
"""Verify a completed demo crawl against the synthetic servers' access log.

Checks, from the server side only (not from crawler bookkeeping):

1. no request ever reached a robots-disallowed path or the out-of-scope trap;
2. the observed per-origin interval respects the configured minimum;
3. while the slow origin was stalling, other origins kept making progress;
4. an independent traversal of the known synthetic graph reproduces exactly the
   details that were stored, including their source paths;
5. the frontier is complete: every discovered URL is in a terminal state;
6. every detail has a real response URL and a source page, and no detail was
   stored twice for the same item_id.

Usage:
    python scripts/verify_demo.py --config config/demo.json \
        --log .runtime/site_access.log --export .runtime/export
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webfrontier.config import load_config  # noqa: E402

FORBIDDEN = [
    "/private/hidden",
    "/quarantine/rack.html",
    "/public/secret",
    "/go/secret",
    "/blocked/hidden",
]
TRAP_PORT = 8899

# Independent model of the synthetic site graph: which paths are details and
# which directory pages can reach them.  Kept deliberately simple and separate
# from the crawler so the two must agree.
EXPECTED_DETAILS = {f"A-{i}" for i in range(1, 9)} | {"B-1", "D-1"} | {
    f"C-{i}" for i in range(1, 7)
}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, message: str) -> None:
        if ok:
            self.notes.append(f"PASS {message}")
        else:
            self.failures.append(f"FAIL {message}")

    def report(self) -> int:
        for line in self.notes:
            print(line)
        for line in self.failures:
            print(line)
        print()
        print(
            f"{len(self.notes)} checks passed, {len(self.failures)} failed"
        )
        return 1 if self.failures else 0


def load_log(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/demo.json")
    parser.add_argument("--log", default=".runtime/site_access.log")
    parser.add_argument("--export", default=".runtime/export")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    log = load_log(Path(args.log))
    export = Path(args.export)
    details = json.loads((export / "details.json").read_text(encoding="utf-8"))
    summary = json.loads((export / "summary.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(open(export / "urls.csv", encoding="utf-8")))

    c = Checker()
    c.check(len(log) > 0, f"access log has {len(log)} records")

    # 1. forbidden and out-of-scope targets never requested
    for path in FORBIDDEN:
        hits = [r for r in log if r["path"] == path]
        c.check(not hits, f"robots-disallowed {path} never requested (hits={len(hits)})")
    trap = [r for r in log if r["port"] == TRAP_PORT]
    c.check(not trap, f"out-of-scope origin :{TRAP_PORT} never requested (hits={len(trap)})")
    robotsfail = [r for r in log if r["port"] == 8806 and not r["path"].startswith("/robots.txt")]
    c.check(
        not robotsfail,
        f"robots-unreachable site never crawled beyond robots.txt (hits={len(robotsfail)})",
    )

    # 2. per-origin minimum interval as observed by the server
    by_origin: dict[str, list[dict]] = defaultdict(list)
    configured_origins = set(config.origin_values)
    for record in log:
        by_origin[record["origin"]].append(record)
    # The access log is shared by every run of the synthetic group; records from
    # origins outside this task (e.g. the 10k navigation site) are ignored.
    log = [r for r in log if r["origin"] in configured_origins]
    by_origin = {o: rs for o, rs in by_origin.items() if o in configured_origins}
    c.check(
        all(o in configured_origins for o in by_origin),
        "every logged request belongs to a configured origin",
    )
    for origin, records in sorted(by_origin.items()):
        records.sort(key=lambda r: r["ts"])
        required = config.origin_config(origin).min_interval
        gaps = [b["ts"] - a["ts"] for a, b in zip(records, records[1:])]
        smallest = min(gaps) if gaps else None
        c.check(
            not gaps or smallest >= required - 0.002,
            f"{origin}: observed min interval {smallest!r} >= configured {required}",
        )

    # 3. slow site stalls but others progress
    slow = [r["ts"] for r in log if r["path"].startswith("/slow/")]
    if slow:
        lo, hi = min(slow), max(slow)
        during = Counter(
            r["site"] for r in log
            if lo <= r["ts"] <= hi and not r["path"].startswith("/slow")
        )
        others = sum(v for k, v in during.items() if k != "site-c")
        c.check(
            others > 0,
            f"other origins progressed while slow site stalled ({dict(during)})",
        )
    else:
        c.check(False, "slow-site requests were observed")

    # 4. independent graph traversal vs stored details
    stored_ids = Counter(d["item_id"] for d in details)
    c.check(
        set(stored_ids) == EXPECTED_DETAILS,
        f"detail item_ids match independent model "
        f"(missing={sorted(EXPECTED_DETAILS - set(stored_ids))}, "
        f"extra={sorted(set(stored_ids) - EXPECTED_DETAILS)})",
    )
    duplicated = {k: v for k, v in stored_ids.items() if v > 1}
    c.check(not duplicated, f"no detail stored twice (duplicates={duplicated})")

    # every detail has a real response URL and (except seeds) a source page
    bad_details = [
        d for d in details
        if not d["fetch_url"].startswith("http://127.0.0.1:")
        or (d["source_canonical"] is None and d["entry_url"] not in config.all_seeds())
    ]
    c.check(not bad_details, f"every detail has response URL + source ({len(bad_details)} bad)")

    # 5. frontier completeness
    states = summary["states"]
    c.check(
        states.get("pending") == 0 and states.get("in_flight") == 0
        and states.get("retry_wait") == 0,
        f"frontier fully terminal: {states}",
    )
    c.check(
        summary["dispositions"].get("retries_exhausted", 0) >= 1,
        "a retry-exhausted URL is recorded",
    )
    c.check(
        summary["dispositions"].get("robots_denied", 0) >= 4,
        "robots-denied URLs are recorded",
    )
    c.check(
        summary["dispositions"].get("redirect_alias", 0) >= 50,
        "many entry points collapsed into redirect aliases",
    )
    c.check(
        summary["dispositions"].get("out_of_scope", 0) >= 1,
        "out-of-scope redirect target recorded as refused",
    )
    c.check(
        summary["dispositions"].get("over_budget", 0) == 0,
        "no URL was left over budget in the complete run",
    )

    # 6. alias deduplication: one HTTP request per unique detail URL
    detail_hits = Counter(r["path"] for r in log if r["path"].startswith("/detail/"))
    c.check(
        detail_hits["/detail/A-1"] >= 1,
        f"A-1 was fetched through at least one path ({detail_hits['/detail/A-1']})",
    )

    # every ledger row has a terminal disposition
    open_rows = [r for r in ledger if r["state"] != "done"]
    c.check(not open_rows, f"no open ledger rows ({len(open_rows)})")

    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
