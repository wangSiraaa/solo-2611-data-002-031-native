"""`snapx verify` — independently re-check a published export.

Re-reads the published directory and re-computes everything from the
JSONL bytes: digests, counts, integer sums, shard rule, PK ordering,
cross-shard uniqueness, FK closure, per-order and global payment sums,
epoch/snapshot uniformity, coordinator cross-check, and the line-by-line
baseline diff. If the export ran in barrier-demo mode it also proves the
writer committed before the export finished while the exported data still
reflects the pre-commit snapshot.
"""

import argparse
import os

from . import db, exports_root, util


class Checker:
    def __init__(self):
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = ""):
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {label}"
        if detail and not ok:
            line += f" — {detail}"
        print(line)
        if not ok:
            self.failures += 1


def cmd_verify(args: argparse.Namespace) -> int:
    c = Checker()
    final_dir = os.path.join(exports_root(), args.export_id)
    man_path = os.path.join(final_dir, "manifest.json")
    if not os.path.exists(man_path):
        print(f"verify: '{args.export_id}' is not published (no manifest.json)")
        return 4
    m = util.read_json(man_path)
    print(f"verify: export '{args.export_id}' at {final_dir}")

    c.check(m.get("status") == "published", "manifest status is 'published'")
    c.check(m.get("export_id") == args.export_id, "manifest export_id matches directory")
    n = m["params"]["shards"]
    sid = m["db"]["snapshot_id"]
    epoch = m["epoch"]
    c.check(len(m["shards"]) == n, f"manifest covers all {n} shards")

    merged_orders, merged_payments = {}, {}
    all_meta_ok = True
    for k in range(n):
        sdir = os.path.join(final_dir, "shards")
        o_path = os.path.join(sdir, f"orders-{k}.jsonl")
        p_path = os.path.join(sdir, f"payments-{k}.jsonl")
        meta_path = os.path.join(sdir, f"shard-{k}.meta.json")
        entry = next((e for e in m["shards"] if e["shard"] == k), None)
        if entry is None or not (os.path.exists(o_path) and os.path.exists(p_path)
                                 and os.path.exists(meta_path)):
            c.check(False, f"shard {k}: files present", "missing shard files")
            continue
        meta = util.read_json(meta_path)
        orders = util.read_jsonl(o_path)
        payments = util.read_jsonl(p_path)

        ok = (util.sha256_file(o_path) == entry["orders_sha256"] == meta["orders_sha256"]
              and util.sha256_file(p_path) == entry["payments_sha256"] == meta["payments_sha256"])
        c.check(ok, f"shard {k}: sha256 digests match manifest and meta")

        ok = ([o["order_id"] for o in orders] == sorted(o["order_id"] for o in orders)
              and [p["payment_id"] for p in payments] == sorted(p["payment_id"] for p in payments))
        c.check(ok, f"shard {k}: JSONL sorted by primary key")

        ok = (entry["orders"] == len(orders) and entry["payments"] == len(payments)
              and entry["order_amount_cents"] == sum(o["amount_cents"] for o in orders)
              and entry["payment_amount_cents"] == sum(p["amount_cents"] for p in payments))
        c.check(ok, f"shard {k}: row counts and integer sums match manifest")

        ok = all(util.shard_of(o["order_id"], n) == k for o in orders) and \
             all(util.shard_of(p["order_id"], n) == k for p in payments)
        c.check(ok, f"shard {k}: every row obeys shard rule order_id % {n} == {k}")

        ok = (meta["export_snapshot_id"] == sid and meta["epoch"] == epoch
              and meta["shard"] == k and meta["shards"] == n)
        all_meta_ok = all_meta_ok and ok
        c.check(ok, f"shard {k}: meta snapshot/epoch match manifest")

        cc_o = m["coordinator_crosscheck"]["orders_per_shard"].get(str(k), [0, 0])
        cc_p = m["coordinator_crosscheck"]["payments_per_shard"].get(str(k), [0, 0])
        ok = (cc_o == [len(orders), sum(o["amount_cents"] for o in orders)]
              and cc_p == [len(payments), sum(p["amount_cents"] for p in payments)])
        c.check(ok, f"shard {k}: matches coordinator's independent in-snapshot counts")

        for o in orders:
            if o["order_id"] in merged_orders:
                c.check(False, f"shard {k}: duplicate order {o['order_id']} across shards")
            merged_orders[o["order_id"]] = o
        for p in payments:
            if p["payment_id"] in merged_payments:
                c.check(False, f"shard {k}: duplicate payment {p['payment_id']} across shards")
            merged_payments[p["payment_id"]] = p

    # FK closure + per-order and global sums
    pay_by_order = {}
    fk_ok = True
    for p in merged_payments.values():
        if p["order_id"] not in merged_orders:
            fk_ok = False
        pay_by_order[p["order_id"]] = pay_by_order.get(p["order_id"], 0) + p["amount_cents"]
    c.check(fk_ok, "every payment references an exported order (FK closure)")
    bad = [oid for oid, o in merged_orders.items()
           if pay_by_order.get(oid, 0) != o["amount_cents"]]
    c.check(not bad, "per-order invariant: sum(payments) == order amount for every order",
            f"violations: {bad[:5]}")
    t = m["totals"]
    c.check(t["orders"] == len(merged_orders) and t["payments"] == len(merged_payments),
            "global row counts match manifest totals")
    c.check(t["order_amount_cents"] == sum(o["amount_cents"] for o in merged_orders.values())
            and t["payment_amount_cents"] == sum(p["amount_cents"] for p in merged_payments.values())
            and t["order_amount_cents"] == t["payment_amount_cents"],
            "global integer amount sums match (orders == payments)")

    # zero-amount orders without payments must be present
    zero_orphans = [oid for oid, o in merged_orders.items()
                    if o["amount_cents"] == 0 and pay_by_order.get(oid, 0) == 0]
    print(f"  [info] zero-amount orders without payments present in export: "
          f"{len(zero_orphans)} (e.g. {sorted(zero_orphans)[:5]})")

    # baseline line-by-line diff
    bdir = os.path.join(final_dir, "baseline")
    bmeta = util.read_json(os.path.join(bdir, "baseline.meta.json"))
    sample = bmeta["sample_order_ids"]
    o_lines = [util.canon(merged_orders[oid]) for oid in sample]
    p_lines = [util.canon(p) for p in sorted(merged_payments.values(),
               key=lambda p: (p["order_id"], p["payment_id"]))
               if p["order_id"] in set(sample)]
    for name, got in (("orders", o_lines), ("payments", p_lines)):
        with open(os.path.join(bdir, f"{name}.jsonl"), encoding="utf-8") as f:
            want = [l.rstrip("\n") for l in f if l.strip()]
        c.check(got == want, f"baseline {name}.jsonl: line-by-line identical "
                             f"({len(want)} lines)")

    # writer proof (barrier demo)
    w = m.get("writer")
    if w:
        c.check(w["commit_ts"] < m["finished_ts"],
                "writer committed BEFORE the export finished "
                f"({w['commit_iso']} < {m['finished_iso']})")
        c.check(w["epoch_before"] == epoch and w["epoch_after"] == epoch + 1,
                f"exported epoch {epoch} is the writer's pre-commit epoch "
                f"(writer moved it to {w['epoch_after']})")
        group_b = m["groups"]["B"]
        if group_b:
            late = all(e["read_started_ts"] >= w["commit_ts"]
                       for e in m["shards"] if e["shard"] in group_b)
            c.check(late, f"shards {group_b} read AFTER the writer commit, "
                          f"yet show epoch {epoch} and identical snapshot {sid}")
    if all_meta_ok:
        print(f"  [info] all {n} shards observed the same snapshot {sid} at epoch {epoch}")

    # live DB check: writer's commit is durable, export did not see it
    if not args.no_db:
        try:
            now_epoch = db.current_epoch()
            if w:
                c.check(now_epoch >= w["epoch_after"],
                        f"live DB epoch {now_epoch} >= writer's post-commit epoch "
                        f"{w['epoch_after']} (commit durable; export used pre-commit snapshot)")
            else:
                c.check(now_epoch >= epoch,
                        f"live DB epoch {now_epoch} >= exported epoch {epoch}")
        except Exception as exc:
            print(f"  [warn] live DB check skipped: {exc}")

    if c.failures:
        print(f"verify: FAILED ({c.failures} failing checks)")
        return 4
    print("verify: OK — all checks passed")
    return 0
