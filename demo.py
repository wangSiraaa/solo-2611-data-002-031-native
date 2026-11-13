"""End-to-end demonstration against a freshly seeded database.

Steps:
  1. reset the database (drop schema + fixtures/seed.sql)
  2. start the HTTP server in-process
  3. run fixtures/queries.graphql  -> verify ordering / first / empty orders
  4. run fixtures/between_requests.sql (psql -f)
  5. run the same query again      -> new committed values appear
  6. negative + scaling checks

Run:  .venv/bin/python demo.py
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PG_DSN = os.environ.get("PG_DSN", "host=/tmp/pgsock dbname=taskdb")
PORT = int(os.environ.get("PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}/graphql"

PASS, FAIL = "PASS", "FAIL"
checks = 0


def psql(*args: str) -> None:
    subprocess.run(["psql", PG_DSN, "-v", "ON_ERROR_STOP=1", *args],
                   check=True, capture_output=True, text=True)


def post(payload: dict) -> dict:
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:  # 4xx still carries a GraphQL error body
        return json.loads(exc.read())


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  [{PASS if ok else FAIL}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        raise SystemExit(f"check failed: {label}")


def show(body: dict) -> None:
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    print("== 1. reset database (fixtures/seed.sql) ==")
    psql("-c", "DROP SCHEMA IF EXISTS gql_input CASCADE;")
    psql("-f", str(ROOT / "fixtures" / "seed.sql"))

    print("== 2. start server ==")
    from app.server import main as serve

    threading.Thread(target=serve, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    query = (ROOT / "fixtures" / "queries.graphql").read_text(encoding="utf-8")

    print("== 3. request #1: fixtures/queries.graphql ==")
    r1 = post({"query": query})
    show(r1)
    data = r1["data"]["customers"]
    check("ids deduped, first-occurrence order, missing ignored",
          [c["id"] for c in data] == ["1", "2", "3", "4"])
    c1, c2, c3, c4 = data
    check("same created_at tie broken by id DESC (12 before 11)",
          [o["id"] for o in c1["history"]] == ["12", "11"],
          f"createdAt both {c1['history'][0]['createdAt']}")
    check("first=1 vs first=2 are independent loads",
          [o["id"] for o in c1["recent"]] == ["12"]
          and len(c1["history"]) == 2)
    check("repeated alias with same first reuses the same load",
          c1["repeated"] == [{"id": "12"}])
    check("amount is an exact decimal string",
          c1["recent"][0]["amount"] == "12.00")
    check("customer without orders -> real empty arrays",
          c4["recent"] == [] and c4["history"] == [] and c4["repeated"] == [])
    check("customer 2 history", [o["id"] for o in c2["history"]] == ["21", "22"])
    check("customer 3 history", [o["id"] for o in c3["history"]] == ["31"])
    check("SQL count = 1 customers + 2 order batches (first in {1,2})",
          r1["extensions"]["sqlCount"] == 3,
          f"got {r1['extensions']['sqlCount']}")

    print("== 4. fixtures/between_requests.sql (committed between requests) ==")
    psql("-f", str(ROOT / "fixtures" / "between_requests.sql"))

    print("== 5. request #2: identical query ==")
    r2 = post({"query": query})
    show(r2)
    c1 = r2["data"]["customers"][0]
    check("new committed order 14 is now the most recent",
          [o["id"] for o in c1["recent"]] == ["14"]
          and [o["id"] for o in c1["history"]] == ["14", "12"]
          and c1["repeated"] == [{"id": "14"}])
    check("new amount exact", c1["recent"][0]["amount"] == "99.00")
    check("SQL count still 3", r2["extensions"]["sqlCount"] == 3)
    r2n = post({"query": '{ customers(ids: ["1"]) { id name } }'})
    check("updated customer name visible (no cross-request cache)",
          r2n["data"]["customers"][0]["name"] == "甲-更新")

    print("== 6. negative and scaling checks ==")
    r = post({"query": '{ customers(ids: ["1"]) { id orders(first: 0) { id } } }'})
    check("first=0 -> GraphQL error, not an empty array",
          "errors" in r and "between 1 and 5" in r["errors"][0]["message"])
    r = post({"query": '{ customers(ids: ["1"]) { id orders(first: 6) { id } } }'})
    check("first=6 -> GraphQL error", "errors" in r)
    r = post({"query": '{ customers(ids: ["1"]) { id orders { id } } }'})
    check("missing required first -> validation error",
          "errors" in r and "data" not in r)
    r = post({"query": "{ customers(ids: [1, 2, 2,"})
    check("syntax error -> GraphQL error", "errors" in r and "data" not in r)
    r = post({"query": '{ customers(ids: ["4", "2", "4", "9", "abc"]) { id } }'})
    check("order by first occurrence, dupes once, unknown ids ignored",
          [c["id"] for c in r["data"]["customers"]] == ["4", "2"])
    q_scale = ('query($ids: [ID!]!) { customers(ids: $ids) { id '
               "a: orders(first: 1) { id } b: orders(first: 2) { id } } }")
    n1 = post({"query": q_scale, "variables": {"ids": ["1"]}})["extensions"]["sqlCount"]
    n4 = post({"query": q_scale,
               "variables": {"ids": ["1", "2", "3", "4"]}})["extensions"]["sqlCount"]
    check("customer count 1 -> 4 keeps SQL count constant (no N+1)",
          n1 == n4 == 3, f"1 customer: {n1} SQL, 4 customers: {n4} SQL")

    print("== 7. database failure is a GraphQL error, never an empty array ==")
    psql("-c", "DROP TABLE gql_input.orders;")
    r = post({"query": '{ customers(ids: ["1"]) { id orders(first: 1) { id } } }'})
    check("failed orders load -> errors mention the failed query",
          "errors" in r and "orders batch query failed" in r["errors"][0]["message"])
    check("no fake empty orders array in data",
          r.get("data") is None or all(
              c.get("orders") != [] for c in (r["data"].get("customers") or []) if c))
    psql("-c", "DROP SCHEMA IF EXISTS gql_input CASCADE;")
    psql("-f", str(ROOT / "fixtures" / "seed.sql"))  # restore for the next run

    print(f"\nall {checks} checks passed")


if __name__ == "__main__":
    main()
