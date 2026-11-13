"""`snapx _worker` — internal export worker (spawned by the coordinator).

Each worker is an independent OS process. It opens its own connection,
starts a REPEATABLE READ READ ONLY transaction, imports the coordinator's
exported snapshot, and reads exactly one shard (orders + their payments +
the epoch row). Because of MVCC the worker never blocks writers and is
never blocked by them.

Exit codes: 0 ok | 3 simulated crash | 4 snapshot import failed |
5 barrier/coordinator lost | 6 unexpected error.
"""

import argparse
import os
import sys
import time

from . import db, util


def _write_jsonl(shards_dir: str, name: str, rows: list) -> dict:
    path = os.path.join(shards_dir, name)
    tmp = path + ".tmp"
    body = "".join(util.canon(r) + "\n" for r in rows)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    return {"path": path, "rows": len(rows), "sha256": util.sha256_text(body)}


def _coord_alive(pid: int) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run(args: argparse.Namespace) -> int:
    k, n = args.shard, args.shards
    shards_dir = os.path.join(args.out, "shards")
    barrier_dir = os.path.join(args.control, "barrier")
    os.makedirs(shards_dir, exist_ok=True)
    os.makedirs(barrier_dir, exist_ok=True)
    log = lambda msg: print(f"[worker shard={k} try={args.try_no} pid={os.getpid()}] {msg}", flush=True)

    conn = db.connect()
    try:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            conn.execute(f"SET TRANSACTION SNAPSHOT '{args.snapshot}'")
        except Exception as exc:  # snapshot gone (coordinator died) -> fatal
            log(f"cannot import snapshot {args.snapshot}: {exc}")
            return 4
        util.touch(os.path.join(barrier_dir, f"{k}.acquired"))
        log(f"snapshot {args.snapshot} imported")

        # --- deterministic crash injection for the retry test -------------
        crash_env = {int(x) for x in os.environ.get("SNAPX_CRASH_SHARDS", "").split(",") if x.strip()}
        crash_marker = os.path.join(args.control, f"crash-{k}-done")
        if k in crash_env and not os.path.exists(crash_marker):
            util.touch(crash_marker)
            with open(os.path.join(shards_dir, f"orders-{k}.jsonl.tmp"), "w") as f:
                f.write('{"order_id": 1, "customer_id"')  # torn write
            log("simulated crash (SIGKILL-like) after partial write")
            os._exit(3)
        # ------------------------------------------------------------------

        if args.group == "B":
            release = os.path.join(barrier_dir, "release_b")
            log("group B: waiting for release barrier (writer commit)")
            ok = util.wait_for(release, args.barrier_timeout,
                               abort=lambda: not _coord_alive(args.coordinator_pid))
            if not ok:
                log("release barrier timeout or coordinator lost")
                return 5
            log("released: reading AFTER the writer committed")

        t0 = time.time()
        epoch = int(conn.execute("SELECT epoch FROM dataset_epoch WHERE id = 1").fetchone()[0])
        orders = [
            {"order_id": r[0], "customer_id": r[1], "amount_cents": int(r[2])}
            for r in conn.execute(
                "SELECT order_id, customer_id, amount_cents FROM orders "
                "WHERE mod(order_id, %s) = %s ORDER BY order_id", (n, k))
        ]
        payments = [
            {"payment_id": r[0], "order_id": r[1], "amount_cents": int(r[2])}
            for r in conn.execute(
                "SELECT payment_id, order_id, amount_cents FROM payments "
                "WHERE mod(order_id, %s) = %s ORDER BY payment_id", (n, k))
        ]
        t1 = time.time()

        o = _write_jsonl(shards_dir, f"orders-{k}.jsonl", orders)
        p = _write_jsonl(shards_dir, f"payments-{k}.jsonl", payments)
        meta = {
            "export_snapshot_id": args.snapshot,
            "shard": k, "shards": n, "epoch": epoch,
            "orders": o["rows"], "payments": p["rows"],
            "order_amount_cents": sum(r["amount_cents"] for r in orders),
            "payment_amount_cents": sum(r["amount_cents"] for r in payments),
            "orders_sha256": o["sha256"], "payments_sha256": p["sha256"],
            "group": args.group, "try": args.try_no, "worker_pid": os.getpid(),
            "read_started_ts": t0, "read_finished_ts": t1,
        }
        util.write_json_atomic(os.path.join(shards_dir, f"shard-{k}.meta.json"), meta)
        conn.execute("COMMIT")
        util.touch(os.path.join(barrier_dir, f"{k}.done"))
        log(f"done: orders={o['rows']} payments={p['rows']} epoch={epoch}")
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        log(f"unexpected error: {exc!r}")
        return 6
    finally:
        conn.close()


def cmd_worker(args: argparse.Namespace) -> int:
    return run(args)
