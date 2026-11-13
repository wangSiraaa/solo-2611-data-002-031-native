"""Chunk-boundary and grammar tests for the streaming scanner."""

from __future__ import annotations

import unittest

import csvref
from helpers import (
    VALID_CORPUS,
    assert_matches_reference,
    corpus,
    scan_bytes,
)

from csvslice.errors import CsvSyntaxError
from csvslice.scanner import Scanner

# One byte at a time, odd/even splits, tiny and large blocks.
CHUNK_SIZES = (1, 2, 3, 4, 5, 7, 8, 13, 64, 1024)


class ScannerEquivalenceTests(unittest.TestCase):
    def test_corpus_matches_reference_for_all_chunk_sizes(self):
        for data in VALID_CORPUS:
            for chunk in CHUNK_SIZES:
                with self.subTest(data=data[:30], chunk=chunk):
                    assert_matches_reference(self, data, chunk)

    def test_chinese_multibyte_crosses_every_block_boundary(self):
        # "中" is 3 bytes; feed one byte at a time so every split point inside
        # the multibyte character is exercised.
        data = 'a,b\n1,"中文内容"\n2,"复核材料，含逗号"\n'.encode("utf-8")
        for chunk in (1, 2, 4, 5):
            with self.subTest(chunk=chunk):
                assert_matches_reference(self, data, chunk)

    def test_cr_and_lf_split_across_chunks(self):
        # Split CRLF exactly between CR and LF, and also the terminator at EOF.
        data = b"a,b\r\n1,2\r\n3,4"
        ref = scan_bytes(data, len(data))
        for chunk in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            with self.subTest(chunk=chunk):
                got = scan_bytes(data, chunk)
                self.assertEqual(
                    [(r.start, r.end, r.term_len) for r in got],
                    [(r.start, r.end, r.term_len) for r in ref],
                )

    def test_escaped_double_quote_split_across_chunks(self):
        data = b'a,b\n1,"x""y""z"\n'
        boundaries = set()
        for chunk in range(1, len(data) + 1):
            got = scan_bytes(data, chunk)
            boundaries.add(tuple((r.start, r.end, r.term_len) for r in got))
        self.assertEqual(len(boundaries), 1, boundaries)
        (only,) = boundaries
        self.assertEqual(only, ((0, 4, 1), (4, len(data), 1)))

    def test_multiline_field_spanning_many_blocks(self):
        payload = "第一行\n" * 400 + "结束"
        data = ("a,b\n" + '1,"' + payload.replace('"', '""') + '"\n2,z\n').encode("utf-8")
        for chunk in (1, 7, 64, 4096):
            with self.subTest(chunk=chunk):
                assert_matches_reference(self, data, chunk)

    def test_empty_fields(self):
        data = b"a,b,c\n,,\n1,,3\n"
        for chunk in CHUNK_SIZES:
            got = scan_bytes(data, chunk)
            self.assertEqual([r.fields for r in got], [3, 3, 3])

    def test_empty_line_is_one_empty_field(self):
        data = b"a,b\n\n1,2\n"
        got = scan_bytes(data, 1)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[1].fields, 1)
        self.assertEqual(got[1].end - got[1].start, 1)

    def test_header_only(self):
        for data in (b"a,b\n", b"a,b", b"a,b\r\n", b"\xef\xbb\xbfa,b\r\n"):
            with self.subTest(data=data):
                got = scan_bytes(data, 1)
                self.assertEqual(len(got), 1)
                self.assertTrue(got[0].is_header)
                self.assertEqual(got[0].fields, 2)

    def test_last_record_without_terminator(self):
        got = scan_bytes(b"a,b\n1,2", 1)
        self.assertEqual(got[-1].term_len, 0)
        self.assertEqual(got[-1].end, 7)
        self.assertEqual(got[-1].fields, 2)

    def test_header_registered_first_and_data_numbered_from_zero(self):
        got = scan_bytes(b"a,b\n1,2\n3,4\n", 3)
        self.assertIsNone(got[0].record_no)
        self.assertTrue(got[0].is_header)
        self.assertEqual([r.record_no for r in got[1:]], [0, 1])
        self.assertEqual([r.logical_no for r in got], [1, 2, 3])


