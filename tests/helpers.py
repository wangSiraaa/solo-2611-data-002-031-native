"""Shared helpers for the csvslice test suite."""

from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import csvref  # noqa: E402

from csvslice.scanner import Scanner  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="csvslice-test-")
        self.tmpdir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def write(self, name: str, data: bytes) -> str:
        p = self.path(name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def read(self, p: str) -> bytes:
        with open(p, "rb") as f:
            return f.read()


def scan_bytes(data: bytes, chunk: int, *, bom_len: int | None = None):
    """Run the production scanner over ``data`` in fixed-size chunks.

    Returns the records with offsets relative to the start of ``data``
    (the BOM, if any, is skipped exactly as ``build`` does).
    """
    if bom_len is None:
        bom_len = 3 if data.startswith(csvref.BOM) else 0
    body = data[bom_len:]
    records = []
    scanner = Scanner(records.append, start_offset=0)
    off = 0
    while off < len(body):
        step = min(chunk, len(body) - off)
        scanner.feed(body[off : off + step], off)
        off += step
    scanner.finish(len(body))
    if bom_len:
        records = [
            dataclasses.replace(rec, start=rec.start + bom_len, end=rec.end + bom_len)
            for rec in records
        ]
    return records


def assert_matches_reference(test: unittest.TestCase, data: bytes, chunk: int) -> None:
    bom, refs = csvref.split_records(data)
    got = scan_bytes(data, chunk)
    test.assertEqual(len(got), len(refs), f"record count differs at chunk={chunk}")
    for got_r, ref_r in zip(got, refs):
        test.assertEqual(
            (got_r.start, got_r.end, got_r.term_len),
            (ref_r.start, ref_r.end, len(ref_r.term)),
            f"boundaries differ at chunk={chunk} for record {ref_r.start}",
        )
        test.assertEqual(got_r.fields, len(ref_r.fields), f"field count differs at chunk={chunk}")


def corpus() -> list[bytes]:
    """Hand-built fixtures used by several tests."""
    return [
        b"a,b\n1,2\n",
        b"a,b\n1,2",  # no final newline
        b"a,b\r\n1,2\r\n",
        b"a,b\r\n1,2",  # CRLF file, unterminated last
        b"a,b\n1,\n\n",  # empty record at end
        b"a,b\n,\n",  # empty fields
        b"a,b\n\n1,2\n",  # empty record in the middle
        b"a,b\n1,2\r3,4\n",  # lone CR terminator
        b"a,b\n1,2\r",  # file ends with lone CR (CR is the terminator)
        b'a,b\n1,"x\ny"\n2,z\n',  # newline in quoted field
        b'a,b\r\n1,"line1\r\nline2\r\nline3"\r\n2,z\r\n',  # CRLF inside quotes
        b'a,b\n1,"x\r\ny"\n',  # CRLF in quoted field inside LF file
        b'a,b\n1,"say ""hi"""\n',  # escaped quotes
        b'a,b\n1,""""""\n',  # field == two quotes
        b'a,b\n1,"a,b,c"\n',  # quoted commas
        b'a,b\n1,"a ""b"", c"\n',  # escaped quotes plus comma
        'a,b\n1,"日本語\n中文字符",\n'.encode("utf-8"),  # CJK spanning lines/trailing empty
        b"\xef\xbb\xbfa,b\n1,2\n",  # BOM
        b"\xef\xbb\xbfa,b\r\n1,2",  # BOM + CRLF + no final newline
        b"a\n1\n2\n3\n",  # single column
        b"a,b,c\n1,2,3\n4,5,6\n",
        b'a,b\n1,"unterminated',  # malformed: used by error tests too
    ]


VALID_CORPUS = [c for c in corpus() if b"unterminated" not in c]
