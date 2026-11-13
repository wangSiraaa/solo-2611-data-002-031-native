"""Extract requested record ranges into a new CSV atomically.

The flow is:

1. open the index read-only and verify its self-checksum and structure;
2. hash the whole source once and compare with the index identity;
3. stream the header once, then copy only the requested data ranges;
4. write everything to a temporary file, fsync, and atomically rename it.

Duplicate ranges are copied again (the original bytes are re-read), so the
output preserves duplicates in request order.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Callable

from .errors import ExtractRangeError, OutputConflictError
from .index import IndexReader, check_source_identity



@dataclass
class ExtractResult:
    output_path: str
    ranges: list[tuple[int, int]]
    records_written: int
    bytes_written: int
    header_bytes: int
    bom_bytes: int
    padded_terminator: bool
    padding_offset_in_output: int | None
    source_bytes_read_for_records: int
    source_read_ops: int


def parse_ranges(specs: list[str]) -> list[tuple[int, int]]:
    """Parse ``START:STOP`` intervals (0-based, half-open)."""
    ranges: list[tuple[int, int]] = []
    for spec in specs:
        text = spec.strip()
        if ":" not in text:
            raise ExtractRangeError(
                f"invalid range {spec!r}: expected START:STOP, e.g. 0:10"
            )
        left, right = text.split(":", 1)
        try:
            start = int(left)
            stop = int(right)
        except ValueError as exc:
            raise ExtractRangeError(
                f"invalid range {spec!r}: boundaries must be integers"
            ) from exc
        if start < 0 or stop < 0:
            raise ExtractRangeError(f"invalid range {spec!r}: boundaries must be >= 0")
        if start > stop:
            raise ExtractRangeError(f"invalid range {spec!r}: START must be <= STOP")
        ranges.append((start, stop))
    if not ranges:
        raise ExtractRangeError("at least one record range is required")
    return ranges


def _validate_ranges_against_index(
    ranges: list[tuple[int, int]], data_record_count: int
) -> None:
    for start, stop in ranges:
        if stop > data_record_count:
            raise ExtractRangeError(
                f"range {start}:{stop} is out of bounds: the source has "
                f"{data_record_count} data record(s), valid stop is <= {data_record_count}"
            )


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def extract_ranges(
    source_path: str,
    index_path: str,
    output_path: str,
    specs: list[str],
    *,
    open_hook: Callable[[str, str], BinaryIO] | None = None,
    reader_factory: Callable[[BinaryIO], BinaryIO] | None = None,
    force: bool = False,
) -> ExtractResult:
    """Write the requested record ranges to ``output_path``.

    ``open_hook`` / ``reader_factory`` exist so tests can wrap the source file
    and record exactly which source bytes extraction reads after the identity
    check; production code uses the defaults.
    """
    ranges = parse_ranges(specs)
    source_abs = os.path.abspath(source_path)
    index_abs = os.path.abspath(index_path)
    out_abs = os.path.abspath(output_path)
    if out_abs == source_abs:
        raise OutputConflictError("refusing to overwrite the source file")
    if out_abs == index_abs:
        raise OutputConflictError("refusing to overwrite the index file")
    if os.path.isdir(out_abs):
        raise OutputConflictError(f"output path is a directory: {output_path}")
    if os.path.exists(out_abs) and not force:
        raise OutputConflictError(
            f"output file already exists: {output_path} (pass --force to replace it)"
        )
    out_dir = os.path.dirname(out_abs)
    if not os.path.isdir(out_dir):
        raise OutputConflictError(f"output directory does not exist: {out_dir}")

    reader = IndexReader(index_path, verify=True)
    try:
        _validate_ranges_against_index(ranges, reader.data_record_count)
        # Full identity check: the index must describe the current file.
        check_source_identity(reader.meta, source_path)

        opener = open_hook or (lambda mode, path: open(path, mode))
        nonempty_after = [False] * len(ranges)
        seen_nonempty = False
        for idx in range(len(ranges) - 1, -1, -1):
            nonempty_after[idx] = seen_nonempty
            if ranges[idx][0] < ranges[idx][1]:
                seen_nonempty = True
        stats = {"record_bytes": 0, "read_ops": 0}

        with opener("rb", source_path) as raw:
            src: BinaryIO = reader_factory(raw) if reader_factory else raw
            fd, tmp_path = tempfile.mkstemp(
                prefix=".csvslice-extract-", suffix=".tmp", dir=out_dir
            )
            try:
                with os.fdopen(fd, "wb") as dst:
                    header_bytes = _copy_range(src, dst, 0, reader.header_end)
                    bytes_written = header_bytes
                    records_written = 0
                    padded = False
                    padding_at: int | None = None

                    for range_idx, (start, stop) in enumerate(ranges):
                        for record_no, rec_start, rec_end, term_len in reader.iter_records(
                            start, stop
                        ):
                            src.seek(rec_start)
                            remaining = rec_end - rec_start
                            if remaining:
                                data = src.read(remaining)
                                if len(data) != remaining:
                                    raise ExtractRangeError(
                                        f"short read for record {record_no} at byte {rec_start}"
                                    )
                                dst.write(data)
                                stats["record_bytes"] += remaining
                                stats["read_ops"] += 1
                                bytes_written += remaining
                            records_written += 1
                            # An unterminated EOF record placed anywhere but the
                            # very end would concatenate with the next record.
                            # Add exactly one LF and record that we did.
                            if rec_end == reader.source_size and term_len == 0:
                                is_last_here = record_no + 1 == stop
                                if not is_last_here or nonempty_after[range_idx]:
                                    dst.write(b"\n")
                                    bytes_written += 1
                                    padded = True
                                    padding_at = bytes_written - 1
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(tmp_path, out_abs)
                _fsync_dir(out_dir)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    finally:
        reader.close()

    return ExtractResult(
        output_path=output_path,
        ranges=ranges,
        records_written=records_written,
        bytes_written=bytes_written,
        header_bytes=header_bytes,
        bom_bytes=reader.bom_len,
        padded_terminator=padded,
        padding_offset_in_output=padding_at,
        source_bytes_read_for_records=stats["record_bytes"],
        source_read_ops=stats["read_ops"],
    )


def _copy_range(src: BinaryIO, dst: BinaryIO, start: int, end: int) -> int:
    src.seek(start)
    remaining = end - start
    total = 0
    while remaining > 0:
        data = src.read(min(remaining, 1 << 20))
        if not data:
            raise ExtractRangeError(f"unexpected end of file reading bytes {start}:{end}")
        dst.write(data)
        total += len(data)
        remaining -= len(data)
    return total
