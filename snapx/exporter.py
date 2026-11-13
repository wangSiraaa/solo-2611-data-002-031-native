"""`snapx export` — coordinator process.

Flow:
  1. Idempotency: if exports/<export_id>/ is already published, return the
     original artifacts (same params) or refuse (different shard params).
  2. Stale attempts of the same export_id are marked ABANDONED — their
     snapshot died with their coordinator, their shards must never be
     mixed into a new attempt.
  3. Coordinator opens a REPEATABLE READ transaction and calls
     pg_export_snapshot(). The transaction stays open for the whole export.
  4. Independent worker PROCESSES import that snapshot in their own
     REPEATABLE READ READ ONLY transactions and each reads one shard
     (stable rule: order_id % N). No data is copied to a staging database
     and no table locks are taken — MVCC only.
  5. --barrier-demo: even shards read BEFORE a gated writer commits, odd
     shards wait on a file barrier and read AFTER the commit; both groups
     must observe the identical snapshot.
  6. Failed workers (while the coordinator transaction lives) are retried
     on the SAME snapshot. If the coordinator dies, the attempt can never
     be published; a later run starts a fresh attempt with a new snapshot.
  7. All shards are validated (digests, counts, sums, shard rule, FK
     closure, per-order payment sums, coordinator cross-check, baseline
     line-by-line diff). Only then manifest.json is written and the
     payload is atomically renamed to exports/<export_id>/.
"""

import argparse
import os
import subprocess
import sys
import time

from . import PROJECT_ROOT, db, exports_root, util

WORKER_TIMEOUT = 120.0


class AttemptFailed(Exception):
    pass


# ---------------------------------------------------------------- paths

def _final_dir(export_id: str) -> str:
    return os.path.join(exports_root(), export_id)


def _attempts_dir(export_id: str) -> str:
    return os.path.join(exports_root(), "_attempts", export_id)


def _check_export_id(export_id: str) -> None:
    if not util.EXPORT_ID_RE.match(export_id):
        raise SystemExit(f"invalid export_id {export_id!r}")


# ------------------------------------------------------- attempt ledger

def _attempt_record(attempt_dir: str) -> dict:
    path = os.path.join(attempt_dir, "ATTEMPT.json")
    return util.read_json(path) if os.path.exists(path) else {}


def _write_attempt(attempt_dir: str, rec: dict) -> None:
    util.write_json_atomic(os.path.join(attempt_dir, "ATTEMPT.json"), rec)


def _abandon_stale_attempts(export_id: str) -> int:
    """Mark non-terminal attempts as abandoned; return next attempt number."""
    root = _attempts_dir(export_id)
    nums = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            if name.startswith("attempt-"):
                try:
                    nums.append(int(name.split("-", 1)[1]))
                except ValueError:
                    pass
    for num in nums:
        d = os.path.join(root, f"attempt-{num}")
        rec = _attempt_record(d)
        if rec.get("status") in (None, "running"):
            rec.update({
                "status": "abandoned",
                "reason": "attempt never published (coordinator exited or was killed); "
                          "its snapshot is invalid and its shards must not be reused",
                "ended_ts": time.time(), "ended_iso": util.iso(time.time()),
            })
            _write_attempt(d, rec)
            print(f"export: previous attempt {num} of '{export_id}' marked ABANDONED", flush=True)
    return (max(nums) + 1) if nums else 1


# ------------------------------------------------------------- workers

def _spawn_worker(args, sid, k, try_no, group, payload_dir, control_dir):
    log_path = os.path.join(control_dir, f"worker-{k}-try{try_no}.log")
    logf = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, "-m", "snapx", "_worker",
        "--snapshot", sid, "--shard", str(k), "--shards", str(args.shards),
        "--out", payload_dir, "--control", control_dir,
        "--group", group, "--coordinator-pid", str(os.getpid()),
        "--try", str(try_no), "--barrier-timeout", str(args.barrier_timeout),
    ]
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
    return proc, logf


def _spawn_writer(args, gate_dir):
    logf = open(os.path.join(gate_dir, "writer.log"), "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "snapx", "writer", "--once",
           "--seed", str(args.writer_seed), "--gate-dir", gate_dir,
           "--gate-timeout", str(args.gate_timeout)]
    return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env), logf


# --------------------------------------------------------- coordinator

def _wait_acquired(procs: dict, barrier_dir: str, deadline: float):
    """Wait until every shard imported the snapshot (acquired marker)."""
    pending = set(procs)
    while pending and time.monotonic() < deadline:
        for k in list(pending):
            if os.path.exists(os.path.join(barrier_dir, f"{k}.acquired")):
                pending.discard(k)
            elif procs[k][0].poll() is not None:
                return k  # worker died before acquiring
        time.sleep(0.05)
    return next(iter(pending)) if pending else None


