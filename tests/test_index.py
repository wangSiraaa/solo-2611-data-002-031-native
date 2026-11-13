"""Index build/verify tests: offsets, persistence, failure modes."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import unittest

import csvref
from helpers import TempDirTestCase, assert_matches_reference

from csvslice.errors import (
    ColumnCountError,
    CsvSyntaxError,
    EmptyInputError,
    IndexChecksumError,
    IndexIntegrityError,
    IndexValidationError,
    RecordTooLargeError,
    SourceMismatchError,
)
from csvslice.index import (
    FORMAT_VERSION,
    IndexReader,
    build_index,
    verify_index,
)
from csvslice.scanner import Scanner


SAMPLE = (
    "id,name,note,city,amount\n"
    '0,用户0,"第0条\n第二行",北京,0.00\n'
    "1,用户1,普通,上海,0.10\n"
    '2,用户2,"含""引号""",广州,0.20\n'
    "3,用户3,,深圳,0.30\n"
).encode("utf-8")


def expected_offsets(data: bytes):
    bom, refs = csvref.split_records(data)
    header, rows = refs[0], refs[1:]
    return {
        "bom": bom,
        "header": (header.start, header.end, len(header.term)),
        "records": [(r.start, r.end, len(r.term), len(r.fields)) for r in rows],
    }


class BuildOffsetTests(TempDirTestCase):
    def test_offsets_match_reference_for_many_chunk_sizes(self):
        src = self.write("sample.csv", SAMPLE)
        expected = expected_offsets(SAMPLE)
        snapshots = set()
        for chunk in (1, 2, 3, 5, 8, 13, 64, len(SAMPLE)):
            idx = self.path(f"sample-{chunk}.idx")
            result = build_index(src, idx, chunk_bytes=chunk)
            self.assertEqual(result.data_record_count, len(expected["records"]))
            with IndexReader(idx) as reader:
                self.assertEqual(
                    reader.header_range(),
                    expected["header"],
                )
                got = [reader.record(i) for i in range(result.data_record_count)]
            snapshots.add(tuple(got))
            self.assertEqual(got, expected["records"], f"chunk={chunk}")
        self.assertEqual(len(snapshots), 1)

    def test_crlf_and_bom_offsets(self):
        data = (
            b"\xef\xbb\xbfid,name\r\n"
            b'1,"a\r\nb"\r\n'
            b"2,c\r\n"
        )
        src = self.write("crlf.csv", data)
        idx = self.path("crlf.idx")
        result = build_index(src, idx, chunk_bytes=2)
        expected = expected_offsets(data)
        self.assertEqual(result.bom_len, 3)
        self.assertEqual(result.header_start, 3)
        with IndexReader(idx) as reader:
            self.assertEqual(reader.header_range(), expected["header"])
            self.assertEqual(
                [reader.record(i) for i in range(result.data_record_count)],
                expected["records"],
            )

    def test_offsets_identical_to_reference_for_every_split(self):
        body = SAMPLE
        for chunk in range(1, 40):
            src = self.write(f"c{chunk}.csv", body)
            idx = self.path(f"c{chunk}.idx")
            build_index(src, idx, chunk_bytes=chunk)
            with IndexReader(idx) as reader:
                got = [reader.record(i) for i in range(reader.data_record_count)]
            self.assertEqual(got, expected_offsets(body)["records"], f"chunk={chunk}")


