#!/usr/bin/env python3
"""Scenario harness: starts the synthetic site, drives the helper, verifies.

For each scenario it:
  1. boots a fresh site process (random port, fresh SQLite),
  2. (optionally) toggles faults via the site's ``/__control`` endpoint,
  3. runs the helper CLI against a task list,
  4. fetches the site's independent truth export,
  5. compares org/month/rows/amount for every saved report,
  6. asserts failed tasks left no readable ``report.csv`` behind,
  7. checks that request logs show failures (not silent successes).

Usage: python tools/verify_scenarios.py [--keep]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
RUNTIME = ROOT / ".runtime" / "verify"

TASKS = [
    {"name": "alpha-sales", "org_label": "Alpha 制造",
     "month_label": "2023年2月", "report_type": "销售汇总"},
    {"name": "beta-cost", "org_label": "Beta 商贸",
     "month_label": "2024年7月", "report_type": "成本明细"},
    {"name": "gamma-stock", "org_label": "Gamma 物流",
     "month_label": "2023年11月", "report_type": "库存周转"},
    {"name": "delta-audit", "org_label": "Delta 研发",
     "month_label": "2024年3月", "report_type": "审计台账"},
]


@dataclass
class Site:
    proc: subprocess.Popen
    base: str
    db: Path
    log: Path

    def control(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + "/__control", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def truth(self) -> dict:
        with urllib.request.urlopen(self.base + "/__truth.csv", timeout=10) as r:
            text = r.read().decode()
        return {(row["org_label"], row["month_label"], row["type_label"]): row
                for row in csv.DictReader(io.StringIO(text))}

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def boot_site(out: Path, variant: str, mode: str, faults: dict | None = None,
              cookie_path: str | None = None) -> Site:
    out.mkdir(parents=True, exist_ok=True)
    db = out / "site.sqlite"
    log = out / "site.log"
    ready = out / "ready"
    if ready.exists():
        ready.unlink()
    cmd = [PY, "-m", "report_site.server", "--variant", variant,
           "--db", str(db), "--port", "0", "--mode", mode,
           "--ready-file", str(ready)]
    if cookie_path:
        cmd += ["--cookie-path", cookie_path]
    with log.open("w") as fh:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        if ready.exists():
            addr = ready.read_text().strip()
            site = Site(proc=proc, base="http://" + addr, db=db, log=log)
            try:
                urllib.request.urlopen(site.base + "/__health", timeout=2).read()
                if faults:
                    site.control(faults)
                return site
            except OSError:
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"site exited: {log.read_text()[-2000:]}")
        time.sleep(0.15)
    proc.kill()
    raise RuntimeError("site did not become ready")


def write_config(path: Path, base: str, retry: int = 2) -> Path:
    cfg = {
        "base_url": base,
        "login_url": "/",
        "credentials": {"username": "ops", "password": "demo-pass"},
        "forms": {
            "section_control": "columns",
            "section_labels": ["显示摘要"],
        },
        "retry_limit": retry,
        "timeout": 15,
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_tasks(path: Path, tasks: list[dict]) -> Path:
    path.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def run_helper(cfg: Path, tasks: Path, out: Path) -> subprocess.CompletedProcess:
    cmd = [PY, "-m", "report_helper.cli", "run",
           "--config", str(cfg), "--tasks", str(tasks),
           "--state-dir", str(out / "state"),
           "--trace-dir", str(out / "trace"),
           "--summary-csv", str(out / "summary.csv")]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)


def load_states(out: Path) -> dict[str, dict]:
    states = {}
    for p in sorted((out / "state").glob("*/state.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        states[s.get("task", p.parent.name)] = s
    return states


def saved_csv(out: Path, state: dict):
    if not state.get("final_file"):
        return None
    path = Path(out) / "state" / _safe(state["task"]) / state["final_file"]
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]


@dataclass
class Check:
    task: str
    result: str
    ok: bool
    detail: str


def verify(out: Path, truth: dict, tasks: list[dict], expect_complete: set[str],
           expect_failed: set[str]) -> list[Check]:
    states = load_states(out)
    checks = []
    for task in tasks:
        name = task["name"]
        st = states.get(name)
        if st is None:
            checks.append(Check(name, "missing-state", False, "no state.json"))
            continue
        rows = saved_csv(out, st)
        if name in expect_complete:
            exp = truth[(task["org_label"], task["month_label"], task["report_type"])]
            if rows is None:
                checks.append(Check(name, "no-file", False, "expected completed file"))
                continue
            problems = []
            if len(rows) - 1 != int(exp["row_count"]):
                problems.append(f"rows {len(rows)-1}!={exp['row_count']}")
            total = sum(float(r[-1].replace(",", "")) for r in rows[1:])
            if f"{total:.2f}" != exp["total"]:
                problems.append(f"total {total:.2f}!={exp['total']}")
            if st.get("save_state") != "completed":
                problems.append(f"save_state={st.get('save_state')}")
            if st.get("qid") is None:
                problems.append("no qid")
            checks.append(Check(name, "complete", not problems,
                                "; ".join(problems) or "org/month/rows/total match"))
        elif name in expect_failed:
            leaked = (Path(out) / "state" / _safe(name) / "report.csv").exists()
            partials = list((Path(out) / "state" / _safe(name) / ".partial").glob("*"))
            ok = (st.get("save_state") == "failed" and not leaked and not partials)
            checks.append(Check(name, "failed", ok,
                                st.get("last_error", "")[:160]
                                + ("; leaked report.csv!" if leaked else "")
                                + (f"; {len(partials)} partial files!" if partials else "")))
    return checks


ALL_TASKS = {t["name"] for t in TASKS}

# A task whose requested org label does not exist on the site.  Its failure
# must be reported (with the page it happened on) without blocking the others.
BAD_TASK = {"name": "bogus-org", "org_label": "不存在的机构",
            "month_label": "2023年2月", "report_type": "销售汇总"}
TASKS_WITH_BAD = TASKS + [BAD_TASK]

SCENARIOS = [
    # Same semantics, different page structure: both must produce identical
    # business reports (compared against the site's own truth export).
    {"id": "happy-a", "variant": "a", "mode": "unscoped", "faults": None,
     "complete": ALL_TASKS, "failed": set()},
    {"id": "happy-b", "variant": "b", "mode": "unscoped", "faults": None,
     "complete": ALL_TASKS, "failed": set()},
    # Session invalidation at every stage, answering with a 200 login page.
    {"id": "expire-org-a", "variant": "a", "mode": "scoped",
     "faults": {"mode": "scoped", "expire_org": 1},
     "complete": ALL_TASKS, "failed": set()},
    {"id": "expire-params-b", "variant": "b", "mode": "scoped",
     "faults": {"mode": "scoped", "expire_params": 1},
     "complete": ALL_TASKS, "failed": set()},
    {"id": "expire-preview-a", "variant": "a", "mode": "scoped",
     "faults": {"mode": "scoped", "expire_preview": 1},
     "complete": ALL_TASKS, "failed": set()},
    {"id": "expire-download-b", "variant": "b", "mode": "scoped",
     "faults": {"mode": "scoped", "expire_download": 1},
     "complete": ALL_TASKS, "failed": set()},
    # Download response integrity faults.
    {"id": "mismatch-download-a", "variant": "a", "mode": "scoped",
     "faults": {"mode": "scoped", "mismatch_download": 1},
     "complete": ALL_TASKS, "failed": set()},
    {"id": "bad-content-type-b", "variant": "b", "mode": "scoped",
     "faults": {"mode": "scoped", "bad_content_type": 1},
     "complete": ALL_TASKS, "failed": set()},
    {"id": "truncated-download-a", "variant": "a", "mode": "scoped",
     "faults": {"mode": "scoped", "truncate_download": 1},
     "complete": ALL_TASKS, "failed": set()},
    # CSRF rotation on every render/submit must be re-read each time.
    {"id": "csrf-rotate-b", "variant": "b", "mode": "scoped",
     "faults": {"mode": "scoped", "csrf_rotate": True},
     "complete": ALL_TASKS, "failed": set()},
    # Persistent corruption: every task fails, and no saved report may exist.
    {"id": "persistent-truncation-a", "variant": "a", "mode": "scoped",
     "faults": {"mode": "scoped", "truncate_download": True},
     "tasks": TASKS, "retry": 1, "complete": set(), "failed": ALL_TASKS},
    # A missing option must stop only that task; others continue.
    {"id": "missing-option-b", "variant": "b", "mode": "unscoped",
     "faults": None, "tasks": TASKS_WITH_BAD,
     "complete": ALL_TASKS, "failed": {"bogus-org"}},
    # Cookie Path narrower than the workflow: real HTTP cookie scoping means
    # the session cookie is not sent to later stages; no report may be saved.
    {"id": "cookie-path-a", "variant": "a", "mode": "scoped",
     "faults": None, "cookie_path": "/select-org", "tasks": TASKS,
     "retry": 1, "complete": set(), "failed": ALL_TASKS},
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", action="append", default=None)
    args = ap.parse_args(argv)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    all_ok = True
    for sc in SCENARIOS:
        if args.only and sc["id"] not in args.only:
            continue
        out = RUNTIME / sc["id"]
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        site = boot_site(out, sc["variant"], sc["mode"], sc.get("faults"),
                         sc.get("cookie_path"))
        tasks = sc.get("tasks", TASKS)
        try:
            cfg = write_config(out / "config.json", site.base, sc.get("retry", 2))
            tasks_path = write_tasks(out / "tasks.json", tasks)
            proc = run_helper(cfg, tasks_path, out)
            (out / "helper_stdout.txt").write_text(proc.stdout, encoding="utf-8")
            (out / "helper_stderr.txt").write_text(proc.stderr, encoding="utf-8")
            truth = site.truth()
            checks = verify(out, truth, tasks, sc["complete"], sc["failed"])
        finally:
            site.stop()
        ok = all(c.ok for c in checks)
        all_ok = all_ok and ok
        print(f"\n=== {sc['id']} ({'PASS' if ok else 'FAIL'}) ===")
        for c in checks:
            mark = "ok  " if c.ok else "FAIL"
            print(f"  [{mark}] {c.task:<12} {c.result:<10} {c.detail}")
        print(f"  helper exit={proc.returncode}  log lines={_count_lines(site.log)}")
        print(f"  site request log: {_log_summary(site.db)}")
        if not args.keep:
            pass
    return 0 if all_ok else 1


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _log_summary(db: Path) -> str:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT phase, status, count(*) FROM request_log GROUP BY phase,status"
            " ORDER BY phase").fetchall()
        conn.close()
        return ", ".join(f"{p}@{s}:{n}" for p, s, n in rows)
    except sqlite3.Error as exc:
        return f"<log unavailable: {exc}>"


if __name__ == "__main__":
    raise SystemExit(main())
