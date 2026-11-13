"""End-to-end scenarios for the supplier code migration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import psycopg
import pytest

from supplier_migrate import db
from supplier_migrate.mapping import MappingRejected, load_mapping
from tests.conftest import TEST_DSN, write_csv

CLI = [sys.executable, "-m", "supplier_migrate.cli", "--dsn", TEST_DSN]


def run_cli(*args, expect_ok=True):
    proc = subprocess.run(CLI + list(args), capture_output=True, text=True,
                          cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:  # pragma: no cover
        payload = {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr}
    if expect_ok:
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode, payload


def codes(conn):
    return {r["entity_id"]: r["code"] for r in conn.execute(
        "SELECT entity_id, code FROM suppliers")}


def child_entity_map(conn):
    """order_ref/contact_ref -> current supplier entity_id (via code)."""
    out = {}
    for table, key in (("purchase_orders", "order_ref"), ("contacts", "contact_ref")):
        for row in conn.execute(
            f"""
            SELECT t.{key} AS ref, s.entity_id
              FROM {table} t LEFT JOIN suppliers s ON s.code = t.supplier_code
            """
        ):
            out[(table, row["ref"])] = row["entity_id"]
    return out


def assert_entities_follow(conn, before_entities, after_entities):
    """Every pre-existing child row must still resolve to the same entity_id."""
    compared = 0
    for key, entity_id in after_entities.items():
        if key not in before_entities:
            continue  # row created after the snapshot (e.g. racing add-order)
        compared += 1
        assert entity_id == before_entities[key], (
            f"{key} moved from {before_entities[key]} to {entity_id}")
    return compared


# ---------------------------------------------------------------------------
# mapping semantics
# ---------------------------------------------------------------------------

def test_two_code_swap(fresh, tmp_path):
    before = codes(fresh)
    before_children = child_entity_map(fresh)
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    rc, result = run_cli("apply", "--mapping", path, "--operation-id", "swap")
    assert rc == 0
    after = codes(fresh)
    assert after["ENT-A"] == "SUP-B"
    assert after["ENT-B"] == "SUP-A"
    # the critical trap: A->B, B->A must NOT collapse back to original codes
    assert child_entity_map(fresh) == before_children
    assert result["affected_rows"] == {"purchase_orders": 6, "contacts": 4}
    assert_entities_follow(fresh, before_children, child_entity_map(fresh))


def test_three_node_cycle(fresh, tmp_path):
    before_children = child_entity_map(fresh)
    path = write_csv(tmp_path, "cycle.csv", [
        ("SUP-C", "SUP-D"), ("SUP-D", "SUP-E"), ("SUP-E", "SUP-C"),
    ])
    run_cli("apply", "--mapping", path, "--operation-id", "cycle")
    after = codes(fresh)
    assert after["ENT-C"] == "SUP-D"
    assert after["ENT-D"] == "SUP-E"
    assert after["ENT-E"] == "SUP-C"
    # other suppliers untouched
    assert after["ENT-A"] == "SUP-A"
    assert_entities_follow(fresh, before_children, child_entity_map(fresh))
    # and entity identity is not permuted
    names = {r["entity_id"]: r["name"] for r in fresh.execute(
        "SELECT entity_id, name FROM suppliers")}
    assert names["ENT-C"] == "Gamma Logistics"


def test_chain_into_free_code(fresh, tmp_path):
    before_children = child_entity_map(fresh)
    path = write_csv(tmp_path, "chain.csv", [
        ("SUP-A", "SUP-B"), ("SUP-B", "FREE-2"),
    ])
    run_cli("apply", "--mapping", path, "--operation-id", "chain")
    after = codes(fresh)
    assert after["ENT-A"] == "SUP-B"
    assert after["ENT-B"] == "FREE-2"
    assert after["ENT-C"] == "SUP-C"
    assert_entities_follow(fresh, before_children, child_entity_map(fresh))
    # FREE-2 is now occupied, so another target into it must be rejected
    path2 = write_csv(tmp_path, "bad.csv", [("SUP-C", "FREE-2")])
    rc, result = run_cli("plan", "--mapping", path2, expect_ok=False)
    assert rc == 1 and result["code"] == "invalid_mapping"
    assert result["errors"][0]["reason"] == "target_occupied"
    assert result["errors"][0]["line"] == 2


def test_target_occupied_by_outside_entity(fresh, tmp_path):
    path = write_csv(tmp_path, "occ.csv", [("SUP-A", "SUP-C")])
    rc, result = run_cli("plan", "--mapping", path, expect_ok=False)
    assert rc == 1
    assert result["errors"][0]["reason"] == "target_occupied"
    assert result["errors"][0]["line"] == 2
    # apply independently rejects too and changes nothing
    rc, result = run_cli("apply", "--mapping", path, "--operation-id", "occ",
                         expect_ok=False)
    assert rc == 1 and result["code"] == "invalid_mapping"
    assert codes(fresh)["ENT-A"] == "SUP-A"


def test_duplicate_source_and_target(fresh, tmp_path):
    dup_src = write_csv(tmp_path, "dsrc.csv", [
        ("SUP-A", "FREE-1"), ("SUP-A", "FREE-2"),
    ])
    rc, result = run_cli("plan", "--mapping", dup_src, expect_ok=False)
    assert rc == 1
    reasons = {(e["reason"], e["line"]) for e in result["errors"]}
    assert ("duplicate_source", 3) in reasons
    assert result["errors"][0]["line"] == 3

    dup_tgt = write_csv(tmp_path, "dtgt.csv", [
        ("SUP-A", "FREE-1"), ("SUP-B", "FREE-1"),
    ])
    rc, result = run_cli("plan", "--mapping", dup_tgt, expect_ok=False)
    assert rc == 1
    assert any(e["reason"] == "duplicate_target" and e["line"] == 3
               for e in result["errors"])


def test_missing_source(fresh, tmp_path):
    path = write_csv(tmp_path, "miss.csv", [("SUP-A", "FREE-1"), ("NOPE", "FREE-2")])
    rc, result = run_cli("plan", "--mapping", path, expect_ok=False)
    assert rc == 1
    assert any(e["reason"] == "source_missing" and e["line"] == 3
               for e in result["errors"])


def test_noop_mapping(fresh, tmp_path):
    before = codes(fresh)
    path = write_csv(tmp_path, "noop.csv", [("SUP-A", "SUP-A"), ("SUP-B", "SUP-B")])
    rc, result = run_cli("apply", "--mapping", path, "--operation-id", "noop")
    assert rc == 0
    assert result["entry_counts"]["changed"] == 0
    assert result["affected_rows"] == {"purchase_orders": 0, "contacts": 0}
    assert codes(fresh) == before
    v = result["verification"]
    assert v["status"] == "ok" and v["checked"]["purchase_orders"] == 6


def test_exact_case_sensitive_mapping(fresh, tmp_path):
    # lowercase old code does not match SUP-A, and lowercase target is *free*.
    path = write_csv(tmp_path, "case.csv", [("sup-a", "FREE-1")])
    rc, result = run_cli("plan", "--mapping", path, expect_ok=False)
    assert rc == 1
    assert result["errors"][0]["reason"] == "source_missing"


def test_identical_old_new_and_duplicate_noop_rejected(fresh, tmp_path):
    # duplicate target even for a no-op row must be rejected
    path = write_csv(tmp_path, "dupnoop.csv", [
        ("SUP-A", "SUP-A"), ("SUP-B", "SUP-A"),
    ])
    rc, result = run_cli("plan", "--mapping", path, expect_ok=False)
    assert rc == 1
    assert any(e["reason"] == "duplicate_target" for e in result["errors"])


# ---------------------------------------------------------------------------
# plan / apply consistency, idempotency, faults
# ---------------------------------------------------------------------------

def test_plan_is_read_only(fresh, tmp_path):
    before = codes(fresh)
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    rc, result = run_cli("plan", "--mapping", path)
    assert rc == 0 and result["status"] == "plan"
    assert codes(fresh) == before
    # no operation row created
    assert fresh.execute("SELECT count(*) FROM migration_operations").fetchone()["count"] == 0
    assert result["affected_rows"] == {"purchase_orders": 6, "contacts": 4}


def test_preview_then_data_change_revalidated(fresh, tmp_path):
    path = write_csv(tmp_path, "x.csv", [("SUP-A", "FREE-1")])
    run_cli("plan", "--mapping", path)  # preview OK
    # Data changes underneath: FREE-1 is taken by a brand-new supplier.  A
    # direct code move of an existing supplier would be blocked by the FK
    # (existing child rows reference the old code), so this models the
    # realistic concurrent-writer case.
    fresh.execute("INSERT INTO suppliers(entity_id, code, name) "
                  "VALUES ('ENT-NEW', 'FREE-1', 'Newcomer')")
    fresh.commit()
    rc, result = run_cli("apply", "--mapping", path, "--operation-id", "stale",
                         expect_ok=False)
    assert rc == 1 and result["code"] == "invalid_mapping"
    assert result["errors"][0]["reason"] == "target_occupied"
    assert codes(fresh)["ENT-A"] == "SUP-A"


def test_replay_same_operation_same_mapping(fresh, tmp_path):
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    _, first = run_cli("apply", "--mapping", path, "--operation-id", "op1")
    _, second = run_cli("apply", "--mapping", path, "--operation-id", "op1")
    assert second["replayed"] is True
    assert second["verification"]["status"] == "ok"
    # mapping normalised identically (same hash) even with reordered rows
    path2 = write_csv(tmp_path, "swap_rev.csv", [("SUP-B", "SUP-A"), ("SUP-A", "SUP-B")])
    _, third = run_cli("apply", "--mapping", path, "--operation-id", "op1")
    assert third["mapping_hash"] == first["mapping_hash"]
    assert len(list(fresh.execute(
        "SELECT 1 FROM migration_operations WHERE operation_id = 'op1'"
    ))) == 1


def test_reuse_operation_id_with_different_mapping(fresh, tmp_path):
    path = write_csv(tmp_path, "a.csv", [("SUP-A", "FREE-1")])
    run_cli("apply", "--mapping", path, "--operation-id", "opX")
    other = write_csv(tmp_path, "b.csv", [("SUP-B", "FREE-2")])
    rc, result = run_cli("apply", "--mapping", other, "--operation-id", "opX",
                         expect_ok=False)
    assert rc == 1 and result["code"] == "operation_id_conflict"


@pytest.mark.parametrize("stage", ["after_suppliers", "after_child_update", "after_commit"])
def test_fault_injection_and_retry(fresh, tmp_path, stage):
    before = codes(fresh)
    before_children = child_entity_map(fresh)
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])

    rc, result = run_cli("apply", "--mapping", path, "--operation-id", f"op-{stage}",
                         "--fault-at", stage, expect_ok=False)
    assert rc == 1 and result["stage"] == stage

    if stage == "after_commit":
        # commit is durable; suppliers already swapped
        assert result["committed"] is True
        assert codes(fresh)["ENT-A"] == "SUP-B"
    else:
        # rollback: untouched database, still fully usable
        assert result["committed"] is False
        assert codes(fresh) == before
        assert fresh.execute(
            "SELECT count(*) FROM migration_operations WHERE operation_id = %s",
            (f"op-{stage}",)).fetchone()["count"] == 0

    # a plain retry with the same operation id finishes idempotently
    rc, retry = run_cli("apply", "--mapping", path, "--operation-id", f"op-{stage}")
    assert rc == 0
    assert retry["status"] == "committed"
    assert_entities_follow(fresh, before_children, child_entity_map(fresh))
    assert retry["verification"]["status"] == "ok"


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------

def _apply_in_thread(tmp_path, op_id, results, barrier=None, delay=0.0):
    try:
        if barrier is not None:
            barrier.wait()

        def go():
            # exercise the library directly for deterministic timing
            from supplier_migrate import db as dbm
            with dbm.connect(TEST_DSN) as conn:
                mapping = load_mapping(str(tmp_path), None)
                return dbm.apply(conn, mapping, op_id)

        results.append(("ok", go()))
    except Exception as exc:  # noqa: BLE001
        results.append(("err", exc))


def test_add_order_vs_apply_race(fresh, tmp_path):
    """Concurrent add-order and swap: the DB serialises them.  Either the order
    lands pre-swap on SUP-A/ENT-A (and is migrated with it), or it is rejected
    as a stale reference once the code belongs to ENT-B.  Never silently bound
    to the wrong entity.
    """
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    before_children = child_entity_map(fresh)

    outcomes = []
    barrier = threading.Barrier(2)

    def do_apply():
        barrier.wait()
        with db.connect(TEST_DSN) as conn:
            mapping = load_mapping(str(path), None)
            try:
                db.apply(conn, mapping, "race-op")
                outcomes.append(("apply", "ok", None))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("apply", "err", exc))

    def do_add():
        barrier.wait()
        with db.connect(TEST_DSN) as conn:
            try:
                r = db.add_order(conn, "SUP-A", "ENT-A", order_ref="RACE-PO")
                outcomes.append(("add", "ok", r))
            except db.ApplyRejected as exc:
                outcomes.append(("add", "rejected", exc.code))

    threads = [threading.Thread(target=do_apply), threading.Thread(target=do_add)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    added = [o for o in outcomes if o[0] == "add"][0]
    applied = [o for o in outcomes if o[0] == "apply"][0]
    assert applied[1] == "ok", applied

    after = codes(fresh)
    if added[1] == "ok":
        # landed before the swap; the row must have followed ENT-A to SUP-B
        row = fresh.execute(
            "SELECT po.supplier_code, s.entity_id FROM purchase_orders po "
            "JOIN suppliers s ON s.code = po.supplier_code "
            "WHERE po.order_ref = 'RACE-PO'").fetchone()
        assert row is not None
        assert row["entity_id"] == "ENT-A"
        assert row["supplier_code"] == after["ENT-A"] == "SUP-B"
        # every *pre-existing* row also followed its entity
        assert_entities_follow(fresh, before_children, child_entity_map(fresh))
    else:
        assert added[1] == "rejected"
        assert added[2] in ("stale_entity_reference", "unknown_supplier_code")
        assert fresh.execute(
            "SELECT count(*) FROM purchase_orders WHERE order_ref = 'RACE-PO'"
        ).fetchone()["count"] == 0
        assert_entities_follow(fresh, before_children, child_entity_map(fresh))


def test_add_order_serialised_before_apply_commits(fresh, tmp_path):
    """Deterministic ordering: hold the swap open inside its transaction after
    the supplier rewrite, and prove a concurrent add-order blocks (it cannot
    observe the half-written state), then succeeds only against the final
    state or is rejected.

    We simulate the blocked reader with a second connection that tries to
    acquire the advisory lock with a short timeout.
    """
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])

    holder_ready = threading.Event()
    release = threading.Event()
    saw = {}

    def holder():
        with db.connect(TEST_DSN) as conn:
            db._lock(conn)
            mapping = load_mapping(str(path), None)
            # run the same locked transaction but pause before COMMIT
            receipt, _ = db._apply_core(conn, mapping, "hold-op", None,
                                        lambda stage: None)
            holder_ready.set()
            release.wait(timeout=30)
            conn.commit()

    t = threading.Thread(target=holder)
    t.start()
    assert holder_ready.wait(timeout=30)

    # A competing writer must not be able to proceed while the swap is open.
    with db.connect(TEST_DSN, autocommit=True) as probe:
        got = probe.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (db.LOCK_KEY,)
        ).fetchone()["ok"]
        assert got is False, "advisory lock should still be held mid-transaction"
        probe.execute("SELECT pg_advisory_unlock_all()")

    release.set()
    t.join(timeout=30)

    # after the commit, a stale reference is rejected deterministically
    with db.connect(TEST_DSN) as c:
        with pytest.raises(db.ApplyRejected) as ei:
            db.add_order(c, "SUP-A", "ENT-A")
        assert ei.value.code == "stale_entity_reference"


def test_no_temporary_code_visible_after_commit(fresh, tmp_path):
    path = write_csv(tmp_path, "cycle.csv", [
        ("SUP-A", "SUP-B"), ("SUP-B", "SUP-C"), ("SUP-C", "SUP-A"),
    ])
    run_cli("apply", "--mapping", path, "--operation-id", "cycle-temp")
    leftover = fresh.execute(
        "SELECT count(*) AS n FROM suppliers WHERE code LIKE '%%supplier_migrate%%'"
    ).fetchone()["n"]
    assert leftover == 0
    condeferred = fresh.execute(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE conname IN ('purchase_orders_supplier_fk', 'contacts_supplier_fk') "
        "AND condeferred").fetchone()["n"]
    assert condeferred == 0


def test_add_order_rejects_stale_after_swap(fresh, tmp_path):
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    run_cli("apply", "--mapping", path, "--operation-id", "swap")
    # SUP-A now belongs to ENT-B; asking for ENT-A must be rejected
    proc = subprocess.run(
        CLI + ["add-order", "--supplier-code", "SUP-A", "--expected-entity-id", "ENT-A"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["code"] == "stale_entity_reference"
    assert payload["details"]["current_entity_id"] == "ENT-B"
    assert fresh.execute(
        "SELECT count(*) FROM purchase_orders WHERE order_ref LIKE 'auto-%'"
    ).fetchone()["count"] == 0
    # correct pairing succeeds
    rc, payload = run_cli("add-order", "--supplier-code", "SUP-A",
                          "--expected-entity-id", "ENT-B", "--order-ref", "NEW-1")
    assert rc == 0 and payload["status"] == "created"


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def test_verify_and_export(fresh, tmp_path):
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    run_cli("apply", "--mapping", path, "--operation-id", "op-export")
    rc, report = run_cli("verify", "--operation-id", "op-export")
    assert rc == 0 and report["status"] == "ok"
    assert report["entities_checked"] == 2
    assert report["dangling_foreign_keys"] == {"purchase_orders": 0, "contacts": 0}

    out = tmp_path / "receipt.json"
    rc, bundle = run_cli("export-receipt", "--operation-id", "op-export",
                         "--output", str(out))
    assert rc == 0 and bundle["verification_report"]["status"] == "ok"
    saved = json.loads(out.read_text())
    assert saved["receipt"]["operation_id"] == "op-export"
    assert saved["receipt"]["mapping_hash"] == bundle["receipt"]["mapping_hash"]


def test_verify_detects_tampering(fresh, tmp_path):
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    run_cli("apply", "--mapping", path, "--operation-id", "op-tamper")
    # simulate out-of-band corruption: an order of ENT-A (now coded SUP-B)
    # is moved to SUP-C's code.  FK still allows it (SUP-C exists), but the
    # entity is wrong.
    fresh.execute("UPDATE purchase_orders SET supplier_code = 'SUP-C' "
                  "WHERE order_ref = 'PO-SUP-A-1'")
    fresh.commit()
    rc, report = run_cli("verify", "--operation-id", "op-tamper", expect_ok=False)
    assert rc == 1
    assert report["status"] == "failed"
    assert any(m["reason"] == "row_no_longer_belongs_to_entity"
               for m in report["mismatches"])


def test_entity_id_immutable(fresh):
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        fresh.execute("UPDATE suppliers SET entity_id = 'ENT-Z' WHERE entity_id = 'ENT-A'")
    fresh.rollback()


def test_foreign_keys_still_enforced_after_apply(fresh, tmp_path):
    path = write_csv(tmp_path, "swap.csv", [("SUP-A", "SUP-B"), ("SUP-B", "SUP-A")])
    run_cli("apply", "--mapping", path, "--operation-id", "fk")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        fresh.execute("INSERT INTO purchase_orders(order_ref, supplier_code) "
                      "VALUES ('BAD', 'NOT-A-SUPPLIER')")
    fresh.rollback()
    # constraint is not left deferred
    condefer = fresh.execute(
        "SELECT condeferrable, condeferred FROM pg_constraint "
        "WHERE conname = 'purchase_orders_supplier_fk'").fetchone()
    assert condefer["condeferrable"] is True
    assert condefer["condeferred"] is False


def test_chain_preserves_identity_not_string(fresh, tmp_path):
    """A->B, B->FREE-2: ENT-A must end on B and ENT-B on FREE-2.  A naive
    sequential string replace would leave ENT-A's rows on FREE-2 (wrong)."""
    before_children = child_entity_map(fresh)
    path = write_csv(tmp_path, "chain.csv", [("SUP-A", "SUP-B"), ("SUP-B", "FREE-2")])
    run_cli("apply", "--mapping", path, "--operation-id", "chain-id")
    assert codes(fresh)["ENT-A"] == "SUP-B"
    assert codes(fresh)["ENT-B"] == "FREE-2"
    assert_entities_follow(fresh, before_children, child_entity_map(fresh))
    row = fresh.execute(
        "SELECT s.entity_id FROM purchase_orders po JOIN suppliers s "
        "ON s.code = po.supplier_code WHERE po.order_ref = 'PO-SUP-A-1'"
    ).fetchone()
    assert row["entity_id"] == "ENT-A"


