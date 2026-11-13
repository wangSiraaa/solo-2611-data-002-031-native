"""Cross-checks: extracted slices parsed independently and rebuild-equivalence.

These tests combine build + extract and validate results with the
independent reference parser in ``csvref`` (whole-file, non-streaming) and
the standard library ``csv`` module.
"""

from __future__ import annotations

import os
import random
import sqlite3
import unittest

import csvref
from helpers import TempDirTestCase

from csvslice.errors import ColumnCountError, CsvSyntaxError, EmptyInputError
from csvslice.extract import extract_ranges
from csvslice.index import IndexReader, build_index


def make_source(tmpdir: str, name: str, data: bytes) -> tuple[str, str]:
    src = os.path.join(tmpdir, name)
    with open(src, "wb") as f:
        f.write(data)
    return src, src + ".idx"


class SliceContentTests(TempDirTestCase):
    """Extract every single record and compare parsed content to reference."""

    CASES = {
        "cjk_across_blocks": (
            'id,note\n0,"中文内容，复核材料"\n1,普通\n'.encode("utf-8"),
            3,  # 3 bytes per chunk splits inside multibyte characters
        ),
        "cr_lf_split": (b'id,note\r\n0,"a\r\nb"\r\n1,c\r\n', 2),
        "escaped_quote_split": (b'id,note\n0,"x""y""z"\n1,plain\n', 3),
        "multiline_field": (
            'id,note\n0,"第一行\n第二行\r\n第三行"\n1,z\n'.encode("utf-8"),
            5,
        ),
        "empty_fields": (b"a,b,c\n,,\n1,,3\n", 1),
        "header_only": (b"a,b\r\n", 1),
        "no_final_newline": (b"a,b\n1,2\n3,last", 2),
        "bom_crlf": (b"\xef\xbb\xbfa,b\r\n1,2\r\n", 1),
    }

    def test_each_slice_parses_like_reference(self):
        for name, (data, chunk) in self.CASES.items():
            with self.subTest(case=name):
                src, idx = make_source(self.tmpdir, name + ".csv", data)
                build_index(src, idx, chunk_bytes=chunk)
                bom, refs = csvref.split_records(data)
                header, rows = refs[0], refs[1:]
                with IndexReader(idx) as reader:
                    self.assertEqual(reader.bom_len, bom)
                    self.assertEqual(reader.header_fields, len(header.fields))
                    for i in range(reader.data_record_count):
                        out = self.path(f"{name}-{i}.out")
                        extract_ranges(src, idx, out, [f"{i}:{i+1}"])
                        slice_bytes = self.read(out)
                        _out_bom, out_refs = csvref.split_records(slice_bytes)
                        self.assertEqual(len(out_refs), 2)
                        self.assertEqual(out_refs[0].fields, header.fields)
                        self.assertEqual(out_refs[1].fields, rows[i].fields)
                        # And the stdlib agrees on the raw record.
                        std = csvref.parse_with_stdlib(rows[i].raw)
                        self.assertEqual(std, [rows[i].fields])

    def test_concatenated_slices_equal_raw_ranges(self):
        data = (
            'id,note,city\n'
            '0,"line1\nline2",北京\n'
            '1,"say ""hi""",上海\n'
            '2,,广州\n'
        ).encode("utf-8")
        src, idx = make_source(self.tmpdir, "cat.csv", data)
        build_index(src, idx, chunk_bytes=1)
        _, refs = csvref.split_records(data)
        header, rows = refs[0], refs[1:]
        whole = b"".join(
            [data[header.start : header.end]]
            + [data[r.start : r.end] for r in rows]
        )
        self.assertEqual(whole, data)

        # Every partition of the three records must reproduce the whole file
        # when the header is written once.
        for start in range(4):
            for stop in range(start, 4):
                with self.subTest(range=f"{start}:{stop}"):
                    out = self.path(f"cat-{start}-{stop}.out")
                    extract_ranges(src, idx, out, [f"{start}:{stop}"])
                    expected = data[header.start : header.end] + b"".join(
                        data[r.start : r.end] for r in rows[start:stop]
                    )
                    self.assertEqual(self.read(out), expected)

    def test_contiguous_runs_cover_the_file_exactly(self):
        rng = random.Random(7)
        for case in range(6):
            n = rng.randint(1, 8)
            lines = ["a,b"]
            for i in range(n):
                note = "x" if i % 2 else f'"多行{i}\n内容"'
                lines.append(f"{i},{note}")
            term = "\r\n" if case % 2 else "\n"
            data = term.join(lines) + ("" if case == 5 else term)
            data = data.encode("utf-8")
            src, idx = make_source(self.tmpdir, f"run{case}.csv", data)
            build_index(src, idx, chunk_bytes=rng.choice((1, 2, 3, 7, 64)))
            _, refs = csvref.split_records(data)
            header = refs[0]
            cumulative = 0
            for k in range(n + 1):
                out = self.path(f"run{case}-{k}.out")
                extract_ranges(src, idx, out, ["0:%d" % k])
                expected = header.raw + b"".join(r.raw for r in refs[1 : k + 1])
                self.assertEqual(self.read(out), expected)
                if k:
                    # Each step adds exactly one record.
                    prev = self.read(self.path(f"run{case}-{k-1}.out"))
                    self.assertTrue(self.read(out)[len(prev) :] == refs[k].raw)


