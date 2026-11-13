"""Local supplier-code migration CLI.

Commands: init-db, plan, apply, add-order, verify, export-receipt, seed-demo.
All output is JSON on stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg

from . import db
from .mapping import MappingRejected, load_mapping


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _connect(args) -> psycopg.Connection:
    return db.connect(args.dsn)


def cmd_init_db(args) -> int:
    db.init_db(args.dsn)
    print(_dump({"status": "initialized", "schema": db.SCHEMA_FILE}))
    return 0


def _load(args, existing_codes):
    return load_mapping(args.mapping, existing_codes)


def cmd_plan(args) -> int:
    try:
        with db.open_plan_connection(args.dsn) as conn:
            codes = db.existing_codes(conn)
            mapping = _load(args, codes)
            result = db.plan(conn, mapping)
            print(_dump(result))
        return 0
    except MappingRejected as exc:
        print(_dump(exc.as_dict("plan")))
        return 1


def cmd_apply(args) -> int:
    if not args.operation_id:
        print(_dump({"status": "error", "code": "missing_operation_id",
                     "message": "--operation-id is required"}), file=sys.stdout)
        return 2
    try:
        with _connect(args) as conn:
            # Structural parse; the authoritative precondition check happens
            # again inside the locked apply transaction.
            mapping = load_mapping(args.mapping, None)
            receipt = db.apply(conn, mapping, args.operation_id,
                               fault_at=args.fault_at)
            print(_dump(receipt))
        return 0
    except (MappingRejected, db.ApplyRejected, db.VerificationFailed,
            db.InjectedFault) as exc:
        _print_failure(exc, "apply")
        return 1


def cmd_add_order(args) -> int:
    try:
        with _connect(args) as conn:
            result = db.add_order(conn, args.supplier_code,
                                  args.expected_entity_id,
                                  order_ref=args.order_ref,
                                  customer=args.customer,
                                  amount=args.amount)
            print(_dump(result))
        return 0
    except db.ApplyRejected as exc:
        print(_dump({"status": "rejected", "command": "add-order",
                     "code": exc.code, "message": exc.message,
                     "details": exc.extra}))
        return 1


def cmd_verify(args) -> int:
    with _connect(args) as conn:
        receipt = db.get_receipt(conn, args.operation_id)
        if receipt is None:
            print(_dump({"status": "error", "code": "unknown_operation",
                         "operation_id": args.operation_id}))
            return 1
        report = _verify_committed(conn, receipt)
        print(_dump(report))
        return 0 if report["status"] == "ok" else 1


def _verify_committed(conn: psycopg.Connection, receipt: dict) -> dict:
    """Re-check the live database against a committed operation's receipt.

    Invariants, all keyed by the immutable entity_id:
      * the entity still exists and carries exactly its post-migration code;
      * every child row recorded for that entity in the receipt still resolves
        through its *current* code to that same entity_id (catches rows moved
        or rebound out of band);
      * no foreign key is dangling anywhere in the two child tables.
    """
    mismatches = []
    entities = []
    checked_rows = {"purchase_orders": 0, "contacts": 0}
    bindings = receipt.get("entity_bindings")

    if bindings:
        for ent in bindings:
            checked_rows["purchase_orders"] += len(ent.get("purchase_orders", []))
            checked_rows["contacts"] += len(ent.get("contacts", []))
            for table, ref_key, key in (
                    ("purchase_orders", "purchase_orders", "order_ref"),
                    ("contacts", "contacts", "contact_ref")):
                refs = ent.get(ref_key, [])
                if not refs:
                    continue
                rows = conn.execute(
                    f"""
                    SELECT t.{key} AS ref, s.entity_id
                      FROM {table} t
                      LEFT JOIN suppliers s ON s.code = t.supplier_code
                     WHERE t.{key} = ANY(%s)
                    """,
                    (refs,),
                ).fetchall()
                by_ref = {r["ref"]: r for r in rows}
                for ref in refs:
                    row = by_ref.get(ref)
                    if row is None:
                        mismatches.append({
                            "table": table, "ref": ref,
                            "entity_id": ent["entity_id"],
                            "reason": "row_missing"})
                    elif row["entity_id"] != ent["entity_id"]:
                        mismatches.append({
                            "table": table, "ref": ref,
                            "entity_id": ent["entity_id"],
                            "reason": "row_no_longer_belongs_to_entity",
                            "actual_entity_id": row["entity_id"]})

    for ent in receipt.get("affected_entities", []):
        row = conn.execute(
            "SELECT entity_id, code FROM suppliers WHERE entity_id = %s",
            (ent["entity_id"],),
        ).fetchone()
        if row is None:
            mismatches.append({"entity_id": ent["entity_id"],
                               "reason": "entity_missing"})
            continue
        if row["code"] != ent["new_code"]:
            mismatches.append({"entity_id": ent["entity_id"],
                               "reason": "wrong_code",
                               "expected": ent["new_code"],
                               "actual": row["code"]})
            continue
        entities.append({"entity_id": ent["entity_id"],
                         "old_code": ent.get("old_code"),
                         "new_code": ent["new_code"]})
        # every row currently carrying this entity's code must resolve back
        # to this entity (guards against a foreign row being pointed here).
        for table in ("purchase_orders", "contacts"):
            bad = conn.execute(
                f"""
                SELECT count(*) AS n
                  FROM {table} t
                  LEFT JOIN suppliers s ON s.code = t.supplier_code
                 WHERE t.supplier_code = %s
                   AND (s.entity_id IS NULL OR s.entity_id <> %s)
                """,
                (ent["new_code"], ent["entity_id"]),
            ).fetchone()["n"]
            if bad:
                mismatches.append({"entity_id": ent["entity_id"],
                                   "table": table,
                                   "reason": "rows_resolve_to_other_entity",
                                   "count": bad})

    dangling = {}
    for table in ("purchase_orders", "contacts"):
        n = conn.execute(
            f"""
            SELECT count(*) AS n FROM {table} t
             LEFT JOIN suppliers s ON s.code = t.supplier_code
             WHERE s.code IS NULL
            """
        ).fetchone()["n"]
        dangling[table] = n
        if n:
            mismatches.append({"table": table, "reason": "dangling_fk", "count": n})

    return {
        "operation_id": receipt.get("operation_id"),
        "mapping_hash": receipt.get("mapping_hash"),
        "status": "ok" if not mismatches else "failed",
        "entities_checked": len(entities),
        "rows_checked": checked_rows,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "dangling_foreign_keys": dangling,
        "verified_at": db._now(),
    }


def cmd_export_receipt(args) -> int:
    with _connect(args) as conn:
        receipt = db.get_receipt(conn, args.operation_id)
        if receipt is None:
            print(_dump({"status": "error", "code": "unknown_operation"}))
            return 1
        report = _verify_committed(conn, receipt)
        bundle = {
            "exported_at": db._now(),
            "receipt": receipt,
            "verification_report": report,
        }
        text = _dump(bundle)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
    return 0 if report["status"] == "ok" else 1


def cmd_seed_demo(args) -> int:
    from .seed import seed_demo
    with _connect(args) as conn:
        summary = seed_demo(conn, reset=args.reset)
        conn.commit()
        print(_dump(summary))
    return 0


def _print_failure(exc, command: str) -> None:
    if isinstance(exc, MappingRejected):
        payload = exc.as_dict(command)
    elif isinstance(exc, db.ApplyRejected):
        payload = {"command": command, "status": "rejected", "code": exc.code,
                   "message": exc.message, **exc.extra}
    elif isinstance(exc, db.VerificationFailed):
        payload = {"command": command, "status": "failed",
                   "code": "verification_failed",
                   "message": str(exc), "mismatches": exc.mismatches}
    elif isinstance(exc, db.InjectedFault):
        payload = {"command": command, "status": "fault_injected",
                   "code": "injected_fault", "stage": exc.stage,
                   "committed": exc.committed,
                   "note": ("commit already succeeded; state is durable and a "
                            "retry with the same operation-id will replay"
                            if exc.committed else
                            "transaction rolled back; database unchanged")}
    else:  # pragma: no cover - defensive
        payload = {"command": command, "status": "error",
                   "code": "internal_error", "message": repr(exc)}
    print(_dump(payload))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supplier-migrate",
        description="Batch supplier code migration with exact, ordered semantics.",
    )
    parser.add_argument("--dsn", default=db.DEFAULT_DSN,
                        help="PostgreSQL DSN (default: %(default)s)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="create schema and tables")
    p_init.set_defaults(func=cmd_init_db)

    p_plan = sub.add_parser("plan", help="read-only preview of a mapping")
    p_plan.add_argument("--mapping", required=True, help="CSV with old_code,new_code")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="atomically apply a mapping")
    p_apply.add_argument("--mapping", required=True)
    p_apply.add_argument("--operation-id", required=True)
    p_apply.add_argument("--fault-at", choices=sorted(db.FAULT_POINTS),
                         help="test-only fault injection point")
    p_apply.set_defaults(func=cmd_apply)

    p_add = sub.add_parser(
        "add-order",
        help="insert an order only if the code still resolves to expected entity",
    )
    p_add.add_argument("--supplier-code", required=True)
    p_add.add_argument("--expected-entity-id", required=True)
    p_add.add_argument("--order-ref")
    p_add.add_argument("--customer", default="")
    p_add.add_argument("--amount", default="0")
    p_add.set_defaults(func=cmd_add_order)

    p_verify = sub.add_parser("verify", help="re-verify a committed operation")
    p_verify.add_argument("--operation-id", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_export = sub.add_parser("export-receipt",
                              help="export receipt + verification report")
    p_export.add_argument("--operation-id", required=True)
    p_export.add_argument("--output")
    p_export.set_defaults(func=cmd_export_receipt)

    p_seed = sub.add_parser("seed-demo", help="load synthetic demo data")
    p_seed.add_argument("--reset", action="store_true",
                        help="truncate business tables first")
    p_seed.set_defaults(func=cmd_seed_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except psycopg.Error as exc:
        print(_dump({"status": "error", "code": "database_error",
                     "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