class ScannerErrorTests(unittest.TestCase):
    def assert_error(self, data: bytes, chunk: int, offset: int, needle: str, record_no=None):
        records = []
        scanner = Scanner(records.append)
        body = data
        bom = 3 if data.startswith(b"\xef\xbb\xbf") else 0
        body = data[bom:]
        pos = 0
        with self.assertRaises(CsvSyntaxError) as ctx:
            while pos < len(body):
                step = min(chunk, len(body) - pos)
                scanner.feed(body[pos : pos + step], pos)
                pos += step
            scanner.finish(len(body))
        exc = ctx.exception
        self.assertEqual(exc.offset + bom, offset, str(exc))
        self.assertIn(needle, str(exc))
        if record_no is not None:
            self.assertEqual(exc.record_no, record_no, str(exc))
        return exc

    def test_unclosed_quote_reports_opening_quote(self):
        data = b'a,b\n1,"abc\ndef\n'
        for chunk in (1, 2, 5, 64):
            with self.subTest(chunk=chunk):
                self.assert_error(data, chunk, 6, "unterminated", record_no=0)

    def test_unclosed_quote_at_eof_without_newline(self):
        data = b'a,b\n1,"abc'
        self.assert_error(data, 1, 6, "unterminated", record_no=0)

    def test_quote_inside_unquoted_field(self):
        data = b'a,b\n1,x"y\n'
        for chunk in (1, 3, 64):
            with self.subTest(chunk=chunk):
                self.assert_error(data, chunk, 7, "inside an unquoted field", record_no=0)

    def test_quote_at_start_of_unquoted_second_field(self):
        data = b'a,b\nab,cd"e\n'
        self.assert_error(data, 1, 9, "inside an unquoted field", record_no=0)

    def test_junk_after_closing_quote(self):
        data = b'a,b\n1,"ab"x\n'
        self.assert_error(data, 1, 10, "after a closing quote", record_no=0)

    def test_space_after_closing_quote_is_rejected(self):
        data = b'a,b\n1,"ab" \n'
        self.assert_error(data, 1, 10, "after a closing quote", record_no=0)

    def test_header_grammar_errors_are_marked(self):
        exc = self.assert_error(b'a,"b\n', 1, 2, "unterminated")
        self.assertTrue(exc.is_header)
        self.assertIsNone(exc.record_no)

    def test_error_in_later_record_reports_record_number(self):
        data = b"a,b\n1,2\n3,4\n5,x\"y\n"
        exc = self.assert_error(data, 2, 15, "inside an unquoted field")
        self.assertEqual(exc.record_no, 2)
        self.assertFalse(exc.is_header)

    def test_column_mismatch_is_not_a_syntax_error(self):
        # The scanner itself only counts; the indexer rejects mismatches.
        got = scan_bytes(b"a,b\n1\n", 1)
        self.assertEqual([r.fields for r in got], [2, 1])


class ScannerLimitTests(unittest.TestCase):
    def test_record_limit_triggers_immediately(self):
        from csvslice.errors import RecordTooLargeError

        records = []
        scanner = Scanner(records.append, max_record_bytes=10)
        with self.assertRaises(RecordTooLargeError) as ctx:
            scanner.feed(b"a,b\n" + b"x" * 20, 0)
        self.assertEqual(ctx.exception.record_start, 4)
        self.assertEqual(ctx.exception.limit, 10)

    def test_record_limit_can_be_exact(self):
        records = []
        scanner = Scanner(records.append, max_record_bytes=4)
        scanner.feed(b"a,b\n1,2\n", 0)
        scanner.finish(8)
        self.assertEqual(len(records), 2)

    def test_header_limit(self):
        from csvslice.errors import RecordTooLargeError

        scanner = Scanner(lambda r: None, max_record_bytes=3)
        with self.assertRaises(RecordTooLargeError) as ctx:
            scanner.feed(b"abcd\n", 0)
        self.assertTrue(ctx.exception.is_header)
        self.assertIsNone(ctx.exception.record_no)


class ScannerFuzzTests(unittest.TestCase):
    """Randomized differential check against the independent parser."""

    def test_random_grammar_fragments(self):
        import random

        rng = random.Random(991)
        alphabet = [
            b"a",
            b"b",
            b",",
            b'"',
            b"\n",
            b"\r",
            b"\r\n",
            "中".encode("utf-8"),
            b" ",
            b"0",
        ]
        compared = 0
        for _ in range(4000):
            pieces = [rng.choice(alphabet) for _ in range(rng.randint(1, 24))]
            data = b"".join(pieces)
            try:
                bom, refs = csvref.split_records(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if len(refs) < 1:
                continue
            chunk = rng.choice((1, 2, 3, 4, 5, 7, 11, 32))
            got = scan_bytes(data, chunk)
            self.assertEqual(
                [(r.start, r.end, r.term_len, r.fields) for r in got],
                [(r.start, r.end, len(r.term), len(r.fields)) for r in refs],
                f"data={data!r} chunk={chunk}",
            )
            compared += 1
        self.assertGreater(compared, 1000)

    def test_random_valid_multiline_documents(self):
        import random

        rng = random.Random(4242)
        for case in range(300):
            fields = rng.choice((1, 2, 3, 4))
            header = ",".join(f"h{i}" for i in range(fields))
            rows = []
            for r in range(rng.randint(1, 6)):
                cells = []
                for c in range(fields):
                    kind = rng.random()
                    if kind < 0.3:
                        cell = ""
                    elif kind < 0.5:
                        cell = f"第{r}行{c}列"
                    elif kind < 0.7:
                        cell = f'quoted,{r}'
                    elif kind < 0.85:
                        cell = f"line1\nline{r}"
                    else:
                        cell = 'he said ""ok""'
                    if any(x in cell for x in ',"\r\n'):
                        cell = '"' + cell.replace('"', '""') + '"'
                    cells.append(cell)
                rows.append(",".join(cells))
            term = rng.choice(("\n", "\r\n"))
            doc = header + term + term.join(rows)
            if rng.random() < 0.5:
                doc += term
            data = doc.encode("utf-8")
            chunk = rng.choice((1, 2, 3, 5, 8, 13, 64))
            from helpers import assert_matches_reference

            assert_matches_reference(self, data, chunk)


if __name__ == "__main__":
    unittest.main()