class RebuildEquivalenceTests(TempDirTestCase):
    def test_all_chunk_sizes_produce_identical_index_rows(self):
        rng = random.Random(2024)
        lines = ["a,b,c"]
        for i in range(40):
            if i % 7 == 0:
                lines.append(f'{i},"多行\n字段{i}",v')
            elif i % 5 == 0:
                lines.append(f'{i},"含,逗号",v')
            elif i % 3 == 0:
                lines.append(f'{i},"引""号",v')
            else:
                lines.append(f"{i},,v")
        data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
        src, _ = make_source(self.tmpdir, "multi.csv", data)
        expected_rows = None
        expected_meta = None
        sizes = (1, 2, 3, 5, 8, 64, 1000, 65536)
        for chunk in sizes:
            idx = self.path(f"m{chunk}.idx")
            build_index(src, idx, chunk_bytes=chunk)
            conn = sqlite3.connect(idx)
            try:
                rows = conn.execute(
                    "SELECT record_no,start_off,end_off,term_len,fields FROM records ORDER BY record_no"
                ).fetchall()
                meta = conn.execute(
                    "SELECT key,value FROM meta WHERE key NOT IN ('index_checksum') ORDER BY key"
                ).fetchall()
            finally:
                conn.close()
            if expected_rows is None:
                expected_rows, expected_meta = rows, meta
            self.assertEqual(rows, expected_rows, f"rows differ at chunk={chunk}")
            self.assertEqual(meta, expected_meta, f"meta differs at chunk={chunk}")
            # And the offsets equal the independent parser's.
            _, refs = csvref.split_records(data)
            self.assertEqual(
                rows,
                [
                    (i, r.start, r.end, len(r.term), len(r.fields))
                    for i, r in enumerate(refs[1:])
                ],
            )


class ErrorPositionMatrixTests(TempDirTestCase):
    """Build every illegal-quote pattern and assert the reported location."""

    CASES = [
        # (data, expected offset, expected substring, expected data record no)
        (b'a,b\n1,"abc', 6, "unterminated", 0),
        (b'a,b\n1,"abc\ndef', 6, "unterminated", 0),
        (b'a,b\n1,"abc\n2,3\n', 6, "unterminated", 0),
        (b'a,b\n1,x"y\n', 7, "unquoted", 0),
        (b'a,b\n1, "x"\n', 7, "unquoted", 0),
        (b'a,b\n1,"x"y\n', 9, "closing quote", 0),
        (b'a,b\n1,"x" y\n', 9, "closing quote", 0),
        (b'a,b\n1,"x""\n', 6, "unterminated", 0),
        (b'a,b\n1,2\n3,"bad"x\n', 15, "closing quote", 1),
        (b'a,b\n1,2\n3,4\n5,6"7\n', 15, "unquoted", 2),
        (b'a,"h\n', 2, "unterminated", None),  # header error
        (b'a,b"c\n', 3, "unquoted", None),
        (b'a,"b"c\n', 5, "closing quote", None),
    ]

    def test_illegal_quote_positions(self):
        for data, offset, needle, record_no in self.CASES:
            for chunk in (1, 2, 3, 5, 64):
                with self.subTest(data=data, chunk=chunk):
                    src, idx = make_source(
                        self.tmpdir, f"e_{abs(hash((data, chunk)))}.csv", data
                    )
                    with self.assertRaises(CsvSyntaxError) as ctx:
                        build_index(src, idx, chunk_bytes=chunk)
                    exc = ctx.exception
                    self.assertEqual(exc.offset, offset, str(exc))
                    self.assertIn(needle, str(exc))
                    self.assertEqual(exc.record_no, record_no, str(exc))
                    self.assertEqual(exc.is_header, record_no is None, str(exc))
                    self.assertFalse(os.path.exists(idx))

    def test_empty_file_error_mentions_header(self):
        src, idx = make_source(self.tmpdir, "empty.csv", b"")
        with self.assertRaises(EmptyInputError) as ctx:
            build_index(src, idx)
        self.assertIn("header", str(ctx.exception))

    def test_column_mismatch_reports_offset_and_number(self):
        data = b"a,b,c\n1,2,3\n4,5,6\n7,8\n"
        src, idx = make_source(self.tmpdir, "cols.csv", data)
        with self.assertRaises(ColumnCountError) as ctx:
            build_index(src, idx)
        self.assertEqual(ctx.exception.record_no, 2)
        self.assertEqual(ctx.exception.offset, len(b"a,b,c\n1,2,3\n4,5,6\n"))


if __name__ == "__main__":
    unittest.main()
