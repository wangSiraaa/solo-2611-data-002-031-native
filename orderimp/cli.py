"""Command line interface: generate / import / inspect / verify."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from . import VERSION
from .api import import_file
from .errors import DocumentError
from .generate import generate_all
from .verify import run_verify

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DOCUMENT = 2  # document-level failure; nothing published


def _cmd_generate(args) -> int:
    written = generate_all(args.out, big_orders=args.big_orders, seed=args.seed,
                           write_big_file=not args.skip_big)
    for path in written:
        size = os.path.getsize(path)
        print(f"wrote {path} ({size:,} bytes)")
    return EXIT_OK


def _cmd_import(args) -> int:
    if not (1 <= args.chunk_size <= 16 * 1024 * 1024):
        print("error: --chunk-size must be between 1 and 16777216", file=sys.stderr)
        return EXIT_ERROR
    try:
        manifest = import_file(
            args.file, args.data_dir,
            chunk_size=args.chunk_size,
            max_depth=args.max_depth,
            max_record_bytes=args.max_record_bytes,
            keep_staging=args.keep_staging,
        )
    except DocumentError as exc:
        print(json.dumps({"status": "document_failure", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_DOCUMENT
    except OSError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}),
              file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps({
        "status": "ok",
        "version": manifest["version"],
        "counts": manifest["counts"],
        "total_cents": manifest["total_cents"],
        "rejects_by_reason": manifest["rejects_by_reason"],
    }, ensure_ascii=False))
    return EXIT_OK


def _cmd_inspect(args) -> int:
    cur = os.path.join(args.data_dir, "current")
    if not os.path.lexists(cur):
        print(f"no published version in {args.data_dir}", file=sys.stderr)
        return EXIT_ERROR
    with open(os.path.join(cur, "manifest.json"), encoding="utf-8") as fh:
        m = json.load(fh)
    print(f"version        : {m['version']}  (created {m['created_at']})")
    print(f"source         : {m['source_file']}")
    print(f"  sha256       : {m['source_sha256']}  ({m['source_bytes']:,} bytes)")
    print(f"limits         : depth<={m['limits']['max_depth']}, "
          f"record<={m['limits']['max_record_bytes']} bytes, "
          f"chunk={m['chunk_size']}")
    c = m["counts"]
    print(f"counts         : customers={c['customers']} orders={c['orders']} "
          f"lines={c['lines']} rejects={c['rejects']} batches={c['batches']}")
    print(f"total_cents    : {m['total_cents']}")
    d = m["diagnostics"]
    print(f"diagnostics    : extension_subtrees={d['extension_subtrees_ignored']} "
          f"unknown_business={d['unknown_business_subtrees_ignored']} "
          f"stray_lines={d['stray_lines_ignored']}")
    if m["rejects_by_reason"]:
        print("rejects/reason : " + ", ".join(
            f"{k}={v}" for k, v in m["rejects_by_reason"].items()))
    for name in ("customers", "orders", "lines", "rejects"):
        path = os.path.join(cur, f"{name}.jsonl")
        print(f"-- {name}.jsonl (first {args.limit}) --")
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= args.limit:
                    break
                print("  " + line.rstrip())
    return EXIT_OK


def _cmd_verify(args) -> int:
    return run_verify(args.work_dir, skip_big=args.skip_big,
                      big_orders=args.big_orders, seed=args.seed)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="importer",
        description="Streaming importer for nested-batch order XML exports "
                    "(namespace urn:orders:v1).",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic sample files")
    g.add_argument("--out", default="samples", help="output directory")
    g.add_argument("--big-orders", type=int, default=1_200_000,
                   help="number of orders in big.xml (default 1.2M, >128 MiB)")
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--skip-big", action="store_true",
                   help="do not generate big.xml")
    g.set_defaults(func=_cmd_generate)

    i = sub.add_parser("import", help="import one XML file and publish a version")
    i.add_argument("file", help="XML file to import")
    i.add_argument("--data-dir", default="data", help="version store directory")
    i.add_argument("--chunk-size", type=int, default=65536,
                   help="read block size in bytes (default 65536)")
    i.add_argument("--max-depth", type=int, default=32)
    i.add_argument("--max-record-bytes", type=int, default=64 * 1024)
    i.add_argument("--keep-staging", action="store_true",
                   help="keep the scratch SQLite file for debugging")
    i.set_defaults(func=_cmd_import)

    n = sub.add_parser("inspect", help="show the currently published version")
    n.add_argument("--data-dir", default="data")
    n.add_argument("--limit", type=int, default=5,
                   help="rows to sample from each dataset")
    n.set_defaults(func=_cmd_inspect)

    v = sub.add_parser("verify", help="run the self-check suite")
    v.add_argument("--work-dir", default="verify_workspace")
    v.add_argument("--skip-big", action="store_true",
                   help="skip the >128 MiB memory test")
    v.add_argument("--big-orders", type=int, default=1_200_000)
    v.add_argument("--seed", type=int, default=7)
    v.set_defaults(func=_cmd_verify)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
