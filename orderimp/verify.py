"""Self-check suite.

Generates every synthetic sample locally, imports them, and verifies:

* forward references (order before its customer)
* same customer id in different batches (scoped identity)
* nested batches; orders never inherit parent-batch customers
* namespace prefix substitution (identical result)
* extension subtrees / pseudo orders ignored
* bad line -> whole order quarantined (no surviving lines)
* file-tail truncation and other document-level failures
* old version stays readable after a failed import
* chunk-size invariance of the published output
* peak RSS of a large (>128 MiB) import stays under 96 MiB
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import time

from .api import import_file
from .errors import DocumentError
from .generate import generate_all

RSS_LIMIT_KB = 96 * 1024  # 96 MiB

# Hand-computed expectations for small.xml (see generate.py header).
SMALL_EXPECT = {
    "customers": 4, "orders": 4, "lines": 5,
    "rejects": 6, "batches": 3,
}
SMALL_TOTAL_CENTS = 721
SMALL_ORDER_TOTALS = {  # (batch_id, order_id) -> total_cents
    ("b1", "o1"): 300, ("b1", "o2"): 21,
    ("b1a", "o1"): 150, ("b2", "o9"): 250,
}
SMALL_REJECTS = [  # (kind, batch_id, record_id, seq, reason)
    ("order", "b1a", "oX", 7, "unknown_customer"),
    ("order", "b1a", "oP", 8, "unknown_customer"),
    ("order", "b2", "oBad", 11, "bad_line"),
    ("customer", "b2", "cBad", 12, "missing_name"),
    ("order", "b2", "oUseBad", 13, "customer_rejected"),
    ("order", "b2", "oEmpty", 14, "no_lines"),
]

# Hand-computed expectations for bad_records.xml (24 records, 22 rejects).
BAD_EXPECT_REASONS = {
    "bad_line": 10, "no_lines": 1, "missing_id": 2,
    "missing_customer_id": 1, "nested_order": 1, "nested_batch": 1,
    "missing_name": 1, "customer_rejected": 2, "unknown_customer": 1,
    "subtree_too_large": 2,
}

DOCFAILURE_FILES = [
    "truncated.xml", "dtd.xml", "entity_ref.xml", "dup_batch.xml",
    "dup_customer.xml", "dup_order.xml", "bad_root.xml", "wrong_ns.xml",
    "no_ns_root.xml", "outside_batch.xml", "junk_after.xml",
    "mismatched.xml", "bad_encoding.xml", "deep_fail.xml",
]


class Suite:
    def __init__(self):
        self.failures = 0
        self.checks = 0

    def check(self, cond, label, extra=""):
        self.checks += 1
        if cond:
            print(f"  PASS  {label}")
        else:
            self.failures += 1
            print(f"  FAIL  {label}  {extra}")


def _read_jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _current_dir(data_dir):
    cur = os.path.join(data_dir, "current")
    return os.path.realpath(cur) if os.path.lexists(cur) else None


def _manifest(data_dir):
    with open(os.path.join(data_dir, "current", "manifest.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def run_verify(work_dir, skip_big=False, big_orders=1_200_000, seed=7):
    s = Suite()
    samples = os.path.join(work_dir, "samples")
    data_root = os.path.join(work_dir, "data")
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(samples)

    print("== generate synthetic samples ==")
    generate_all(samples, big_orders=big_orders, seed=seed,
                 write_big_file=not skip_big)
    print(f"  samples written to {samples}")

    # ------------------------------------------------------------------ small
    print("== small.xml: forward refs, scoped ids, nested batches, quarantine ==")
    d = os.path.join(data_root, "small")
    m = import_file(os.path.join(samples, "small.xml"), d, chunk_size=65536)
    s.check(m["counts"] == SMALL_EXPECT and m["total_cents"] == SMALL_TOTAL_CENTS,
            "small.xml counts and grand total match hand-computed values",
            f"got {m['counts']} / {m['total_cents']}")
    orders = _read_jsonl(os.path.join(d, "current", "orders.jsonl"))
    got_totals = {(o["batch_id"], o["order_id"]): o["total_cents"] for o in orders}
    s.check(got_totals == SMALL_ORDER_TOTALS,
            "small.xml per-order totals match hand computation",
            f"got {got_totals}")
    rejects = _read_jsonl(os.path.join(d, "current", "rejects.jsonl"))
    got_rejects = [(r["kind"], r["batch_id"], r["record_id"], r["seq"], r["reason"])
                   for r in rejects]
    s.check(got_rejects == SMALL_REJECTS,
            "small.xml rejects (id, batch, seq, reason) match",
            f"got {got_rejects}")
    lines = _read_jsonl(os.path.join(d, "current", "lines.jsonl"))
    s.check(all(l["order_id"] != "oBad" for l in lines),
            "quarantined order oBad kept no lines")
    s.check(not any(o["order_id"].startswith("FAKE") for o in orders),
            "pseudo orders inside extension subtree were ignored")

    # ------------------------------------------------- prefix substitution
    print("== small_prefixed.xml: namespace prefix substitution ==")
    d2 = os.path.join(data_root, "small_prefixed")
    import_file(os.path.join(samples, "small_prefixed.xml"), d2, chunk_size=4096)
    same = all(
        open(os.path.join(d, "current", f), "rb").read()
        == open(os.path.join(d2, "current", f), "rb").read()
        for f in ("customers.jsonl", "orders.jsonl", "lines.jsonl", "rejects.jsonl")
    )
    s.check(same, "prefixed document produces byte-identical output")

    # ------------------------------------------------------- chunk invariance
    print("== chunk-size invariance ==")
    variants = {}
    for cs in (1, 13, 1024, 65536, 1 << 20):
        dv = os.path.join(data_root, f"chunk_{cs}")
        import_file(os.path.join(samples, "small.xml"), dv, chunk_size=cs)
        variants[cs] = [
            open(os.path.join(dv, "current", f), "rb").read()
            for f in ("customers.jsonl", "orders.jsonl", "lines.jsonl", "rejects.jsonl")
        ]
    s.check(all(v == variants[1] for v in variants.values()),
            "identical output for chunk sizes 1 / 13 / 1K / 64K / 1M")
    # bad_records exercises the 64 KiB subtree guard across chunk boundaries
    v7 = os.path.join(data_root, "bad_7")
    vbig = os.path.join(data_root, "bad_1m")
    import_file(os.path.join(samples, "bad_records.xml"), v7, chunk_size=7)
    import_file(os.path.join(samples, "bad_records.xml"), vbig, chunk_size=1 << 20)
    same = all(
        open(os.path.join(v7, "current", f), "rb").read()
        == open(os.path.join(vbig, "current", f), "rb").read()
        for f in ("customers.jsonl", "orders.jsonl", "lines.jsonl", "rejects.jsonl")
    )
    s.check(same, "bad_records identical for chunk sizes 7 and 1M")

    # -------------------------------------------------------------- bad records
    print("== bad_records.xml: record-level quarantine ==")
    m = _manifest(vbig)
    s.check(m["counts"]["customers"] == 1 and m["counts"]["orders"] == 1
            and m["counts"]["lines"] == 1 and m["total_cents"] == 1,
            "bad_records: only the one clean order/customer survives",
            f"got {m['counts']}")
    s.check(m["counts"]["rejects"] == 22,
            "bad_records: 22 rejects (hand-computed)",
            f"got {m['counts']['rejects']}")
    s.check(m["rejects_by_reason"] == BAD_EXPECT_REASONS,
            "bad_records: reject reasons match hand computation",
            f"got {m['rejects_by_reason']}")
    s.check(m["counts"]["batches"] == 1,
            "batch nested inside an order was ignored, not registered")

    # -------------------------------------------------------------- extension
    print("== extension.xml: foreign namespaces ignored ==")
    de = os.path.join(data_root, "ext")
    m = import_file(os.path.join(samples, "extension.xml"), de)
    s.check(m["counts"]["customers"] == 1 and m["counts"]["orders"] == 1
            and m["counts"]["rejects"] == 0 and m["total_cents"] == 10,
            "only the real order survives; pseudo orders ignored",
            f"got {m['counts']}")
    s.check(m["diagnostics"]["extension_subtrees_ignored"] >= 2
            and m["diagnostics"]["unknown_business_subtrees_ignored"] >= 1,
            "extension/unknown subtrees counted in diagnostics")

    # ------------------------------------------------------------------ depth
    print("== nesting depth limit ==")
    dd = os.path.join(data_root, "deep_ok")
    m = import_file(os.path.join(samples, "deep_ok.xml"), dd)
    s.check(m["counts"]["batches"] == 29 and m["counts"]["orders"] == 1,
            "depth 32 (29 nested batches) accepted")

    # ------------------------------------------------------- document failures
    print("== document-level failures: nothing published ==")
    for name in DOCFAILURE_FILES:
        df = os.path.join(data_root, f"fail_{name}")
        try:
            import_file(os.path.join(samples, name), df)
        except DocumentError as exc:
            s.check(not os.path.lexists(os.path.join(df, "current")),
                    f"{name}: rejected as document failure ({exc})", "")
            continue
        except Exception as exc:  # noqa: BLE001
            s.check(False, f"{name}: wrong exception type",
                    f"{type(exc).__name__}: {exc}")
            continue
        s.check(False, f"{name}: import unexpectedly succeeded")

    # ------------------------------------------- version retention on failure
    print("== failed import keeps previous version readable ==")
    dv = os.path.join(data_root, "versions")
    m1 = import_file(os.path.join(samples, "small.xml"), dv)
    before = _current_dir(dv)
    try:
        import_file(os.path.join(samples, "truncated.xml"), dv)
        s.check(False, "truncated import should have failed")
    except DocumentError:
        pass
    s.check(_current_dir(dv) == before, "current symlink unchanged after failure")
    m_again = _manifest(dv)
    s.check(m_again["counts"] == m1["counts"],
            "previous version still fully readable after failure")
    m2 = import_file(os.path.join(samples, "extension.xml"), dv)
    s.check(m2["version"] != m1["version"]
            and _current_dir(dv) != before
            and os.path.isdir(before),
            "next successful import publishes a new version, old one kept")

    # ------------------------------------------------------------------- big
    if not skip_big:
        print("== big.xml: >128 MiB file, peak RSS must stay < 96 MiB ==")
        big_xml = os.path.join(samples, "big.xml")
        size_mb = os.path.getsize(big_xml) / (1 << 20)
        print(f"  big.xml is {size_mb:.1f} MiB")
        s.check(size_mb > 128, "big sample exceeds 128 MiB")
        dbig = os.path.join(data_root, "big")
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "orderimp", "import", big_xml,
             "--data-dir", dbig, "--chunk-size", str(1 << 20)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True,
        )
        dt = time.monotonic() - t0
        rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        print(f"  import took {dt:.1f}s, peak RSS {rss_kb / 1024:.1f} MiB")
        s.check(proc.returncode == 0, "big import succeeded",
                proc.stdout + proc.stderr)
        s.check(rss_kb < RSS_LIMIT_KB,
                f"peak RSS {rss_kb / 1024:.1f} MiB under 96 MiB limit")
        with open(big_xml + ".expected.json", encoding="utf-8") as fh:
            expected = json.load(fh)
        mbig = _manifest(dbig)
        counts_ok = all(mbig["counts"][k] == expected[k]
                        for k in ("customers", "orders", "lines", "batches", "rejects"))
        s.check(counts_ok and mbig["total_cents"] == expected["total_cents"],
                "big import counts and grand total match generator",
                f"got {mbig['counts']} / {mbig['total_cents']}")
        print(f"  big.xml: {expected['orders']:,} orders, "
              f"{expected['lines']:,} lines, total {expected['total_cents']:,} cents")

    print(f"\n{'=' * 60}\n{s.checks} checks, {s.failures} failure(s)")
    return 1 if s.failures else 0