def _reap_shard(args, sid, k, procs, tries, payload_dir, control_dir, group):
    """Wait for shard k's worker; retry crash-exits on the same snapshot."""
    while True:
        proc, logf = procs[k]
        rc = proc.wait()
        logf.close()
        if rc == 0:
            return
        if rc == 3 and tries[k] < args.max_shard_retries:
            tries[k] += 1
            print(f"export: shard {k} worker crashed (try {tries[k]-1}); "
                  f"retrying on the SAME snapshot (try {tries[k]})", flush=True)
            procs[k] = _spawn_worker(args, sid, k, tries[k], group, payload_dir, control_dir)
            continue
        raise AttemptFailed(f"shard {k} worker exited rc={rc} after {tries[k]} "
                            f"try/tries — see attempt control log")


def _coordinator_counts(conn, n):
    orders, payments = {}, {}
    for r in conn.execute("SELECT mod(order_id,%s), count(*), COALESCE(sum(amount_cents),0) "
                          "FROM orders GROUP BY 1", (n,)):
        orders[int(r[0])] = (int(r[1]), int(r[2]))
    for r in conn.execute("SELECT mod(order_id,%s), count(*), COALESCE(sum(amount_cents),0) "
                          "FROM payments GROUP BY 1", (n,)):
        payments[int(r[0])] = (int(r[1]), int(r[2]))
    return {"orders_per_shard": orders, "payments_per_shard": payments}


def _write_baseline(conn, n, per_shard, payload_dir, sid, epoch):
    """Small-sample baseline: independent complete queries inside the
    coordinator transaction, saved as canonical JSONL for a later
    line-by-line diff against the merged shard output."""
    bdir = os.path.join(payload_dir, "baseline")
    os.makedirs(bdir, exist_ok=True)
    sample_ids = []
    for k in range(n):
        rows = conn.execute(
            "SELECT order_id FROM orders WHERE mod(order_id,%s)=%s "
            "ORDER BY order_id LIMIT %s", (n, k, per_shard)).fetchall()
        sample_ids.extend(int(r[0]) for r in rows)
    sample_ids.sort()
    o_lines, p_lines = [], []
    for oid in sample_ids:
        r = conn.execute("SELECT order_id, customer_id, amount_cents FROM orders "
                         "WHERE order_id=%s", (oid,)).fetchone()
        o_lines.append(util.canon({"order_id": int(r[0]), "customer_id": int(r[1]),
                                   "amount_cents": int(r[2])}))
        for p in conn.execute("SELECT payment_id, order_id, amount_cents FROM payments "
                              "WHERE order_id=%s ORDER BY payment_id", (oid,)):
            p_lines.append(util.canon({"payment_id": int(p[0]), "order_id": int(p[1]),
                                       "amount_cents": int(p[2])}))
    util.write_file_atomic(os.path.join(bdir, "orders.jsonl"), "".join(l + "\n" for l in o_lines))
    util.write_file_atomic(os.path.join(bdir, "payments.jsonl"), "".join(l + "\n" for l in p_lines))
    util.write_json_atomic(os.path.join(bdir, "baseline.meta.json"), {
        "snapshot_id": sid, "epoch": epoch, "sample_order_ids": sample_ids,
        "orders_sha256": util.sha256_file(os.path.join(bdir, "orders.jsonl")),
        "payments_sha256": util.sha256_file(os.path.join(bdir, "payments.jsonl")),
    })
    return sample_ids


# ----------------------------------------------------------- validation

def _load_shard(payload_dir, k):
    sdir = os.path.join(payload_dir, "shards")
    meta = util.read_json(os.path.join(sdir, f"shard-{k}.meta.json"))
    orders = util.read_jsonl(os.path.join(sdir, f"orders-{k}.jsonl"))
    payments = util.read_jsonl(os.path.join(sdir, f"payments-{k}.jsonl"))
    return meta, orders, payments


