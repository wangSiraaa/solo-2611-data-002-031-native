#!/usr/bin/env python3.11
"""Runnable demo of the blind double-annotation service.

Starts a private server process against a fresh SQLite database (or targets
``--base`` if you already have one running), then walks through three
identities:

1. Blind phase: ann_a reads every read endpoint before submitting and must not
   see ann_b's labels or even ann_b's progress; the arbiter sees nothing.
2. Agreement: T01 is submitted identically by both annotators -> one agreed
   final record, exported.
3. Dispute: T02 differs -> stays out of the export until the arbiter resolves
   it; the arbitration is bound to both submission ids.
4. Concurrency: both annotators submit one item at the same instant, and the
   arbiter fires duplicate/conflicting arbitration requests at the same time.
5. Restart: the server is restarted on the same database and the submission
   and arbitration chain is verified.

Usage:
    python3.11 tools/demo.py            # manages its own server
    python3.11 tools/demo.py --base http://127.0.0.1:8765   # no restart check
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = {"ann_a": "fixture-a", "ann_b": "fixture-b", "arbiter": "fixture-arbiter"}


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, who, method, path, body=None):
        headers = {"Authorization": f"Bearer {TOKENS[who]}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def items(self, who):
        return self.call(who, "GET", "/api/items")

    def item(self, who, item_id):
        return self.call(who, "GET", f"/api/items/{item_id}")

    def submit(self, who, item_id, labels):
        return self.call(who, "POST", f"/api/items/{item_id}/submission", {"labels": labels})

    def export(self, who):
        return self.call(who, "GET", "/api/export")

    def queue(self, who):
        return self.call(who, "GET", "/api/arbitration/queue")

    def arbitrate(self, item_id, labels, expected=None):
        body = {"labels": labels}
        if expected is not None:
            body["expected_submissions"] = expected
        return self.call("arbiter", "POST", f"/api/items/{item_id}/arbitration", body)


CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


def assert_no_labels(payload, where: str) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for label in ("product", "service", "delivery"):
        check(label not in text, f"{where} reveals no label id ({label})")


def assert_blind_entry(entry: dict, where: str) -> None:
    for key in ("peer_submission", "submissions", "final", "peer_submitted"):
        check(key not in entry, f"{where} reveals no '{key}' field")


def start_server(db: Path, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "annotation_service",
            "--db",
            str(db),
            "--port",
            str(port),
            "--log-level",
            "WARNING",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"server exited during startup:\n{out}")
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1):
                return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("server did not become healthy")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_flows(c: Client) -> None:
    print("\n[1] Blind phase: ann_b submits first; ann_a must see nothing")
    status, _ = c.submit("ann_b", "T03", ["service", "product"])
    check(status == 201, "ann_b submits T03 while ann_a has not")

    for _, view_name in (("ann_a", "list"), ("ann_a", "detail")):
        status, payload = c.items("ann_a") if view_name == "list" else c.item("ann_a", "T03")
        check(status == 200, f"ann_a {view_name} succeeds")
        assert_no_labels(payload, f"ann_a {view_name} before own submission")
        entry = next(
            (i for i in payload["items"] if i["item"]["id"] == "T03"),
            None,
        ) if view_name == "list" else payload
        assert_blind_entry(entry, f"ann_a {view_name} entry")
        check(entry.get("status") == "not_submitted", f"ann_a {view_name} shows no peer progress")

    status, payload = c.export("ann_a")
    check(status == 200 and payload["item_count"] == 0, "ann_a export is empty before finishing")
    status, payload = c.items("arbiter")
    check(status == 200 and payload["items"] == [], "arbiter list hides items with one submission")
    status, payload = c.queue("arbiter")
    check(status == 200 and payload["items"] == [], "arbiter queue hides incomplete items")
    status, err = c.item("arbiter", "T03")
    check(status == 409 and err["error"]["code"] == "not_ready", "arbiter detail refuses incomplete item")
    check("service" not in json.dumps(err), "arbiter refusal leaks no labels")
    status, err = c.arbitrate("T03", ["service"])
    check(status == 409 and err["error"]["code"] == "not_ready", "arbitration refuses incomplete item")

    status, _ = c.submit("ann_a", "T03", ["service", "product"])
    check(status == 201, "ann_a submits T03 after the blind checks")
    status, view = c.item("ann_a", "T03")
    check(view["peer_submission"]["labels"] == ["product", "service"], "peer labels appear only after both submitted")
    check(view["final"]["source"] == "agreement", "T03 becomes an agreed final")

    print("\n[2] Agreement: T01 submitted identically, order-insensitive")
    check(c.submit("ann_a", "T01", ["delivery", "product"])[0] == 201, "ann_a submits T01")
    status, view = c.submit("ann_b", "T01", ["product", "delivery"])
    check(status == 201 and view["final"]["status"] == "agreed", "T01 final is agreement")
    check(
        view["final"]["binding"]["submissions"][0]["submission_id"]
        < view["final"]["binding"]["submissions"][1]["submission_id"],
        "agreement final is bound to both ordered submission ids",
    )
    status, replay = c.submit("ann_a", "T01", ["product", "delivery"])
    check(status == 200 and replay.get("replayed") is True, "identical resubmission replays the stored submission")
    status, err = c.submit("ann_a", "T01", ["service"])
    check(status == 409 and err["error"]["code"] == "submission_immutable", "different resubmission is refused")

    print("\n[3] Dispute + arbitration: T02 differs")
    check(c.submit("ann_a", "T02", ["product"])[0] == 201, "ann_a submits T02")
    check(c.submit("ann_b", "T02", ["service"])[0] == 201, "ann_b submits T02")
    status, view = c.item("ann_a", "T02")
    check(view["status"] == "disputed" and view["final"] is None, "T02 is disputed with no final")
    status, listing = c.items("arbiter")
    check("T02" in [i["item"]["id"] for i in listing["items"]], "arbiter sees T02 once both submitted")
    status, queue = c.queue("arbiter")
    check("T02" in [i["item"]["id"] for i in queue["items"]], "T02 is in the arbitration queue")
    check(c.export("ann_a")[1]["item_count"] == 2, "export still has only the two agreed items")

    status, err = c.arbitrate("T02", ["product"], expected={"ann_a": 999, "ann_b": 999})
    check(status == 409 and err["error"]["code"] == "stale_binding", "arbitration refuses a stale binding")
    status, result = c.arbitrate("T02", ["product", "service"])
    check(status == 201 and result["final"]["source"] == "arbitration", "arbiter resolves T02")
    binding = result["final"]["binding"]["submissions"]
    check(
        [(b["author_id"], b["submission_id"]) for b in binding]
        == [("ann_a", 5), ("ann_b", 6)],
        "arbitration binds the actual submission ids and authors",
    )
    status, replay = c.arbitrate("T02", ["service", "product"])
    check(status == 200 and replay.get("replayed") is True, "identical arbitration replays")
    status, err = c.arbitrate("T02", ["delivery"])
    check(status == 409 and err["error"]["code"] == "arbitration_immutable", "conflicting arbitration is refused")

    status, exported = c.export("arbiter")
    by_id = {i["item_id"]: i for i in exported["items"]}
    check(set(by_id) == {"T01", "T02", "T03"}, "export contains exactly the finalised items")
    check(by_id["T02"]["final"]["labels"] == ["product", "service"], "export carries the arbitrated labels")
    check("T04" not in by_id, "unsubmitted items never appear (T04)")

    print("\n[4] Concurrency: same-instant submissions and duplicate arbitrations")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: c.submit(*args),
                [("ann_a", "T04", ["delivery"]), ("ann_b", "T04", ["delivery"])],
            )
        )
    check(sorted(r[0] for r in results) == [201, 201], "both concurrent submissions accepted")
    status, view = c.item("ann_a", "T04")
    check(view["final"]["status"] == "agreed", "exactly one agreed final for concurrent submits")

    for item_id, labels in (("T05", ["product"]), ("T06", ["service"])):
        c.submit("ann_a", item_id, labels)
        c.submit("ann_b", item_id, ["delivery"])
    status, _ = c.arbitrate("T05", ["product"])
    check(status == 201, "arbitrate T05")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: c.arbitrate("T06", ["product"] if i % 2 == 0 else ["service"]),
                range(8),
            )
        )
    codes = sorted(r[0] for r in results)
    check(codes.count(201) == 1, "exactly one concurrent arbitration wins")
    check(all(code in (200, 201, 409) for code in codes), "losers get replay or conflict, never a second final")
    status, exported = c.export("arbiter")
    finals = [i for i in exported["items"] if i["item_id"] == "T06"]
    check(len(finals) == 1, "export contains exactly one final for T06")
    winners = {json.dumps(r[1]["final"]["labels"]) for r in results if r[0] == 201}
    losers = {json.dumps(r[1]["final"]["labels"]) for r in results if r[0] == 200}
    check(losers <= winners, "replays agree with the winning arbitration")


def verify_export_consistency(c: Client) -> dict:
    status, exported = c.export("arbiter")
    check(status == 200, "export after restart succeeds")
    check(exported["only_final"] is True, "export is final-only")
    check(all(i["final"]["status"] in ("agreed", "arbitrated") for i in exported["items"]), "every exported row is agreed or arbitrated")
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="existing server base URL; disables server management")
    parser.add_argument("--db", default=str(ROOT / ".runtime" / "demo.db"))
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    managed = args.base is None
    proc = None
    if managed:
        db = Path(args.db)
        db.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        port = args.port or free_port()
        proc = start_server(db, port)
        base = f"http://127.0.0.1:{port}"
    else:
        base = args.base
        port = 0

    try:
        client = Client(base)
        run_flows(client)
        before_export = verify_export_consistency(client)

        if managed:
            print("\n[5] Restart: stop the process, start it again on the same database")
            proc.terminate()
            proc.wait(timeout=10)
            proc = start_server(Path(args.db), port)
            after_export = verify_export_consistency(client)
            check(
                before_export["items"] == after_export["items"],
                "final results are identical after restart",
            )
            status, replay = client.submit("ann_a", "T01", ["product", "delivery"])
            check(status == 200 and replay.get("replayed") is True, "restart preserves submission replays")
            status, err = client.submit("ann_a", "T01", ["service"])
            check(status == 409, "restart preserves submission immutability")
            status, err = client.arbitrate("T02", ["delivery"])
            check(status == 409, "restart preserves arbitration immutability")
            print(f"\nDemo database kept at {args.db}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"\nAll checks passed ({CHECKS} assertions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
