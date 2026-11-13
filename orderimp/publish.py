"""Reference resolution and atomic version publishing.

After a *fully successful* parse, orders are resolved against the
customers of their own batch (forward references work because
resolution happens only once the whole document is staged).  The three
datasets (customers / orders / lines) plus diagnostics (rejects +
manifest) are then written as one immutable version directory and the
``current`` symlink is swapped atomically.  If anything fails before
the swap, the previously published version keeps serving readers.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter

from .staging import Staging

JSON_KW = dict(ensure_ascii=False, separators=(",", ":"))


def _dump(obj) -> bytes:
    return (json.dumps(obj, **JSON_KW) + "\n").encode("utf-8")


def resolve(stg: Staging) -> None:
    """Quarantine orders whose customer is missing or rejected.

    Only orders still marked valid are touched; each newly quarantined
    order is copied into the rejects table exactly once.
    """
    db = stg.db
    db.execute(
        """UPDATE orders SET valid = 0, reason = 'customer_rejected'
           WHERE valid = 1 AND EXISTS (
               SELECT 1 FROM customers c
               WHERE c.batch_id = orders.batch_id
                 AND c.customer_id = orders.customer_id
                 AND c.valid = 0)"""
    )
    db.execute(
        """UPDATE orders SET valid = 0, reason = 'unknown_customer'
           WHERE valid = 1 AND NOT EXISTS (
               SELECT 1 FROM customers c
               WHERE c.batch_id = orders.batch_id
                 AND c.customer_id = orders.customer_id
                 AND c.valid = 1)"""
    )
    db.execute(
        """INSERT INTO rejects(kind, batch_id, record_id, seq, reason, detail)
           SELECT 'order', batch_id, order_id, seq, reason, NULL
           FROM orders WHERE valid = 0 AND reported = 0"""
    )
    db.execute("UPDATE orders SET reported = 1 WHERE reported = 0")
    db.commit()


def publish(stg: Staging, data_dir: str, *, source_path: str, source_sha256: str,
            source_bytes: int, chunk_size: int, limits, stats) -> dict:
    """Write a new version directory and swap ``current`` onto it."""
    versions_dir = os.path.join(data_dir, "versions")
    os.makedirs(versions_dir, exist_ok=True)
    existing = [
        d for d in os.listdir(versions_dir)
        if d.startswith("v") and d[1:].isdigit()
    ]
    number = max((int(d[1:]) for d in existing), default=0) + 1
    vname = f"v{number:06d}"
    tmp = os.path.join(versions_dir, f".tmp-{vname}-{os.getpid()}")
    final = os.path.join(versions_dir, vname)
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    counts: dict[str, int] = {}
    total_cents = 0

    # -- customers.jsonl ---------------------------------------------------
    n = 0
    with open(os.path.join(tmp, "customers.jsonl"), "wb", buffering=1 << 20) as fh:
        for batch_id, cid, name, seq in stg.db.execute(
            "SELECT batch_id, customer_id, name, seq"
            " FROM customers WHERE valid = 1 ORDER BY key"
        ):
            fh.write(_dump({
                "batch_id": batch_id, "customer_id": cid,
                "name": name, "seq": seq,
            }))
            n += 1
    counts["customers"] = n

    # -- orders.jsonl + lines.jsonl (single streaming pass) -----------------
    n_orders = 0
    n_lines = 0
    with open(os.path.join(tmp, "orders.jsonl"), "wb", buffering=1 << 20) as fo, \
         open(os.path.join(tmp, "lines.jsonl"), "wb", buffering=1 << 20) as fl:
        cur_key = None
        cur = None
        cur_total = 0
        cur_nlines = 0

        def flush_order():
            nonlocal n_orders, total_cents
            fo.write(_dump({
                "batch_id": cur[0], "order_id": cur[1],
                "customer_id": cur[2], "seq": cur[3],
                "line_count": cur_nlines, "total_cents": cur_total,
            }))
            n_orders += 1
            total_cents += cur_total

        rows = stg.db.execute(
            """SELECT o.key, o.batch_id, o.order_id, o.customer_id, o.seq,
                      l.line_no, l.sku, l.qty, l.unit_cents
               FROM orders o JOIN lines l ON l.order_key = o.key
               WHERE o.valid = 1
               ORDER BY o.key, l.line_no"""
        )
        for key, bid, oid, cust, seq, line_no, sku, qty_s, unit_s in rows:
            if key != cur_key:
                if cur_key is not None:
                    flush_order()
                cur_key, cur, cur_total, cur_nlines = key, (bid, oid, cust, seq), 0, 0
            qty = int(qty_s)
            unit = int(unit_s)
            amount = qty * unit  # exact Python integer arithmetic
            fl.write(_dump({
                "batch_id": bid, "order_id": oid, "line_no": line_no,
                "sku": sku, "qty": qty, "unit_cents": unit,
                "amount_cents": amount,
            }))
            cur_total += amount
            cur_nlines += 1
            n_lines += 1
        if cur_key is not None:
            flush_order()
    counts["orders"] = n_orders
    counts["lines"] = n_lines

    # -- rejects.jsonl --------------------------------------------------------
    n = 0
    by_reason: Counter[str] = Counter()
    with open(os.path.join(tmp, "rejects.jsonl"), "wb", buffering=1 << 20) as fh:
        for kind, bid, rid, seq, reason, detail in stg.db.execute(
            "SELECT kind, batch_id, record_id, seq, reason, detail"
            " FROM rejects ORDER BY seq, key"
        ):
            obj = {
                "kind": kind, "batch_id": bid, "record_id": rid,
                "seq": seq, "reason": reason,
            }
            if detail:
                obj["detail"] = detail
            fh.write(_dump(obj))
            by_reason[reason] += 1
            n += 1
    counts["rejects"] = n
    counts["batches"] = stats.batches

    # -- manifest.json ---------------------------------------------------------
    manifest = {
        "version": vname,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_file": os.path.abspath(source_path),
        "source_sha256": source_sha256,
        "source_bytes": source_bytes,
        "chunk_size": chunk_size,
        "limits": {
            "max_depth": limits.max_depth,
            "max_record_bytes": limits.max_record_bytes,
        },
        "counts": counts,
        "total_cents": total_cents,
        "rejects_by_reason": dict(sorted(by_reason.items())),
        "diagnostics": {
            "customers_seen": stats.customers_seen,
            "orders_seen": stats.orders_seen,
            "extension_subtrees_ignored": stats.extension_subtrees,
            "unknown_business_subtrees_ignored": stats.unknown_business_subtrees,
            "stray_lines_ignored": stats.stray_lines,
        },
    }
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # -- atomic swap ------------------------------------------------------------
    os.rename(tmp, final)
    link_tmp = os.path.join(data_dir, f".current-{os.getpid()}")
    if os.path.lexists(link_tmp):
        os.remove(link_tmp)
    os.symlink(os.path.join("versions", vname), link_tmp)
    os.replace(link_tmp, os.path.join(data_dir, "current"))
    return manifest
