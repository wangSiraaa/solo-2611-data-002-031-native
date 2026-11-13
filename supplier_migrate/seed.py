"""Synthetic demo data (no real business data)."""

from __future__ import annotations

import psycopg

DEFAULT_SUPPLIERS = [
    ("ENT-A", "SUP-A", "Alpha Metals"),
    ("ENT-B", "SUP-B", "Beta Components"),
    ("ENT-C", "SUP-C", "Gamma Logistics"),
    ("ENT-D", "SUP-D", "Delta Coatings"),
    ("ENT-E", "SUP-E", "Epsilon Tools"),
    ("ENT-F", "SUP-F", "Zeta Plastics"),
    ("ENT-G", "SUP-G", "Eta Packaging"),
    ("ENT-H", "SUP-H", "Theta Services"),
]

# codes deliberately left unused so a chain may terminate in free space.
FREE_CODES = ["FREE-1", "FREE-2", "FREE-3"]


def reset(conn: psycopg.Connection) -> None:
    conn.execute(
        "TRUNCATE migration_operations, purchase_orders, contacts, suppliers "
        "RESTART IDENTITY CASCADE"
    )


def seed(conn: psycopg.Connection, suppliers=DEFAULT_SUPPLIERS,
         free_codes=FREE_CODES, orders_per_supplier: int = 3,
         contacts_per_supplier: int = 2) -> dict:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO suppliers(entity_id, code, name) VALUES (%s, %s, %s)",
            [tuple(s) for s in suppliers],
        )
        for idx, (_, code, _name) in enumerate(suppliers):
            cur.executemany(
                "INSERT INTO purchase_orders(order_ref, supplier_code, customer, "
                "amount) VALUES (%s, %s, %s, %s)",
                [
                    (f"PO-{code}-{n}", code, f"Customer-{idx}-{n}",
                     f"{(idx + 1) * 100 + n}.00")
                    for n in range(1, orders_per_supplier + 1)
                ],
            )
            cur.executemany(
                "INSERT INTO contacts(contact_ref, supplier_code, full_name, email) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (f"CT-{code}-{n}", code, f"Contact {code} #{n}",
                     f"{code.lower()}-{n}@example.test")
                    for n in range(1, contacts_per_supplier + 1)
                ],
            )
    return {
        "suppliers": len(suppliers),
        "unused_codes": list(free_codes),
        "purchase_orders": len(suppliers) * orders_per_supplier,
        "contacts": len(suppliers) * contacts_per_supplier,
    }


def seed_demo(conn: psycopg.Connection, reset: bool = False) -> dict:
    if reset:
        reset_db(conn)
    summary = seed(conn)
    summary["status"] = "seeded"
    summary["reset"] = reset
    return summary


def reset_db(conn: psycopg.Connection) -> None:
    reset(conn)
