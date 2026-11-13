#!/usr/bin/env python3
"""End-to-end concurrent demonstration for proxy.py.

Three scenarios, all using the real fixtures/upstream.py origins and the real
proxy process:

  1. isolation  - saturate the blocked ``slow`` source (past its queue limit)
                  while continuously issuing ``fast`` requests, then release
                  the gate.  Verifies from the *upstream* audit file that the
                  expired queue entries were never dispatched and that the
                  healthy source kept completing.
  2. cancel     - cancel a client while the proxy is reading a blocked
                  upstream response; verifies the slot is reclaimed exactly
                  once and a later request can use it.
  3. shutdown   - SIGTERM while requests are in flight/queued; verifies the
                  process exits within a bounded grace period and every
                  request has a terminal state.

Usage:  python3 demo.py [--scenario isolation|cancel|shutdown|all]
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
CONFIG = FIXTURES / "config.json"
RUNTIME = ROOT / ".runtime"
SLOW_PORT = 8111
FAST_PORT = 8112


# ----------------------------------------------------------------- utilities
def wait_until(pred, timeout: float, interval: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def port_open(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), 0.1)
        s.close()
        return True
    except OSError:
        return False


def read_json(path: Path):
    """Read a stats file that may be atomically replaced under us."""
    for _ in range(5):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.005)
    return None


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


class Proc:
    def __init__(self, cmd, stdout_path: Path, stderr_path: Path, cwd=ROOT):
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._out = open(stdout_path, "w")
        self._err = open(stderr_path, "w")
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.p = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=self._out, stderr=self._err)

    def stop(self, timeout: float = 2.0) -> int | None:
        if self.p.poll() is None:
            self.p.terminate()
        try:
            rc = self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            rc = self.p.wait(timeout)
        self._out.close()
        self._err.close()
        return rc

    def stderr(self) -> str:
        try:
            return self.stderr_path.read_text(errors="replace")
        except OSError:
            return ""


def start_upstream(mode: str, port: int, audit: Path, gate: Path | None, tag: str) -> Proc:
    if audit.exists():
        audit.unlink()
    cmd = [sys.executable, str(FIXTURES / "upstream.py"),
           "--port", str(port), "--mode", mode, "--audit", str(audit)]
    if mode == "blocked":
        cmd += ["--gate", str(gate)]
    proc = Proc(cmd, RUNTIME / f"{tag}.out", RUNTIME / f"{tag}.err")
    assert wait_until(lambda: port_open(port), 5.0), f"origin {mode}:{port} did not start"
    return proc


def start_proxy(port: int, tag: str, grace_ms: int = 300) -> tuple[Proc, Path, Path]:
    stats = RUNTIME / f"{tag}_stats.json"
    events = RUNTIME / f"{tag}_events.jsonl"
    for p in (stats, events):
        if p.exists():
            p.unlink()
    cmd = [sys.executable, str(ROOT / "proxy.py"), "--config", str(CONFIG),
           "--port", str(port), "--stats-file", str(stats),
           "--event-log", str(events), "--shutdown-grace-ms", str(grace_ms)]
    proc = Proc(cmd, RUNTIME / f"{tag}_proxy.out", RUNTIME / f"{tag}_proxy.err")
    assert wait_until(lambda: stats.exists() and read_json(stats) is not None, 5.0), \
        "proxy did not start"
    return proc, stats, events


def fresh_gate(tag: str) -> Path:
    gate = RUNTIME / f"gate_{tag}_{int(time.time() * 1000)}_{os.getpid()}"
    if gate.exists():
        gate.unlink()
    return gate


def get(port: int, source: str, timeout: float = 3.0) -> dict:
    res = {"source": source, "t0": time.monotonic(), "status": None,
           "outcome": None, "error": None}
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", f"/item?source={source}")
        r = conn.getresponse()
        res["status"] = r.status
        res["outcome"] = r.getheader("X-Proxy-Outcome")
        res["body"] = r.read().decode("latin1")
        conn.close()
    except Exception as exc:          # noqa: BLE001 - transport failures are data here
        res["error"] = type(exc).__name__
    res["t1"] = time.monotonic()
    return res


def outcome_of(res: dict) -> str:
    if res["error"]:
        return f"transport:{res['error']}"
    return res["outcome"] or f"http:{res['status']}"


# ---------------------------------------------------------------- verifiers
def replay_events(events: list[dict], caps: dict[str, int], global_cap: int) -> dict:
    per = {s: 0 for s in caps}
    peaks = {s: 0 for s in caps}
    global_now = 0
    global_peak = 0
    received = {}
    finished = {}
    writes = {}
    expired = set()
    errors = []
    for ev in events:
        kind = ev.get("event")
        if kind == "received":
            received[ev["rid"]] = ev["source"]
        elif kind == "finished":
            rid = ev["rid"]
            if rid in finished:
                errors.append(f"duplicate terminal for {rid}")
            finished[rid] = ev["reason"]
        elif kind == "slot_assigned":
            s = ev["source"]
            per[s] += 1
            global_now += 1
            peaks[s] = max(peaks[s], per[s])
            global_peak = max(global_peak, global_now)
            if per[s] > caps[s]:
                errors.append(f"source {s} exceeded cap: {per[s]} > {caps[s]}")
            if global_now > global_cap:
                errors.append(f"global exceeded: {global_now} > {global_cap}")
        elif kind == "slot_released":
            s = ev["source"]
            per[s] -= 1
            global_now -= 1
        elif kind == "upstream_write":
            writes.setdefault(ev["source"], set()).add(ev["rid"])
        elif kind == "queue_expired":
            expired.add(ev["rid"])
    if global_now != 0 or any(v != 0 for v in per.values()):
        errors.append(f"slot leak at end: global={global_now} per={per}")
    for rid in received:
        if rid not in finished:
            errors.append(f"request {rid} has no terminal state")
    for rid in finished:
        if rid not in received:
            errors.append(f"terminal for unknown request {rid}")
    for rid in expired:
        if any(rid in ids for ids in writes.values()):
            errors.append(f"expired queue entry {rid} was dispatched upstream")
    return {"received": received, "finished": finished, "writes": writes,
            "expired": expired, "peaks": peaks, "global_peak": global_peak,
            "errors": errors, "end_slots": {"global": global_now, **per}}


def begin_end(audit: list[dict]) -> tuple[int, int, list[dict], list[dict]]:
    begins = [e for e in audit if e.get("event") == "begin"]
    ends = [e for e in audit if e.get("event") == "end"]
    return len(begins), len(ends), begins, ends


def check_all_terminal(stats: dict) -> list[str]:
    errors = []
    for name, src in stats["sources"].items():
        total_ended = sum(src["ended"].values())
        if total_ended != src["received"]:
            errors.append(
                f"{name}: received={src['received']} != terminal={total_ended} {src['ended']}")
        if src["inflight"] != 0:
            errors.append(f"{name}: inflight leaked: {src['inflight']}")
        if src["queued"] != 0:
            errors.append(f"{name}: queue not empty: {src['queued']}")
    if stats["global_inflight"] != 0:
        errors.append(f"global inflight leaked: {stats['global_inflight']}")
    return errors


# --------------------------------------------------------------- scenarios
def scenario_isolation(pport: int = 8123) -> dict:
    print("\n=== scenario: isolation + queue full + deadline + no late dispatch ===")
    gate = fresh_gate("iso")
    audit_slow = RUNTIME / "iso_slow.jsonl"
    audit_fast = RUNTIME / "iso_fast.jsonl"
    slow = start_upstream("blocked", SLOW_PORT, audit_slow, gate, "iso_slow")
    fast = start_upstream("fast", FAST_PORT, audit_fast, None, "iso_fast")
    proxy, stats_path, events_path = start_proxy(pport, "iso")
    errors = []
    try:
        fast_results = []
        stop_fast = threading.Event()

        def fast_loop():
            while not stop_fast.is_set():
                fast_results.append(get(pport, "fast", timeout=2.0))
                time.sleep(0.01)

        fast_thread = threading.Thread(target=fast_loop, daemon=True)
        fast_thread.start()
        time.sleep(0.05)                   # let the healthy stream establish

        slow_results = []
        barrier = threading.Barrier(5)

        def slow_client():
            barrier.wait()
            slow_results.append(get(pport, "slow", timeout=3.0))

        slow_threads = [threading.Thread(target=slow_client, daemon=True)
                        for _ in range(5)]
        t_submit = time.monotonic()
        for t in slow_threads:
            t.start()

        # Wait until all five arrived, then wait for their single total budget
        # to expire while the gate is still closed.  Nothing may be dispatched
        # from the queue after this point.
        if not wait_until(
                lambda: (read_json(stats_path) or {}).get("sources", {})
                .get("slow", {}).get("received", 0) == 5, 1.0):
            errors.append("not all slow requests arrived")
        if not wait_until(
                lambda: (lambda s: sum(s.get("sources", {}).get("slow", {})
                                       .get("ended", {}).values()) >= 5)(
                    read_json(stats_path) or {}), 2.0):
            errors.append("slow requests did not all reach a terminal state "
                          "under the total budget")
        mid = read_json(stats_path)
        t_after_expiry = time.monotonic()
        print(f"  after budget: slow queued={mid['sources']['slow']['queued']} "
              f"inflight={mid['sources']['slow']['inflight']} "
              f"ended={mid['sources']['slow']['ended']}")

        t_release = time.monotonic()
        gate.write_text("released\n")      # barrier release AFTER queue expiry
        time.sleep(0.3)

        stop_fast.set()
        fast_thread.join(timeout=2)
        for t in slow_threads:
            t.join(timeout=3)

        stats = read_json(stats_path)
        events = read_jsonl(events_path)
        audit_s = read_jsonl(audit_slow)
        audit_f = read_jsonl(audit_fast)

        # --- client-visible outcomes
        slow_out = [outcome_of(r) for r in slow_results]
        n_success = slow_out.count("success")
        n_deadline = slow_out.count("deadline")
        n_full = slow_out.count("queue_full")
        if len(slow_results) != 5:
            errors.append(f"expected 5 slow results, got {len(slow_results)}")
        if (n_success, n_deadline, n_full) != (0, 4, 1):
            errors.append(f"slow outcomes unexpected: {slow_out}")
        # --- healthy source kept working during the blocked window
        fast_ok = [r for r in fast_results if outcome_of(r) == "success"]
        fast_during = [r for r in fast_ok if r["t1"] < t_release]
        fast_bad = [r for r in fast_results if outcome_of(r) != "success"]
        if len(fast_ok) < 40:
            errors.append(f"too few fast completions: {len(fast_ok)}")
        if len(fast_during) < 10:
            errors.append(f"too few fast completions during blocked window: "
                          f"{len(fast_during)}")
        if fast_bad:
            errors.append(f"fast source failures: {[outcome_of(r) for r in fast_bad]}")
        slow_lat = [r["t1"] - r["t0"] for r in fast_ok]
        if slow_lat and max(slow_lat) > 0.3:
            errors.append(f"fast latency exceeded budget: {max(slow_lat):.3f}s")
        # --- proxy accounting
        errors += check_all_terminal(stats)
        if stats["sources"]["slow"]["received"] != 5:
            errors.append(f"slow received={stats['sources']['slow']['received']}")
        if stats["sources"]["slow"]["dispatched"] != 1:
            errors.append(f"slow dispatched={stats['sources']['slow']['dispatched']} "
                          f"(queue entries must not be sent)")
        sa = replay_events(events, {"slow": 1, "fast": 1}, 2)
        errors += sa["errors"]
        # --- upstream truth: only the one in-flight slow request was sent
        sb, se, sb_ev, se_ev = begin_end(audit_s)
        if sb != 1:
            errors.append(f"slow upstream saw {sb} begins, expected 1 "
                          f"(expired queue entries must never be dispatched)")
        if stats["sources"]["slow"]["dispatched"] != sb:
            errors.append("slow dispatched metric != upstream begins")
        if sb_ev and sb_ev[0]["monotonic"] >= t_release:
            errors.append("slow upstream begin happened after gate release")
        if se_ev and se_ev[-1]["monotonic"] <= t_release:
            errors.append("upstream end before release: blocked origin not held?")
        fb, fe, fb_ev, _ = begin_end(audit_f)
        if fb != stats["sources"]["fast"]["dispatched"]:
            errors.append(f"fast upstream begins {fb} != dispatched "
                          f"{stats['sources']['fast']['dispatched']}")
        if fb == 0:
            errors.append("fast upstream never dispatched")

        return {
            "name": "isolation",
            "errors": errors,
            "slow_outcomes": slow_out,
            "fast_ok": len(fast_ok),
            "fast_during_blocked": len(fast_during),
            "gap_after_expiry_ms": round((t_release - t_after_expiry) * 1000, 1),
            "slow_upstream": {"begins": sb, "ends": se,
                              "begin_after_release": sum(
                                  1 for e in sb_ev if e["monotonic"] >= t_release),
                              "held_after_proxy_giveup_ms": round(
                                  (se_ev[-1]["monotonic"] - sb_ev[0]["monotonic"]) * 1000, 1)
                              if sb_ev and se_ev else None},
            "fast_upstream": {"begins": fb, "ends": fe},
            "peaks": sa["peaks"], "global_peak": sa["global_peak"],
            "stats": stats,
        }
    finally:
        stop_fast.set()
        proxy.stop()
        slow.stop()
        fast.stop()


def scenario_cancel(pport: int = 8124) -> dict:
    print("\n=== scenario: cancel while reading upstream ===")
    gate = fresh_gate("cancel")
    audit_slow = RUNTIME / "cancel_slow.jsonl"
    slow = start_upstream("blocked", SLOW_PORT, audit_slow, gate, "cancel_slow")
    fast = start_upstream("fast", FAST_PORT, RUNTIME / "cancel_fast.jsonl", None, "cancel_fast")
    proxy, stats_path, events_path = start_proxy(pport, "cancel")
    errors = []
    try:
        sock = socket.create_connection(("127.0.0.1", pport), timeout=2.0)
        sock.sendall(b"GET /item?source=slow HTTP/1.1\r\nHost: proxy\r\n\r\n")
        ok = wait_until(lambda: (read_json(stats_path) or {}).get("sources", {})
                        .get("slow", {}).get("inflight", 0) == 1, 1.0)
        if not ok:
            errors.append("first slow request never became in-flight")
        ok = wait_until(lambda: len([e for e in read_jsonl(audit_slow)
                                     if e.get("event") == "begin"]) == 1, 1.0)
        if not ok:
            errors.append("upstream never logged the begin of the first request")
        cancel_t = time.monotonic()
        sock.close()                        # client disappears mid-read
        ok = wait_until(lambda: (read_json(stats_path) or {}).get("sources", {})
                        .get("slow", {}).get("inflight", 0) == 0, 1.0)
        if not ok:
            errors.append("in-flight slot not released after client cancel")
        st = read_json(stats_path)
        if st["sources"]["slow"]["ended"]["client_cancelled"] != 1:
            errors.append(f"client_cancelled not recorded: "
                          f"{st['sources']['slow']['ended']}")
        if st["global_inflight"] != 0:
            errors.append(f"global slot leaked after cancel: {st['global_inflight']}")

        # A later request must be able to use the reclaimed slot.
        res2 = get(pport, "slow", timeout=3.0)
        if outcome_of(res2) != "deadline":
            errors.append(f"second slow request outcome={outcome_of(res2)}")
        ok = wait_until(lambda: (read_json(stats_path) or {}).get("sources", {})
                        .get("slow", {}).get("inflight", 0) == 0, 1.0)
        if not ok:
            errors.append("second request slot not released after its deadline")
        res_fast = get(pport, "fast", timeout=2.0)
        if outcome_of(res_fast) != "success":
            errors.append(f"fast after cancel: {outcome_of(res_fast)}")

        gate.write_text("released\n")
        time.sleep(0.15)
        stats = read_json(stats_path)
        events = read_jsonl(events_path)
        errors += check_all_terminal(stats)
        sa = replay_events(events, {"slow": 1, "fast": 1}, 2)
        errors += sa["errors"]
        audit = read_jsonl(audit_slow)
        sb, se, sb_ev, se_ev = begin_end(audit)
        if sb != 2:
            errors.append(f"slow upstream begins={sb}, expected 2 (cancelled + second)")
        if se < 1:
            errors.append("upstream never observed end after gate release")
        held_ms = None
        if len(sb_ev) >= 1 and se_ev:
            held_ms = round((se_ev[0]["monotonic"] - sb_ev[0]["monotonic"]) * 1000, 1)
        if se_ev and se_ev[0]["monotonic"] <= cancel_t:
            errors.append("cancelled request's upstream handler ended before cancel")
        return {
            "name": "cancel",
            "errors": errors,
            "cancel_cancelled": stats["sources"]["slow"]["ended"],
            "second_outcome": outcome_of(res2),
            "fast_outcome": outcome_of(res_fast),
            "upstream_begins": sb, "upstream_ends": se,
            "upstream_held_after_cancel_ms": max(
                0.0, round((se_ev[0]["monotonic"] - cancel_t) * 1000, 1)) if se_ev else None,
            "total_held_ms": held_ms,
            "peaks": sa["peaks"], "global_peak": sa["global_peak"],
        }
    finally:
        proxy.stop()
        slow.stop()
        fast.stop()


def scenario_shutdown(pport: int = 8125) -> dict:
    print("\n=== scenario: bounded shutdown with in-flight + queued requests ===")
    gate = fresh_gate("shut")
    audit_slow = RUNTIME / "shut_slow.jsonl"
    slow = start_upstream("blocked", SLOW_PORT, audit_slow, gate, "shut_slow")
    fast = start_upstream("fast", FAST_PORT, RUNTIME / "shut_fast.jsonl", None, "shut_fast")
    proxy, stats_path, events_path = start_proxy(pport, "shut", grace_ms=300)
    errors = []
    results = []
    barrier = threading.Barrier(5)

    def slow_client():
        barrier.wait()
        results.append(get(pport, "slow", timeout=5.0))

    try:
        threads = [threading.Thread(target=slow_client, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()
        ok = wait_until(
            lambda: (lambda s: s["sources"]["slow"]["received"] == 5
                     and s["sources"]["slow"]["inflight"] == 1
                     and s["sources"]["slow"]["queued"] == 3)(read_json(stats_path) or {}),
            2.0)
        if not ok:
            errors.append(f"did not reach 1 inflight + 3 queued: "
                          f"{read_json(stats_path)}")
        res_fast = get(pport, "fast", timeout=2.0)
        if outcome_of(res_fast) != "success":
            errors.append(f"fast request before shutdown failed: {outcome_of(res_fast)}")

        t0 = time.monotonic()
        rc = proxy.stop(timeout=1.5)
        exit_ms = round((time.monotonic() - t0) * 1000, 1)
        if rc != 0:
            errors.append(f"proxy exit code {rc} (stderr={proxy.stderr()[-400:]})")
        if exit_ms > 1500:
            errors.append(f"shutdown took {exit_ms}ms > 1500ms")
        if "Traceback" in proxy.stderr():
            errors.append("proxy stderr contains a traceback")

        for t in threads:
            t.join(timeout=2)
        if len(results) != 5:
            errors.append(f"expected 5 client results, got {len(results)}")

        stats = read_json(stats_path)
        events = read_jsonl(events_path)
        errors += check_all_terminal(stats)
        sa = replay_events(events, {"slow": 1, "fast": 1}, 2)
        errors += sa["errors"]
        shutdown_end = [e for e in events if e.get("event") == "shutdown_end"]
        if not shutdown_end:
            errors.append("no shutdown_end event")
        elif shutdown_end[-1].get("remaining_requests") != 0:
            errors.append(f"shutdown left requests: {shutdown_end[-1]}")
        ended = stats["sources"]["slow"]["ended"]
        if sum(ended.values()) != stats["sources"]["slow"]["received"]:
            errors.append(f"slow terminal accounting mismatch: {ended}")
        if stats["global_inflight"] != 0:
            errors.append("global inflight not zero after shutdown")
        audit_s = read_jsonl(audit_slow)
        sb, se, _, _ = begin_end(audit_s)
        if sb != 1:
            errors.append(f"slow upstream begins={sb}, expected only the 1 in-flight")
        return {
            "name": "shutdown",
            "errors": errors,
            "exit_ms": exit_ms,
            "exit_code": rc,
            "client_outcomes": [outcome_of(r) for r in results],
            "slow_ended": ended,
            "fast_outcome": outcome_of(res_fast),
            "upstream_begins": sb,
            "global_inflight_final": stats["global_inflight"],
            "peaks": sa["peaks"], "global_peak": sa["global_peak"],
        }
    finally:
        proxy.stop()
        slow.stop()
        fast.stop()


# -------------------------------------------------------------------- main
def format_report(r: dict) -> str:
    errors = r["errors"]
    lines = []
    if r["name"] == "isolation":
        lines.append(f"  slow client outcomes (5): {r['slow_outcomes']}")
        lines.append(f"  fast completions={r['fast_ok']} "
                     f"(during blocked window={r['fast_during_blocked']})")
        lines.append(f"  slow upstream: {r['slow_upstream']}  "
                     f"fast upstream: {r['fast_upstream']}")
        lines.append(f"  peak inflight per source={r['peaks']} "
                     f"global_peak={r['global_peak']}")
    elif r["name"] == "cancel":
        lines.append(f"  slow ended={r['cancel_cancelled']}")
        lines.append(f"  second slow={r['second_outcome']} fast={r['fast_outcome']}")
        lines.append(f"  upstream begins/ends={r['upstream_begins']}/{r['upstream_ends']} "
                     f"handler held after cancel={r['upstream_held_after_cancel_ms']}ms")
        lines.append(f"  peak inflight per source={r['peaks']} "
                     f"global_peak={r['global_peak']}")
    elif r["name"] == "shutdown":
        lines.append(f"  exit code={r['exit_code']} in {r['exit_ms']}ms")
        lines.append(f"  clients: {r['client_outcomes']}")
        lines.append(f"  slow ended={r['slow_ended']} fast={r['fast_outcome']}")
        lines.append(f"  slow upstream begins={r['upstream_begins']} "
                     f"global_inflight_final={r['global_inflight_final']}")
        lines.append(f"  peak inflight per source={r['peaks']} "
                     f"global_peak={r['global_peak']}")
    lines.append("  RESULT: " + ("PASS" if not errors else "FAIL"))
    for e in errors:
        lines.append(f"    - {e}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["isolation", "cancel", "shutdown", "all"],
                    default="all")
    args = ap.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    scenarios = {"isolation": scenario_isolation, "cancel": scenario_cancel,
                 "shutdown": scenario_shutdown}
    chosen = list(scenarios) if args.scenario == "all" else [args.scenario]
    failures = 0
    summary = {}
    for name in chosen:
        result = scenarios[name]()
        print(format_report(result))
        summary[name] = {"pass": not result["errors"],
                         "errors": result["errors"]}
        failures += bool(result["errors"])
    (RUNTIME / "demo_summary.json").write_text(json.dumps(summary, indent=2))
    print("\noverall:", "PASS" if failures == 0 else f"FAIL ({failures} scenario(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