def test_lowercase_free_target_is_distinct(fresh, tmp_path):
    """Exact comparison: 'sup-a' is unused even though 'SUP-A' exists, so it is
    a legal target; the resulting code is genuinely lowercase."""
    path = write_csv(tmp_path, "lower.csv", [("SUP-A", "sup-a")])
    rc, result = run_cli("apply", "--mapping", path, "--operation-id", "lower")
    assert rc == 0
    assert codes(fresh)["ENT-A"] == "sup-a"
    # and the uppercase original is now free again
    assert "SUP-A" not in codes(fresh).values()


def test_malformed_header_and_empty_file(fresh, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("source,target\nSUP-A," + "FREE-1\n")
    rc, result = run_cli("plan", "--mapping", str(bad), expect_ok=False)
    assert rc == 1 and result["errors"][0]["reason"] == "wrong_header"

    empty = tmp_path / "empty.csv"
    empty.write_text("")
    rc, result = run_cli("plan", "--mapping", str(empty), expect_ok=False)
    assert rc == 1 and result["errors"][0]["reason"] == "missing_header"

    only_header = tmp_path / "h.csv"
    only_header.write_text("old_code,new_code\n")
    rc, result = run_cli("apply", "--mapping", str(only_header),
                         "--operation-id", "empty-op")
    assert rc == 0 and result["entry_counts"]["total"] == 0
    assert result["affected_rows"] == {"purchase_orders": 0, "contacts": 0}
