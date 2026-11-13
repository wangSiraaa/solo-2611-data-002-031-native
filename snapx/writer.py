"""`snapx writer` — synthetic concurrent writer.

Each iteration performs ONE business transaction that:
  * inserts a new order with its payments, OR
  * modifies an existing order's amount and rewrites its payments, OR
  * deletes an order together with its payments,
and always bumps dataset_epoch in the same transaction.
Invariant after every commit: for each order, sum(payments) == order.amount_cents.

Gate mode (--gate-dir): the writer builds its transaction, writes
<gate>/ready.json, then waits for the file <gate>/commit.allow before
COMMIT. After commit it writes <gate>/committed.json. The export
coordinator uses this to prove readers on a snapshot are unaffected by a
writer committing mid-export.
"""

import argparse
import json
import os
import random
import time

from . import db, util


def _split_amount(amount: int, k: int) -> list:
    base, rem = divmod(amount, k)
    return [base + 1 if i < rem else base for i in range(k)]


def _new_amount(rng: random.Random) -> int:
    return 0 if rng.random() < 0.15 else rng.randrange(100, 900_000)


def business_txn(conn, rng: random.Random) -> dict:
    """Execute one business transaction body (no COMMIT here)."""
    cur = conn.execute("SELECT epoch FROM dataset_epoch WHERE id = 1")
    epoch_before = int(cur.fetchone()[0])

    order_ids = [r[0] for r in conn.execute("SELECT order_id FROM orders ORDER BY order_id")]
    op = rng.choice(["insert", "update", "delete"]) if order_ids else "insert"

    if op == "insert":
        oid = conn.execute("SELECT COALESCE(MAX(order_id),0)+1 FROM orders").fetchone()[0]
        amount = _new_amount(rng)
        k = 0 if amount == 0 else rng.randint(1, 4)
        parts = _split_amount(amount, k) if k else []
        conn.execute(
            "INSERT INTO orders(order_id, customer_id, amount_cents) VALUES (%s,%s,%s)",
            (oid, rng.randint(1, 50), amount),
        )
    elif op == "update":
        oid = rng.choice(order_ids)
        amount = _new_amount(rng)
        k = 0 if amount == 0 else rng.randint(1, 4)
        parts = _split_amount(amount, k) if k else []
        conn.execute("UPDATE orders SET amount_cents=%s WHERE order_id=%s", (amount, oid))
        conn.execute("DELETE FROM payments WHERE order_id=%s", (oid,))
    else:  # delete: whole business (order + its payments)
        oid = rng.choice(order_ids)
        conn.execute("DELETE FROM payments WHERE order_id=%s", (oid,))
        conn.execute("DELETE FROM orders WHERE order_id=%s", (oid,))
        amount, parts = None, []

    if op != "delete" and parts:
        next_pid = conn.execute("SELECT COALESCE(MAX(payment_id),0) FROM payments").fetchone()[0]
        with conn.cursor() as cur2:
            cur2.executemany(
                "INSERT INTO payments(payment_id, order_id, amount_cents) VALUES (%s,%s,%s)",
                [(next_pid + i + 1, oid, p) for i, p in enumerate(parts)],
            )

    epoch_after = int(conn.execute(
        "UPDATE dataset_epoch SET epoch = epoch + 1, updated_at = now() WHERE id = 1 "
        "RETURNING epoch"
    ).fetchone()[0])

    return {
        "op": op,
        "order_id": oid,
        "amount_cents": amount,
        "payment_parts": parts,
        "epoch_before": epoch_before,
        "epoch_after": epoch_after,
    }


def run_one(args, rng) -> dict:
    """Run one gated (or immediate) business transaction. Returns commit info."""
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        desc = business_txn(conn, rng)
        if args.gate_dir:
            os.makedirs(args.gate_dir, exist_ok=True)
            util.write_json_atomic(os.path.join(args.gate_dir, "ready.json"), {
                "pid": os.getpid(), "ts": time.time(), "description": desc,
            })
            allow = os.path.join(args.gate_dir, "commit.allow")
            if not util.wait_for(allow, args.gate_timeout):
                conn.execute("ROLLBACK")
                raise TimeoutError(f"gate {allow} not released within {args.gate_timeout}s; rolled back")
        conn.execute("COMMIT")
        info = {
            "pid": os.getpid(),
            "commit_ts": time.time(),
            "commit_iso": util.iso(time.time()),
            **desc,
        }
        if args.gate_dir:
            util.write_json_atomic(os.path.join(args.gate_dir, "committed.json"), info)
        return info
    finally:
        conn.close()


def cmd_writer(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    if args.once:
        info = run_one(args, rng)
        print(f"writer: committed epoch={info['epoch_after']} op={info['op']} "
              f"order={info['order_id']} amount={info['amount_cents']} "
              f"parts={len(info['payment_parts'])}")
        return 0
    # loop mode
    for i in range(args.count):
        info = run_one(args, rng)
        print(f"writer[{i}]: epoch={info['epoch_after']} op={info['op']} "
              f"order={info['order_id']} amount={info['amount_cents']}", flush=True)
        time.sleep(args.interval)
    return 0