def _validate(payload_dir, n, sid, epoch0, coord):
    """Validate all shard outputs; return (shard_entries, merged_orders, merged_payments)."""
    entries, merged_orders, merged_payments = [], {}, {}
    for k in range(n):
        meta, orders, payments = _load_shard(payload_dir, k)
        if meta["export_snapshot_id"] != sid:
            raise AttemptFailed(f"shard {k}: snapshot mismatch {meta['export_snapshot_id']} != {sid}")
        if meta["epoch"] != epoch0:
            raise AttemptFailed(f"shard {k}: epoch {meta['epoch']} != coordinator epoch {epoch0}")
        if meta["shard"] != k or meta["shards"] != n:
            raise AttemptFailed(f"shard {k}: bad meta identity")
        for rows, pk, name in ((orders, "order_id", "orders"), (payments, "payment_id", "payments")):
            keys = [r[pk] for r in rows]
            if keys != sorted(keys) or len(set(keys)) != len(keys):
                raise AttemptFailed(f"shard {k}: {name} not strictly sorted by {pk}")
        for o in orders:
            if util.shard_of(o["order_id"], n) != k:
                raise AttemptFailed(f"shard {k}: order {o['order_id']} violates shard rule")
        for p in payments:
            if util.shard_of(p["order_id"], n) != k:
                raise AttemptFailed(f"shard {k}: payment {p['payment_id']} violates shard rule")
        sdir = os.path.join(payload_dir, "shards")
        for name, rows, cnt_key, sum_key, hash_key in (
                ("orders", orders, "orders", "order_amount_cents", "orders_sha256"),
                ("payments", payments, "payments", "payment_amount_cents", "payments_sha256")):
            path = os.path.join(sdir, f"{name}-{k}.jsonl")
            if util.sha256_file(path) != meta[hash_key]:
                raise AttemptFailed(f"shard {k}: {name} digest mismatch")
            if meta[cnt_key] != len(rows):
                raise AttemptFailed(f"shard {k}: {name} count mismatch")
            if meta[sum_key] != sum(r["amount_cents"] for r in rows):
                raise AttemptFailed(f"shard {k}: {name} sum mismatch")
        # cross-check against the coordinator's own in-snapshot counts
        oc, osum = coord["orders_per_shard"].get(k, (0, 0))
        pc, psum = coord["payments_per_shard"].get(k, (0, 0))
        if (len(orders), sum(o["amount_cents"] for o in orders)) != (oc, osum):
            raise AttemptFailed(f"shard {k}: worker/coordinator orders mismatch")
        if (len(payments), sum(p["amount_cents"] for p in payments)) != (pc, psum):
            raise AttemptFailed(f"shard {k}: worker/coordinator payments mismatch")
        for o in orders:
            if o["order_id"] in merged_orders:
                raise AttemptFailed(f"order {o['order_id']} appears in two shards")
            merged_orders[o["order_id"]] = o
        for p in payments:
            if p["payment_id"] in merged_payments:
                raise AttemptFailed(f"payment {p['payment_id']} appears in two shards")
            merged_payments[p["payment_id"]] = p
        entries.append({
            "shard": k, "tries": meta["try"], "group": meta["group"],
            "orders": len(orders), "payments": len(payments),
            "order_amount_cents": sum(o["amount_cents"] for o in orders),
            "payment_amount_cents": sum(p["amount_cents"] for p in payments),
            "orders_sha256": meta["orders_sha256"], "payments_sha256": meta["payments_sha256"],
            "read_started_ts": meta["read_started_ts"], "read_finished_ts": meta["read_finished_ts"],
        })
    # FK closure + per-order invariant over the merged view
    pay_by_order = {}
    for p in merged_payments.values():
        if p["order_id"] not in merged_orders:
            raise AttemptFailed(f"payment {p['payment_id']} references missing order {p['order_id']}")
        pay_by_order[p["order_id"]] = pay_by_order.get(p["order_id"], 0) + p["amount_cents"]
    for oid, o in merged_orders.items():
        if pay_by_order.get(oid, 0) != o["amount_cents"]:
            raise AttemptFailed(f"order {oid}: payments sum != order amount")
    return entries, merged_orders, merged_payments


def _baseline_diff(payload_dir, merged_orders, merged_payments):
    """Line-by-line diff: coordinator baseline vs merged shard content."""
    bdir = os.path.join(payload_dir, "baseline")
    meta = util.read_json(os.path.join(bdir, "baseline.meta.json"))
    sample = meta["sample_order_ids"]
    o_lines = [util.canon(merged_orders[oid]) for oid in sample]
    p_lines = [util.canon(p) for p in sorted(merged_payments.values(),
               key=lambda p: (p["order_id"], p["payment_id"])) if p["order_id"] in set(sample)]
    for name, got in (("orders", o_lines), ("payments", p_lines)):
        with open(os.path.join(bdir, f"{name}.jsonl"), encoding="utf-8") as f:
            want = [l.rstrip("\n") for l in f if l.strip()]
        if got != want:
            for i, (g, w) in enumerate(zip(got + [None] * len(want), want + [None] * len(got))):
                if g != w:
                    raise AttemptFailed(f"baseline diff {name}.jsonl line {i + 1}: {g!r} != {w!r}")
    return sample


