"""`snapx generate` — reset schema (apply migrations) and load synthetic data.

The dataset always contains edge cases required by the tests:
  * order_id 1: amount 0 with NO payments (must not be omitted by export)
  * order_id 2: amount split across 5 payment rows (payments spanning rows)
Every order satisfies: sum(payments.amount_cents) == orders.amount_cents.
"""

import argparse
import os
import random

from . import MIGRATIONS_DIR, db


def _split_amount(amount: int, k: int) -> list:
    """Split `amount` into k integer parts summing exactly to `amount`."""
    base, rem = divmod(amount, k)
    return [base + 1 if i < rem else base for i in range(k)]


def apply_migrations(conn) -> None:
    path = os.path.join(MIGRATIONS_DIR, "0001_init.sql")
    with open(path, "r", encoding="utf-8") as f:
        script = f.read()
    # The migration contains only plain DDL statements separated by ';'.
    for stmt in script.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


def build_dataset(n_orders: int, seed: int):
    rng = random.Random(seed)
    orders, payments = [], []
    pid = 1
    for oid in range(1, n_orders + 1):
        customer_id = rng.randint(1, 50)
        if oid == 1:
            amount, k = 0, 0            # edge case: zero amount, no payments
        elif oid == 2:
            amount, k = 100_000, 5      # edge case: payment split over many rows
        else:
            amount = 0 if rng.random() < 0.15 else rng.randrange(100, 900_000)
            k = 0 if amount == 0 else rng.randint(1, 4)
        orders.append((oid, customer_id, amount))
        for part in _split_amount(amount, k) if k else []:
            payments.append((pid, oid, part))
            pid += 1
    return orders, payments


def cmd_generate(args: argparse.Namespace) -> int:
    conn = db.connect()
    with conn:
        conn.execute("DROP TABLE IF EXISTS payments, orders, dataset_epoch CASCADE")
        apply_migrations(conn)
        orders, payments = build_dataset(args.orders, args.seed)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO orders(order_id, customer_id, amount_cents) VALUES (%s,%s,%s)",
                orders,
            )
            cur.executemany(
                "INSERT INTO payments(payment_id, order_id, amount_cents) VALUES (%s,%s,%s)",
                payments,
            )
            cur.execute("INSERT INTO dataset_epoch(id, epoch) VALUES (1, 1)")
    conn.close()
    total = sum(o[2] for o in orders)
    print(f"generate: orders={len(orders)} payments={len(payments)} "
          f"total_amount_cents={total} epoch=1 (seed={args.seed})")
    print(f"  edge cases: order 1 -> amount 0, no payments; order 2 -> 5 payment rows")
    return 0
