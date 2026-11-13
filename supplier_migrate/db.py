"""Database access for the supplier code migration.

Concurrency contract
--------------------
`apply` and `add-order` both take the same transaction-scoped advisory lock as
their first act.  They are therefore fully serialised: whichever commits first
is visible to the other, so an add-order can never slip an old-code reference
in between the supplier rewrite and the child-table rewrite.  The lock is
released automatically at COMMIT/ROLLBACK; no lock or constraint is disabled
outside the transaction.

Foreign keys on ``purchase_orders`` / ``contacts`` are DEFERRABLE.  Apply runs
``SET CONSTRAINTS ALL DEFERRED`` inside its transaction so the two-phase code
rewrite is legal; they are checked again at COMMIT.  They are never dropped,
disabled or left deferred beyond the transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .mapping import Mapping, MappingRejected, analyze

DEFAULT_DSN = os.environ.get(
    "SUPPLIER_DB_DSN",
    "dbname=supplier_migrate host=/tmp/supplier-pg-sock",
)

# Arbitrary constant key shared by every command in this application.
LOCK_KEY = 0x534D4947  # "SMIG"

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "migrations", "001_schema.sql")

FAULT_POINTS = {"after_suppliers", "after_child_update", "after_commit"}


class InjectedFault(RuntimeError):
    def __init__(self, stage: str, committed: bool = False):
        self.stage = stage
        self.committed = committed
        super().__init__(f"injected fault at {stage}")


class ApplyRejected(Exception):
    def __init__(self, code: str, message: str, **extra):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(f"{code}: {message}")


class VerificationFailed(RuntimeError):
    def __init__(self, mismatches: list[dict]):
        self.mismatches = mismatches
        super().__init__(f"post-migration verification failed ({len(mismatches)} mismatch(es))")


def connect(dsn: str = DEFAULT_DSN, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def init_db(dsn: str = DEFAULT_DSN) -> None:
    with open(SCHEMA_FILE, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def open_plan_connection(dsn: str = DEFAULT_DSN) -> psycopg.Connection:
    """Connection pinned to a read-only REPEATABLE READ snapshot.

    These connection attributes only apply to transactions started after they
    are set, so they must be assigned before any query runs.  The snapshot is
    materialised by the first query inside ``plan``.
    """

    conn = connect(dsn)
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def _lock(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def existing_codes(conn: psycopg.Connection) -> list[str]:
    return [r["code"] for r in conn.execute(
        "SELECT code FROM suppliers ORDER BY code COLLATE \"C\"")]


def supplier_rows(conn: psycopg.Connection, codes: list[str],
                  for_update: bool = False) -> list[dict]:
    if not codes:
        return []
    lock = " FOR UPDATE" if for_update else ""
    return list(conn.execute(
        f"""
        SELECT entity_id, code, name
          FROM suppliers
         WHERE code = ANY(%s)
         ORDER BY entity_id{lock}
        """,
        (codes,),
    ))


def row_counts_by_code(conn: psycopg.Connection, codes: list[str]) -> dict:
    counts = {"purchase_orders": {}, "contacts": {}}
    if not codes:
        return counts
    for table in ("purchase_orders", "contacts"):
        for row in conn.execute(
            f"SELECT supplier_code, count(*) AS n FROM {table} "
            "WHERE supplier_code = ANY(%s) GROUP BY supplier_code",
            (codes,),
        ):
            counts[table][row["supplier_code"]] = row["n"]
    return counts


def _entity_plan(mapping: Mapping, rows: list[dict], counts: dict) -> list[dict]:
    out = []
    by_code = {r["code"]: r for r in rows}
    for entry in mapping.entries:
        row = by_code.get(entry.old_code)
        if row is None:
            # source_missing was already reported; keep the entry visible.
            continue
        out.append({
            "entity_id": row["entity_id"],
            "name": row["name"],
            "old_code": entry.old_code,
            "new_code": entry.new_code,
            "changed": entry.changed,
            "purchase_orders": counts["purchase_orders"].get(entry.old_code, 0),
            "contacts": counts["contacts"].get(entry.old_code, 0),
        })
    return out


# --------------------------------------------------------------------------
# plan (read-only)
# --------------------------------------------------------------------------

def plan(conn: psycopg.Connection, mapping: Mapping) -> dict:
    """Read-only preview against a stable snapshot.  Never locks rows.

    The caller must configure the connection as read-only + REPEATABLE READ
    before the first query (see ``open_plan_connection``), so the whole
    preview sees one consistent snapshot.
    """

    codes = existing_codes(conn)
    errors = analyze(mapping.entries, codes)
    if errors:
        raise MappingRejected(errors)

    rows = supplier_rows(conn, sorted(mapping.old_codes))
    counts = row_counts_by_code(conn, sorted(mapping.old_codes))
    entities = _entity_plan(mapping, rows, counts)
    totals = {
        "purchase_orders": sum(e["purchase_orders"] for e in entities if e["changed"]),
        "contacts": sum(e["contacts"] for e in entities if e["changed"]),
    }
    return {
        "status": "plan",
        "read_only": True,
        "mapping_hash": mapping.mapping_hash,
        "mapping": mapping.normalized,
        "entry_counts": {
            "total": len(mapping.entries),
            "changed": len(mapping.changed_entries),
            "identity": len(mapping.identity_entries),
        },
        "affected_entities": entities,
        "affected_rows": totals,
        "table_totals": {
            "purchase_orders": sum(counts["purchase_orders"].values()),
            "contacts": sum(counts["contacts"].values()),
        },
        "errors": [],
    }


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

_PLACEHOLDER_BASE = "__supplier_migrate__"


def _make_placeholders(operation_id: str, entities: list[dict],
                       current_codes: set[str],
                       final_codes: set[str]) -> dict[str, str]:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:16]
    out: dict[str, str] = {}
    # Never generate a placeholder that equals any current or target code,
    # so phase 2 can never collide with a still-held placeholder.
    used = set(current_codes) | set(final_codes)
    for idx, ent in enumerate(entities):
        n = idx
        while True:
            candidate = f"{_PLACEHOLDER_BASE}{digest}__{n}__"
            if candidate not in used:
                used.add(candidate)
                out[ent["entity_id"]] = candidate
                break
            n += 1
    return out


def _snapshot_children(conn: psycopg.Connection, old_codes: list[str]) -> dict:
    snap = {"purchase_orders": [], "contacts": []}
    if not old_codes:
        return snap
    for table, key in (("purchase_orders", "order_ref"), ("contacts", "contact_ref")):
        snap[table] = [
            {"ref": r["ref"], "code": r["supplier_code"]}
            for r in conn.execute(
                f"SELECT {key} AS ref, supplier_code FROM {table} "
                "WHERE supplier_code = ANY(%s)",
                (old_codes,),
            )
        ]
    return snap


def _verify(conn: psycopg.Connection, mapping: Mapping, entity_by_old: dict,
            snapshot: dict) -> dict:
    old_to_new = mapping.old_to_new()
    mismatches: list[dict] = []
    checked = {}
    digest_input: list[list] = []

    for table, key in (("purchase_orders", "order_ref"), ("contacts", "contact_ref")):
        rows = snapshot[table]
        if not rows:
            checked[table] = 0
            continue
        refs = [r["ref"] for r in rows]
        current = {}
        if refs:
            for row in conn.execute(
                f"""
                SELECT t.{key} AS ref, t.supplier_code, s.entity_id
                  FROM {table} t
                  LEFT JOIN suppliers s ON s.code = t.supplier_code
                 WHERE t.{key} = ANY(%s)
                """,
                (refs,),
            ):
                current[row["ref"]] = row

        for item in rows:
            origin = entity_by_old.get(item["code"])
            expected_code = old_to_new.get(item["code"], item["code"])
            row = current.get(item["ref"])
            if row is None:
                mismatches.append({
                    "table": table, "ref": item["ref"], "reason": "missing_row",
                    "original_code": item["code"],
                })
                continue
            if row["entity_id"] is None:
                mismatches.append({
                    "table": table, "ref": item["ref"], "reason": "dangling_code",
                    "current_code": row["supplier_code"],
                })
                continue
            if row["supplier_code"] != expected_code:
                mismatches.append({
                    "table": table, "ref": item["ref"], "reason": "wrong_code",
                    "original_code": item["code"],
                    "expected_code": expected_code,
                    "current_code": row["supplier_code"],
                    "original_entity_id": origin["entity_id"] if origin else None,
                    "current_entity_id": row["entity_id"],
                })
            elif origin is not None and row["entity_id"] != origin["entity_id"]:
                mismatches.append({
                    "table": table, "ref": item["ref"], "reason": "wrong_entity",
                    "original_code": item["code"],
                    "original_entity_id": origin["entity_id"],
                    "current_entity_id": row["entity_id"],
                    "current_code": row["supplier_code"],
                })
            digest_input.append([table, item["ref"], row["supplier_code"],
                                 row["entity_id"]])
        checked[table] = len(rows)

    by_entity = []
    for ent in entity_by_old.values():
        by_entity.append({
            "entity_id": ent["entity_id"],
            "old_code": ent["old_code"],
            "new_code": old_to_new.get(ent["old_code"], ent["old_code"]),
        })
    by_entity.sort(key=lambda x: x["entity_id"])
    digest = hashlib.sha256(
        json.dumps(sorted(digest_input), ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "status": "ok" if not mismatches else "failed",
        "checked": checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
        "entities": by_entity,
        "digest": digest,
    }


def _apply_core(conn: psycopg.Connection, mapping: Mapping, operation_id: str,
                fault_at: str | None,
                fault: Callable[[str], None]) -> tuple[dict, str]:
    """Run inside an already locked transaction.  Returns (receipt, outcome).

    outcome is either ``committed`` or ``replayed``.  The caller owns COMMIT.
    """

    # 1. idempotency key check
    existing_op = conn.execute(
        "SELECT operation_id, mapping_hash, receipt FROM migration_operations "
        "WHERE operation_id = %s",
        (operation_id,),
    ).fetchone()
    if existing_op is not None:
        if existing_op["mapping_hash"] != mapping.mapping_hash:
            raise ApplyRejected(
                "operation_id_conflict",
                f"operation_id {operation_id!r} was already committed with a "
                "different mapping",
                committed_mapping_hash=existing_op["mapping_hash"],
            )
        receipt = dict(existing_op["receipt"])
        receipt["replayed"] = True
        receipt["status"] = "committed"
        return receipt, "replayed"

    # 2. revalidate against *current* committed state (never trust the preview)
    codes = existing_codes(conn)
    errors = analyze(mapping.entries, codes)
    if errors:
        raise MappingRejected(errors)

    current_codes = set(codes)
    # Lock the affected supplier rows (sorted by entity_id) for the duration of
    # the transaction: defence in depth against writers that do not take the
    # advisory lock.  All old codes are fetched because identity rows still
    # participate in verification.
    rows = supplier_rows(conn, sorted(mapping.old_codes), for_update=True)
    entity_by_old = {
        r["code"]: {"entity_id": r["entity_id"], "old_code": r["code"],
                    "name": r["name"]}
        for r in rows
    }
    changed_entities = [
        entity_by_old[e.old_code] for e in mapping.changed_entries
        if e.old_code in entity_by_old
    ]
    # deterministic ordering for placeholders
    changed_entities.sort(key=lambda x: x["entity_id"])

    counts_before = row_counts_by_code(conn, sorted(mapping.old_codes))
    snapshot = _snapshot_children(conn, sorted(mapping.old_codes))
    started_at = _now()

    # 3. supplier rewrite through transaction-local placeholders
    conn.execute("SET CONSTRAINTS ALL DEFERRED")

    old_to_new = mapping.old_to_new()
    final_codes = {
        e["entity_id"]: old_to_new[e["old_code"]] for e in changed_entities
    }
    placeholders = _make_placeholders(operation_id, changed_entities, current_codes,
                                      set(final_codes.values()))
    if changed_entities:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE suppliers SET code = %s WHERE entity_id = %s",
                [(placeholders[e["entity_id"]], e["entity_id"])
                 for e in changed_entities],
            )
            cur.executemany(
                "UPDATE suppliers SET code = %s WHERE entity_id = %s",
                [(final_codes[e["entity_id"]], e["entity_id"])
                 for e in changed_entities],
            )

    fault("after_suppliers")

    # 4. single-statement child rewrite keyed by the pre-operation code.
    #    A JOIN against the temp map guarantees each old code is applied once
    #    with no possibility of chained second-order replacement.
    conn.execute(
        """
        CREATE TEMP TABLE _mig_map (
            old_code text COLLATE "C" PRIMARY KEY,
            new_code text COLLATE "C" NOT NULL
        ) ON COMMIT DROP
        """
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO _mig_map(old_code, new_code) VALUES (%s, %s)",
            [(e.old_code, e.new_code) for e in mapping.changed_entries],
        )

    updated_orders = conn.execute(
        """
        UPDATE purchase_orders po
           SET supplier_code = m.new_code
          FROM _mig_map m
         WHERE po.supplier_code = m.old_code
        RETURNING po.order_ref
        """
    ).fetchall()

    fault("after_child_update")

    updated_contacts = conn.execute(
        """
        UPDATE contacts c
           SET supplier_code = m.new_code
          FROM _mig_map m
         WHERE c.supplier_code = m.old_code
        RETURNING c.contact_ref
        """
    ).fetchall()

    # 5. verify every pre-existing child row still resolves to its original
    #    immutable entity_id before we allow the commit.
    verification = _verify(conn, mapping, entity_by_old, snapshot)
    if verification["mismatch_count"]:
        raise VerificationFailed(verification["mismatches"])

    committed_at = _now()
    table_counts = {
        "purchase_orders": len(updated_orders),
        "contacts": len(updated_contacts),
    }
    entities = _entity_plan(mapping, rows, counts_before)
    # Persist the exact per-row ownership this operation established so a
    # later `verify` can detect rows that were moved or rebound out of band.
    bindings = []
    for ent in entities:
        refs = {"purchase_orders": [], "contacts": []}
        for item in snapshot["purchase_orders"]:
            if item["code"] == ent["old_code"]:
                refs["purchase_orders"].append(item["ref"])
        for item in snapshot["contacts"]:
            if item["code"] == ent["old_code"]:
                refs["contacts"].append(item["ref"])
        bindings.append({
            "entity_id": ent["entity_id"],
            "old_code": ent["old_code"],
            "new_code": ent["new_code"],
            "purchase_orders": sorted(refs["purchase_orders"]),
            "contacts": sorted(refs["contacts"]),
        })

    receipt = {
        "status": "committed",
        "operation_id": operation_id,
        "mapping_hash": mapping.mapping_hash,
        "mapping": mapping.normalized,
        "started_at": started_at,
        "committed_at": committed_at,
        "entry_counts": {
            "total": len(mapping.entries),
            "changed": len(mapping.changed_entries),
            "identity": len(mapping.identity_entries),
        },
        "affected_entities": entities,
        "entity_bindings": bindings,
        "affected_rows": table_counts,
        "table_totals": {
            "purchase_orders": sum(counts_before["purchase_orders"].values()),
            "contacts": sum(counts_before["contacts"].values()),
        },
        "verification": verification,
    }

    conn.execute(
        """
        INSERT INTO migration_operations
               (operation_id, mapping_hash, normalized_mapping, receipt, status)
        VALUES (%s, %s, %s, %s, 'committed')
        """,
        (operation_id, mapping.mapping_hash, Jsonb(mapping.normalized),
         Jsonb(receipt)),
    )
    return receipt, "committed"


def apply(conn: psycopg.Connection, mapping: Mapping, operation_id: str,
          fault_at: str | None = None) -> dict:
    if fault_at is not None and fault_at not in FAULT_POINTS:
        raise ValueError(f"unknown fault point {fault_at!r}")

    def fault(stage: str) -> None:
        if fault_at == stage:
            # after_commit intentionally means "commit succeeded, then die
            # before returning the receipt"; the retry must replay it.
            raise InjectedFault(stage, committed=(stage == "after_commit"))

    try:
        _lock(conn)
        receipt, outcome = _apply_core(conn, mapping, operation_id, fault_at, fault)
        conn.commit()
        fault("after_commit")
        receipt["replayed"] = outcome == "replayed"
        return receipt
    except Exception:
        conn.rollback()
        raise


def add_order(conn: psycopg.Connection, supplier_code: str,
              expected_entity_id: str, *, order_ref: str | None = None,
              customer: str = "", amount: str = "0") -> dict:
    """Insert an order only if ``supplier_code`` still resolves to the expected
    entity.  Takes the shared advisory lock so it is serialised against apply.

    Because the supplier lookup and the INSERT happen in the same transaction,
    and apply rewrites suppliers + child rows under the same lock, a concurrent
    call can only observe either the fully pre-apply state or the fully
    post-apply state.  Any code/entity disagreement is an explicit rejection.
    """

    if expected_entity_id == "":
        raise ApplyRejected(
            "missing_expected_entity_id",
            "expected_entity_id must be supplied explicitly",
        )

    try:
        _lock(conn)
        # FOR SHARE + the code predicate makes the lookup itself race-free even
        # against a writer that does not take our advisory lock: under READ
        # COMMITTED PostgreSQL re-checks the predicate on the newest row
        # version, so a just-changed code cannot be silently mis-bound.
        row = conn.execute(
            """
            SELECT s.entity_id, s.code, s.name
              FROM suppliers s
             WHERE s.code = %s
             FOR SHARE
            """,
            (supplier_code,),
        ).fetchone()
        if row is None:
            raise ApplyRejected(
                "unknown_supplier_code",
                f"supplier_code {supplier_code!r} does not exist",
            )
        if row["entity_id"] != expected_entity_id:
            raise ApplyRejected(
                "stale_entity_reference",
                f"supplier_code {supplier_code!r} currently belongs to "
                f"entity_id {row['entity_id']!r}, not expected "
                f"{expected_entity_id!r}",
                current_entity_id=row["entity_id"],
            )
        if order_ref is None:
            order_ref = "auto-" + hashlib.sha256(
                (supplier_code + expected_entity_id + _now()).encode()
            ).hexdigest()[:16]
        inserted = conn.execute(
            """
            INSERT INTO purchase_orders
                   (order_ref, supplier_code, customer, amount)
            VALUES (%s, %s, %s, %s)
            RETURNING id, order_ref, supplier_code, customer, amount, created_at
            """,
            (order_ref, supplier_code, customer, amount),
        ).fetchone()
        conn.commit()
        return {
            "status": "created",
            "order": {
                "id": inserted["id"],
                "order_ref": inserted["order_ref"],
                "supplier_code": inserted["supplier_code"],
                "customer": inserted["customer"],
                "amount": str(inserted["amount"]),
            },
            "supplier": {"entity_id": row["entity_id"], "code": row["code"],
                         "name": row["name"]},
        }
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------
# receipts / reports
# --------------------------------------------------------------------------

def get_receipt(conn: psycopg.Connection, operation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT operation_id, mapping_hash, normalized_mapping, receipt, status, "
        "created_at FROM migration_operations WHERE operation_id = %s",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row["receipt"])
    out["stored_mapping_hash"] = row["mapping_hash"]
    out["stored_status"] = row["status"]
    out["stored_at"] = row["created_at"].isoformat()
    return out