# ---------------------------------------------------------------- main

def cmd_export(args: argparse.Namespace) -> int:
    _check_export_id(args.export_id)
    final_dir = _final_dir(args.export_id)
    manifest_path = os.path.join(final_dir, "manifest.json")

    # 1. idempotent retry / parameter conflict
    if os.path.exists(manifest_path):
        man = util.read_json(manifest_path)
        if man.get("params", {}).get("shards") == args.shards:
            print(f"export '{args.export_id}' already published "
                  f"(attempt {man['attempt']}, snapshot {man['db']['snapshot_id']}, "
                  f"epoch {man['epoch']}) — returning original artifacts")
            print(f"  dir: {final_dir}")
            return 0
        print(f"export: REFUSED — export_id '{args.export_id}' already published with "
              f"shards={man.get('params', {}).get('shards')}, requested shards={args.shards}; "
              f"published artifacts are immutable", file=sys.stderr)
        return 2

    attempt = _abandon_stale_attempts(args.export_id)
    attempt_dir = os.path.join(_attempts_dir(args.export_id), f"attempt-{attempt}")
    control_dir = os.path.join(attempt_dir, "control")
    payload_dir = os.path.join(attempt_dir, "payload")
    barrier_dir = os.path.join(control_dir, "barrier")
    gate_dir = os.path.join(control_dir, "gate")
    for d in (control_dir, payload_dir, barrier_dir, gate_dir):
        os.makedirs(d, exist_ok=True)

    rec = {"export_id": args.export_id, "attempt": attempt, "status": "running",
           "pid": os.getpid(), "started_ts": time.time(), "started_iso": util.iso(time.time())}
    _write_attempt(attempt_dir, rec)

    procs, tries, logfs = {}, {}, []
    writer_proc = None
    conn = db.connect()
    started = time.time()

    def fail(reason: str, rc: int = 3) -> int:
        rec.update({"status": "failed", "reason": reason,
                    "ended_ts": time.time(), "ended_iso": util.iso(time.time())})
        _write_attempt(attempt_dir, rec)
        for p, lf in procs.values():
            if p.poll() is None:
                p.terminate()
        if writer_proc and writer_proc[0].poll() is None:
            writer_proc[0].terminate()
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        print(f"export: attempt {attempt} FAILED: {reason}", file=sys.stderr, flush=True)
        print(f"  attempt dir retained at {attempt_dir} (status=failed, not publishable)")
        return rc

    try:
        # 2. coordinator transaction + exported snapshot
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        sid = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        epoch0 = int(conn.execute("SELECT epoch FROM dataset_epoch WHERE id = 1").fetchone()[0])
        rec.update({"snapshot_id": sid, "epoch": epoch0})
        _write_attempt(attempt_dir, rec)
        print(f"export: attempt {attempt} snapshot={sid} epoch={epoch0} shards={args.shards}", flush=True)

        coord = _coordinator_counts(conn, args.shards)
        sample_ids = _write_baseline(conn, args.shards, args.baseline_per_shard,
                                     payload_dir, sid, epoch0)

        # 3. gated writer (barrier demo): begins its txn, waits for our allow
        if args.barrier_demo:
            writer_proc = _spawn_writer(args, gate_dir)
            logfs.append(writer_proc[1])
            if not util.wait_for(os.path.join(gate_dir, "ready.json"), args.gate_timeout):
                return fail("gated writer never became ready")
            print("export: gated writer has an OPEN uncommitted transaction (waiting)")

        # 4. spawn workers: even shards group A (read before writer commit),
        #    odd shards group B (read after writer commit)
        groups = {k: ("A" if k % 2 == 0 else "B") for k in range(args.shards)}
        if not args.barrier_demo:
            groups = {k: "A" for k in range(args.shards)}
        for k in range(args.shards):
            tries[k] = 1
            procs[k] = _spawn_worker(args, sid, k, 1, groups[k], payload_dir, control_dir)

        deadline = time.monotonic() + WORKER_TIMEOUT
        bad = _wait_acquired(procs, barrier_dir, deadline)
        if bad is not None:
            return fail(f"shard {bad} failed to acquire snapshot in time")
        print(f"export: all {args.shards} workers imported snapshot {sid}", flush=True)

        if args.pause_after_acquire > 0:
            print(f"export: pausing {args.pause_after_acquire}s with snapshot held "
                  f"(kill-test window)", flush=True)
            time.sleep(args.pause_after_acquire)

        # 5. group A reads before the writer commits
        for k in sorted(k for k in groups if groups[k] == "A"):
            _reap_shard(args, sid, k, procs, tries, payload_dir, control_dir, "A")

        writer_info = None
        if args.barrier_demo:
            util.touch(os.path.join(gate_dir, "commit.allow"))
            if not util.wait_for(os.path.join(gate_dir, "committed.json"), args.gate_timeout):
                return fail("gated writer did not commit in time")
            writer_info = util.read_json(os.path.join(gate_dir, "committed.json"))
            print(f"export: writer COMMITTED mid-export "
                  f"(epoch {writer_info['epoch_before']} -> {writer_info['epoch_after']})",
                  flush=True)
            util.touch(os.path.join(barrier_dir, "release_b"))

        # 6. group B reads after the writer commit, same snapshot
        for k in sorted(k for k in groups if groups[k] == "B"):
            _reap_shard(args, sid, k, procs, tries, payload_dir, control_dir, "B")

        if writer_proc:
            wrc = writer_proc[0].wait()
            writer_proc[1].close()
            if wrc != 0:
                return fail(f"gated writer exited rc={wrc}")

        # 7. validate everything before publishing anything
        entries, merged_orders, merged_payments = _validate(payload_dir, args.shards,
                                                            sid, epoch0, coord)
        _baseline_diff(payload_dir, merged_orders, merged_payments)
        print(f"export: validation passed "
          f"({len(merged_orders)} orders, {len(merged_payments)} payments, "
          f"baseline sample {len(sample_ids)} orders)", flush=True)

        finished = time.time()
        manifest = {
            "export_id": args.export_id, "attempt": attempt, "status": "published",
            "created_ts": started, "created_iso": util.iso(started),
            "finished_ts": finished, "finished_iso": util.iso(finished),
            "db": {"dbname": os.environ.get("SNAPX_PGDB", db.DEFAULT_DB), "snapshot_id": sid},
            "params": {"shards": args.shards},
            "epoch": epoch0,
            "coordinator_crosscheck": {
                "orders_per_shard": {str(k): list(v) for k, v in coord["orders_per_shard"].items()},
                "payments_per_shard": {str(k): list(v) for k, v in coord["payments_per_shard"].items()},
            },
            "totals": {
                "orders": len(merged_orders), "payments": len(merged_payments),
                "order_amount_cents": sum(o["amount_cents"] for o in merged_orders.values()),
                "payment_amount_cents": sum(p["amount_cents"] for p in merged_payments.values()),
            },
            "shards": entries,
            "baseline": {
                "sample_order_ids": sample_ids,
                "orders_file": "baseline/orders.jsonl",
                "payments_file": "baseline/payments.jsonl",
            },
            "groups": {"A": sorted(k for k in groups if groups[k] == "A"),
                       "B": sorted(k for k in groups if groups[k] == "B")},
            "writer": writer_info,
        }
        util.write_json_atomic(os.path.join(payload_dir, "manifest.json"), manifest)

        # 8. atomic publish: rename only ever happens once, onto a non-existing dir
        try:
            os.rename(payload_dir, final_dir)
        except OSError:
            existing = util.read_json(manifest_path)
            rec.update({"status": "failed",
                        "reason": "concurrent publisher won the rename race; existing "
                                  "publication retained",
                        "ended_ts": time.time(), "ended_iso": util.iso(time.time())})
            _write_attempt(attempt_dir, rec)
            conn.execute("ROLLBACK")
            conn.close()
            print(f"export: already published concurrently (attempt "
                  f"{existing['attempt']}) — returning original artifacts", flush=True)
            return 0

        rec.update({"status": "published", "published_ts": time.time(),
                    "ended_ts": time.time(), "ended_iso": util.iso(time.time())})
        _write_attempt(attempt_dir, rec)
        conn.execute("COMMIT")
        conn.close()
        t = manifest["totals"]
        print(f"export: PUBLISHED {final_dir}", flush=True)
        print(f"  epoch={epoch0} orders={t['orders']} payments={t['payments']} "
              f"order_amount_cents={t['order_amount_cents']} "
              f"payment_amount_cents={t['payment_amount_cents']}")
        if writer_info:
            print(f"  writer committed during export: commit_ts={writer_info['commit_ts']:.3f} "
                  f"< finished_ts={finished:.3f}")
        return 0

    except AttemptFailed as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return fail(f"unexpected coordinator error: {exc!r}")
