"""Command line interface: ``generate``, ``build``, ``extract``, ``verify``."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Sequence

from .errors import CsvSliceError
from .extract import extract_ranges
from .index import (
    DEFAULT_CHUNK_BYTES,
    MAX_RECORD_BYTES,
    build_index,
    verify_index,
)
from .generate import GenerateSpec, generate_csv


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _record_limit(text: str) -> int:
    value = _positive_int(text)
    if value > MAX_RECORD_BYTES:
        raise argparse.ArgumentTypeError(
            f"must not exceed the hard limit of {MAX_RECORD_BYTES} bytes (1 MiB)"
        )
    return value


def _size(text: str) -> int:
    """Parse ``1234``, ``512KiB``, ``512K``, ``64MB``, ``1GiB``."""
    raw = text.strip().lower()
    units = {
        "": 1,
        "b": 1,
        "k": 1 << 10,
        "kb": 1 << 10,
        "kib": 1 << 10,
        "m": 1 << 20,
        "mb": 1 << 20,
        "mib": 1 << 20,
        "g": 1 << 30,
        "gb": 1 << 30,
        "gib": 1 << 30,
    }
    for suffix in ("gib", "gb", "mib", "mb", "kib", "kb", "k", "m", "g", "b"):
        if raw.endswith(suffix) and raw[: -len(suffix)].strip():
            number = raw[: -len(suffix)].strip()
            break
    else:
        number = raw
        suffix = ""
    try:
        value = int(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return value * units[suffix]


def _ranges(text: str) -> list[str]:
    items = [part for part in text.replace(",", " ").split() if part]
    if not items:
        raise argparse.ArgumentTypeError("at least one range is required")
    return items


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _cmd_generate(args: argparse.Namespace) -> int:
    if args.target_size:
        spec = GenerateSpec(
            target_size=args.target_size,
            chunk_bytes=args.chunk_size,
            seed=args.seed,
        )
    else:
        spec = GenerateSpec(
            rows=args.rows,
            chunk_bytes=args.chunk_size,
            seed=args.seed,
            terminate_last=not args.no_final_newline,
            include_bom=args.bom,
            multiline_every=args.multiline_every,
            quoted_comma_every=args.quoted_comma_every,
            quoted_quote_every=args.quoted_quote_every,
        )
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    result = generate_csv(args.output, spec)
    _emit(
        {
            "command": "generate",
            "output": args.output,
            "bytes": result.bytes_written,
            "data_records": result.data_records,
            "bom": result.has_bom,
            "seed": result.seed,
        }
    )
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    result = build_index(
        args.source,
        args.index,
        chunk_bytes=args.chunk_size,
        max_record_bytes=args.max_record_bytes,
    )
    _emit(
        {
            "command": "build",
            "source": result.source_path,
            "index": result.index_path,
            "source_size": result.source_size,
            "source_sha256": result.source_sha256,
            "format_version": "1",
            "chunk_bytes": result.chunk_bytes,
            "bom_bytes": result.bom_len,
            "header_start": result.header_start,
            "header_end": result.header_end,
            "header_fields": result.header_fields,
            "data_records": result.data_record_count,
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_index(args.index, args.source)
    _emit(
        {
            "command": "verify",
            "index": result.index_path,
            "source_size": result.source_size,
            "source_sha256": result.source_sha256,
            "data_records": result.data_record_count,
            "header_fields": result.header_fields,
            "source_checksum_checked": result.source_checksum_checked,
            "source_checksum_ok": result.source_checksum_ok,
            "format_version": "1",
            "status": "ok",
        }
    )
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    result = extract_ranges(
        args.source,
        args.index,
        args.output,
        args.ranges,
        force=args.force,
    )
    payload = {
        "command": "extract",
        "source": args.source,
        "index": args.index,
        "output": args.output,
        "ranges": [f"{a}:{b}" for a, b in result.ranges],
        "records_written": result.records_written,
        "bytes_written": result.bytes_written,
        "header_bytes": result.header_bytes,
        "bom_bytes": result.bom_bytes,
        "source_bytes_read_for_records": result.source_bytes_read_for_records,
        "source_read_ops": result.source_read_ops,
        "terminator_padding": (
            {
                "added_bytes": b"\n".hex(),
                "offset_in_output": result.padding_offset_in_output,
                "note": "unterminated end-of-file record was not last in output; "
                "one LF was appended to keep records separate",
            }
            if result.padded_terminator
            else None
        ),
        "status": "ok",
    }
    _emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csvslice",
        description=(
            "Local CSV record indexer and range extractor.  Records are "
            "physical CSV records; newlines inside quoted fields belong to "
            "the field and never start a new record."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate a local sample CSV")
    g.add_argument("output", help="path of the CSV to write")
    g.add_argument(
        "--rows", type=_positive_int, default=20, help="number of data records"
    )
    g.add_argument(
        "--target-size",
        type=_size,
        default=None,
        help="grow the file to about this size (e.g. 130MiB) instead of --rows",
    )
    g.add_argument(
        "--chunk-size",
        type=_size,
        default=None,
        help="approximate bytes per generated data record when using --target-size",
    )
    g.add_argument("--seed", type=int, default=1234, help="PRNG seed")
    g.add_argument("--bom", action="store_true", help="write a UTF-8 BOM")
    g.add_argument(
        "--no-final-newline",
        action="store_true",
        help="leave the last record unterminated",
    )
    g.add_argument(
        "--multiline-every",
        type=_positive_int,
        default=0,
        help="make every Nth record a quoted field containing newlines",
    )
    g.add_argument(
        "--quoted-comma-every",
        type=_positive_int,
        default=0,
        help="make every Nth record quote a field containing a comma",
    )
    g.add_argument(
        "--quoted-quote-every",
        type=_positive_int,
        default=0,
        help="make every Nth record quote a field containing an escaped pair of quotes",
    )
    g.set_defaults(func=_cmd_generate)

    b = sub.add_parser("build", help="stream the CSV and publish an index")
    b.add_argument("source", help="CSV source file")
    b.add_argument("index", help="index database to create")
    b.add_argument(
        "--chunk-size",
        type=_positive_int,
        default=DEFAULT_CHUNK_BYTES,
        help=f"read/scan block size in bytes (default {DEFAULT_CHUNK_BYTES})",
    )
    b.add_argument(
        "--max-record-bytes",
        type=_record_limit,
        default=MAX_RECORD_BYTES,
        help=f"logical record cap (default and maximum {MAX_RECORD_BYTES} = 1 MiB)",
    )
    b.set_defaults(func=_cmd_build)

    e = sub.add_parser("extract", help="copy record ranges using an index")
    e.add_argument("source", help="CSV source file")
    e.add_argument("index", help="index database built from the source")
    e.add_argument("output", help="destination CSV (written atomically)")
    e.add_argument(
        "--ranges",
        "-r",
        type=_ranges,
        required=True,
        help="one or more START:STOP half-open record ranges (comma or space separated)",
    )
    e.add_argument(
        "--force", action="store_true", help="replace an existing output file"
    )
    e.set_defaults(func=_cmd_extract)

    v = sub.add_parser("verify", help="check index integrity and source identity")
    v.add_argument("index", help="index database")
    v.add_argument(
        "--source",
        default=None,
        help="also hash this source and compare it with the index",
    )
    v.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CsvSliceError as exc:
        sys.stderr.write(f"csvslice: error: {exc}\n")
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(f"csvslice: error: {exc}\n")
        return 1
    except BrokenPipeError:  # pragma: no cover
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