class IndexPersistenceTests(TempDirTestCase):
    def test_persists_size_sha_version_and_checksum(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        conn = sqlite3.connect(idx)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
        self.assertEqual(meta["format_version"], FORMAT_VERSION)
        self.assertEqual(int(meta["source_size"]), len(SAMPLE))
        self.assertEqual(meta["source_sha256"], hashlib.sha256(SAMPLE).hexdigest())
        self.assertRegex(meta["index_checksum"], r"^[0-9a-f]{64}$")
        # header is stored separately, not as a data record
        self.assertIn("header_start", meta)
        self.assertIn("header_end", meta)
        self.assertEqual(int(meta["data_record_count"]), 4)

    def test_verify_with_and_without_source(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        ok = verify_index(idx)
        self.assertFalse(ok.source_checksum_checked)
        ok2 = verify_index(idx, src)
        self.assertTrue(ok2.source_checksum_ok)
        self.assertEqual(ok2.data_record_count, 4)

    def test_source_mismatch_after_modification(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        with open(src, "ab") as f:
            f.write("4,用户4,追加,杭州,0.40\n".encode("utf-8"))
        with self.assertRaises(SourceMismatchError) as ctx:
            verify_index(idx, src)
        self.assertIn("size", str(ctx.exception).lower())

    def test_same_size_modification_detected_by_hash(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        mutated = bytearray(SAMPLE)
        mutated[-3:] = b"XYZ"
        with open(src, "wb") as f:
            f.write(mutated)
        with self.assertRaises(SourceMismatchError) as ctx:
            verify_index(idx, src)
        self.assertIn("sha256", str(ctx.exception).lower())

    def test_corrupted_checksum_detected(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        conn = sqlite3.connect(idx)
        try:
            conn.execute("UPDATE meta SET value='0'*64 WHERE key='index_checksum'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(IndexChecksumError):
            verify_index(idx)

    def test_tampered_record_row_detected_by_checksum(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        conn = sqlite3.connect(idx)
        try:
            conn.execute("UPDATE records SET end_off = end_off + 1 WHERE record_no = 1")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(IndexChecksumError):
            verify_index(idx)

    def test_unknown_format_version_rejected(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        conn = sqlite3.connect(idx)
        try:
            conn.execute("UPDATE meta SET value='999' WHERE key='format_version'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(IndexValidationError) as ctx:
            verify_index(idx)
        self.assertIn("format version", str(ctx.exception))

    def test_missing_index_file(self):
        with self.assertRaises(IndexValidationError):
            verify_index(self.path("nope.idx"))

    def test_garbage_index_file(self):
        idx = self.write("bad.idx", b"not a database")
        with self.assertRaises((IndexValidationError, IndexIntegrityError)):
            verify_index(idx)

    def test_integrity_failure_surfaces(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        with open(idx, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * 16)
        with self.assertRaises((IndexIntegrityError, IndexValidationError, IndexChecksumError)):
            verify_index(idx)


class BuildFailureTests(TempDirTestCase):
    def _assert_no_index(self, idx: str):
        self.assertFalse(os.path.exists(idx))
        self.assertEqual(os.listdir(self.tmpdir), [n for n in os.listdir(self.tmpdir) if not n.startswith(".csvslice-index-")])

    def test_empty_file_rejected(self):
        src = self.write("empty.csv", b"")
        idx = self.path("empty.idx")
        with self.assertRaises(EmptyInputError):
            build_index(src, idx)
        self.assertFalse(os.path.exists(idx))

    def test_bom_only_file_rejected(self):
        src = self.write("bom.csv", b"\xef\xbb\xbf")
        with self.assertRaises(EmptyInputError):
            build_index(src, self.path("bom.idx"))

    def test_header_only_is_valid(self):
        src = self.write("h.csv", b"a,b\r\n")
        idx = self.path("h.idx")
        result = build_index(src, idx)
        self.assertEqual(result.data_record_count, 0)
        with IndexReader(idx) as reader:
            self.assertEqual(reader.header_fields, 2)
            self.assertEqual(list(reader.iter_all_records()), [])

    def test_column_count_mismatch_reports_record_and_offset(self):
        data = b"a,b,c\n1,2,3\n4,5\n"
        src = self.write("bad.csv", data)
        idx = self.path("bad.idx")
        with self.assertRaises(ColumnCountError) as ctx:
            build_index(src, idx)
        exc = ctx.exception
        self.assertEqual(exc.record_no, 1)
        self.assertEqual(exc.offset, len(b"a,b,c\n1,2,3\n"))
        self.assertEqual((exc.expected, exc.actual), (3, 2))
        self.assertFalse(os.path.exists(idx))

    def test_syntax_error_prevents_publishing(self):
        for data, offset, needle in [
            (b'a,b\n1,"open\n', 6, "unterminated"),
            (b'a,b\n1,x"y\n', 7, "unquoted"),
            (b'a,b\n1,"x"y\n', 9, "closing quote"),
        ]:
            with self.subTest(data=data):
                src = self.write("s.csv", data)
                idx = self.path("s.idx")
                with self.assertRaises(CsvSyntaxError) as ctx:
                    build_index(src, idx)
                self.assertEqual(ctx.exception.offset, offset)
                self.assertIn(needle, str(ctx.exception))
                self.assertFalse(os.path.exists(idx))

    def test_failed_rebuild_keeps_previous_index(self):
        src = self.write("s.csv", SAMPLE)
        idx = self.path("s.idx")
        build_index(src, idx)
        original = self.read(idx)
        with open(src, "ab") as f:
            f.write(b"4,only,three,fields\n")
        with self.assertRaises(ColumnCountError):
            build_index(src, idx)
        self.assertEqual(self.read(idx), original)
        verify_index(idx)  # still the old, valid index

    def test_record_over_1mib_fails_fast(self):
        good = b"a,b\n1,2\n"
        big = b'a,b\n1,"' + b"x" * (1 << 20) + b'"\n'
        src = self.write("big.csv", big)
        idx = self.path("big.idx")
        with self.assertRaises(RecordTooLargeError) as ctx:
            build_index(src, idx)
        self.assertEqual(ctx.exception.record_no, 0)
        self.assertFalse(os.path.exists(idx))

    def test_exactly_1mib_record_allowed(self):
        # Build a record that is exactly 1 MiB including its LF terminator.
        target = 1 << 20
        prefix = b"a,b\n1,"
        suffix = b"\n"
        payload_len = target - len(prefix) - len(suffix)
        data = prefix + b"x" * payload_len + suffix
        src = self.write("exact.csv", data)
        result = build_index(src, self.path("exact.idx"))
        self.assertEqual(result.data_record_count, 1)

    def test_1mib_plus_one_rejected(self):
        # The data record itself (including its LF) is one byte over the cap.
        target = (1 << 20) + 1
        header = b"a,b\n"
        prefix = b"1,"
        suffix = b"\n"
        data = header + prefix + b"x" * (target - len(prefix) - len(suffix)) + suffix
        src = self.write("over.csv", data)
        with self.assertRaises(RecordTooLargeError):
            build_index(src, self.path("over.idx"))

    def test_index_path_equals_source_rejected(self):
        src = self.write("s.csv", SAMPLE)
        with self.assertRaises(ValueError):
            build_index(src, src)


class BuildDeterminismTests(TempDirTestCase):
    def test_index_contents_identical_across_chunk_sizes(self):
        src = self.write("s.csv", SAMPLE)
        fingerprints = set()
        for chunk in (1, 4, 17, 1024):
            idx = self.path(f"d{chunk}.idx")
            build_index(src, idx, chunk_bytes=chunk)
            conn = sqlite3.connect(idx)
            try:
                rows = conn.execute(
                    "SELECT record_no,start_off,end_off,term_len,fields FROM records ORDER BY record_no"
                ).fetchall()
                meta = dict(conn.execute("SELECT key,value FROM meta WHERE key<>'chunk'"))
            finally:
                conn.close()
            fingerprints.add((tuple(rows), tuple(sorted(meta.items()))))
        self.assertEqual(len(fingerprints), 1)


if __name__ == "__main__":
    unittest.main()
