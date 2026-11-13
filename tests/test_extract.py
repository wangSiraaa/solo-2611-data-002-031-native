"""Extraction tests: ordering, byte fidelity, padding, atomicity, reads."""

from __future__ import annotations

import io
import os
import unittest

import csvref
from helpers import TempDirTestCase

from csvslice.errors import (
    ExtractRangeError,
    OutputConflictError,
    SourceMismatchError,
)
from csvslice.extract import extract_ranges, parse_ranges
from csvslice.index import build_index

SAMPLE = (
    "id,name,note,city,amount\n"
    '0,用户0,"第0条\n第二行",北京,0.00\n'
    "1,用户1,普通,上海,0.10\n"
    '2,用户2,"含""引号""",广州,0.20\n'
    "3,用户3,,深圳,0.30\n"
).encode("utf-8")


def ref_header_and_rows(data: bytes):
    bom, refs = csvref.split_records(data)
    return bom, refs[0], refs[1:]


class ExtractOrderingTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write("s.csv", SAMPLE)
        self.idx = self.path("s.idx")
        build_index(self.src, self.idx)
        self.bom, self.header, self.rows = ref_header_and_rows(SAMPLE)

    def extract(self, *specs, name="out.csv"):
        out = self.path(name)
        result = extract_ranges(self.src, self.idx, out, list(specs))
        return out, result, self.read(out)

    def test_single_range_has_one_header_and_original_bytes(self):
        out, result, data = self.extract("1:3")
        self.assertEqual(result.records_written, 2)
        self.assertEqual(
            data, SAMPLE[: self.header.end] + SAMPLE[self.rows[1].start : self.rows[2].end]
        )
        self.assertEqual(data.count(b"id,name,note,city,amount"), 1)

    def test_ranges_are_emitted_in_request_order(self):
        _, _, data = self.extract("2:4", "0:1")
        self.assertEqual(
            data,
            SAMPLE[: self.header.end]
            + SAMPLE[self.rows[2].start : self.rows[3].end]
            + SAMPLE[self.rows[0].start : self.rows[0].end],
        )

    def test_duplicate_ranges_are_preserved(self):
        _, result, data = self.extract("0:2", "0:2")
        self.assertEqual(result.records_written, 4)
        chunk = SAMPLE[self.rows[0].start : self.rows[1].end]
        self.assertEqual(data, SAMPLE[: self.header.end] + chunk + chunk)

    def test_overlapping_ranges_repeat_records(self):
        _, result, data = self.extract("0:3", "1:2")
        expected = (
            SAMPLE[: self.header.end]
            + SAMPLE[self.rows[0].start : self.rows[2].end]
            + SAMPLE[self.rows[1].start : self.rows[1].end]
        )
        self.assertEqual(data, expected)
        self.assertEqual(result.records_written, 4)

    def test_empty_range_contributes_nothing(self):
        _, result, data = self.extract("0:0", "1:2", "3:3")
        self.assertEqual(result.records_written, 1)
        self.assertEqual(
            data,
            SAMPLE[: self.header.end] + SAMPLE[self.rows[1].start : self.rows[1].end],
        )

    def test_full_range_is_byte_identical(self):
        _, _, data = self.extract("0:4")
        self.assertEqual(data, SAMPLE)

    def test_crlf_file_preserves_crlf_and_no_final_newline(self):
        data = b'a,b\r\n1,"x\r\ny"\r\n2,z\r\n3,last'
        src = self.write("crlf.csv", data)
        idx = self.path("crlf.idx")
        build_index(src, idx, chunk_bytes=1)
        out = self.path("crlf.out")
        extract_ranges(src, idx, out, ["0:3"], force=False)
        self.assertEqual(self.read(out), data)

    def test_bom_is_preserved_exactly_once(self):
        data = b"\xef\xbb\xbfa,b\n1,2\n3,4\n"
        src = self.write("bom.csv", data)
        idx = self.path("bom.idx")
        build_index(src, idx)
        out = self.path("bom.out")
        result = extract_ranges(src, idx, out, ["1:2", "0:1"])
        self.assertEqual(self.read(out), b"\xef\xbb\xbfa,b\n3,4\n1,2\n")
        self.assertEqual(result.bom_bytes, 3)


class PaddingTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.data = b"a,b\n0,zero\n1,one\n2,last"
        self.src = self.write("s.csv", self.data)
        self.idx = self.path("s.idx")
        build_index(self.src, self.idx)
        _, self.header, self.rows = ref_header_and_rows(self.data)

    def test_unterminated_record_last_in_output_not_padded(self):
        out = self.path("o1.csv")
        result = extract_ranges(self.src, self.idx, out, ["1:2", "2:3"])
        self.assertFalse(result.padded_terminator)
        self.assertEqual(self.read(out), b"a,b\n1,one\n2,last")

    def test_unterminated_record_in_middle_is_padded_and_reported(self):
        out = self.path("o2.csv")
        result = extract_ranges(self.src, self.idx, out, ["2:3", "0:1"])
        self.assertTrue(result.padded_terminator)
        self.assertEqual(self.read(out), b"a,b\n2,last\n0,zero\n")
        data = self.read(out)
        self.assertEqual(data[result.padding_offset_in_output], ord("\n"))
        self.assertEqual(data[: result.padding_offset_in_output], b"a,b\n2,last")
        # Without padding, the two records would read back as one.
        _, refs = csvref.split_records(data)
        self.assertEqual(len(refs), 3)
        # And the padding byte really is a single LF.
        self.assertTrue(data.endswith(b"last\n0,zero\n"))

    def test_unterminated_record_duplicated_then_followed_is_padded(self):
        out = self.path("o3.csv")
        result = extract_ranges(self.src, self.idx, out, ["2:3", "2:3"])
        self.assertTrue(result.padded_terminator)
        self.assertEqual(self.read(out), b"a,b\n2,last\n2,last")

    def test_unterminated_record_repeated_last_pads_between(self):
        out = self.path("o4.csv")
        result = extract_ranges(self.src, self.idx, out, ["0:3", "2:3"])
        self.assertTrue(result.padded_terminator)
        self.assertEqual(self.read(out), b"a,b\n0,zero\n1,one\n2,last\n2,last")

    def test_empty_ranges_after_do_not_trigger_padding(self):
        out = self.path("o5.csv")
        result = extract_ranges(self.src, self.idx, out, ["2:3", "0:0"])
        self.assertFalse(result.padded_terminator)
        self.assertEqual(self.read(out), b"a,b\n2,last")


class ExtractFailureTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write("s.csv", SAMPLE)
        self.idx = self.path("s.idx")
        build_index(self.src, self.idx)

    def test_out_of_range_rejected(self):
        for i, spec in enumerate(("0:5", "5:6")):
            out = self.path(f"o{i}.csv")
            with self.subTest(spec=spec):
                with self.assertRaises(ExtractRangeError):
                    extract_ranges(self.src, self.idx, out, [spec])
                self.assertFalse(os.path.exists(out))

    def test_empty_range_at_end_is_valid(self):
        out = self.path("empty.csv")
        result = extract_ranges(self.src, self.idx, out, ["4:4"])
        self.assertEqual(result.records_written, 0)
        self.assertEqual(self.read(out), SAMPLE[: SAMPLE.index(b"\n") + 1])

    def test_malformed_ranges(self):
        for spec in ("1", "a:b", "-1:2", "3:1", "1:2:3"):
            with self.subTest(spec=spec):
                with self.assertRaises(ExtractRangeError):
                    parse_ranges([spec])

    def test_source_modified_after_index_is_rejected(self):
        with open(self.src, "ab") as f:
            f.write(b"9,new,row,here,9.99\n")
        out = self.path("o.csv")
        with self.assertRaises(SourceMismatchError):
            extract_ranges(self.src, self.idx, out, ["0:1"])
        self.assertFalse(os.path.exists(out))

    def test_same_length_modification_is_rejected(self):
        mutated = bytearray(SAMPLE)
        mutated[SAMPLE.index(b"0.10")] = ord("9")
        with open(self.src, "wb") as f:
            f.write(mutated)
        with self.assertRaises(SourceMismatchError):
            extract_ranges(self.src, self.idx, self.path("o.csv"), ["0:1"])

    def test_refuses_to_overwrite_source_or_index(self):
        with self.assertRaises(OutputConflictError):
            extract_ranges(self.src, self.idx, self.src, ["0:1"], force=True)
        with self.assertRaises(OutputConflictError):
            extract_ranges(self.src, self.idx, self.idx, ["0:1"], force=True)

    def test_existing_output_requires_force(self):
        out = self.write("exists.csv", b"keep me")
        with self.assertRaises(OutputConflictError):
            extract_ranges(self.src, self.idx, out, ["0:1"])
        self.assertEqual(self.read(out), b"keep me")
        extract_ranges(self.src, self.idx, out, ["0:1"], force=True)
        self.assertNotEqual(self.read(out), b"keep me")

    def test_failure_keeps_previous_successful_output(self):
        out = self.path("o.csv")
        extract_ranges(self.src, self.idx, out, ["0:1"])
        good = self.read(out)
        with self.assertRaises(SourceMismatchError):
            with open(self.src, "ab") as f:
                f.write(b"x")
            extract_ranges(self.src, self.idx, out, ["0:2"], force=True)
        self.assertEqual(self.read(out), good)

    def test_failure_leaves_no_temporary_files(self):
        with self.assertRaises(ExtractRangeError):
            extract_ranges(self.src, self.idx, self.path("o.csv"), ["0:999"])
        leftovers = [n for n in os.listdir(self.tmpdir) if n.startswith(".csvslice-extract-")]
        self.assertEqual(leftovers, [])


class ReadAccountingTests(TempDirTestCase):
    """Prove extraction reads only the header plus requested record ranges."""

    def setUp(self):
        super().setUp()
        # Many records so that an accidental full re-parse would be obvious.
        self.rows = 500
        body = ["a,b"]
        for i in range(self.rows):
            body.append(f"{i},value-{i}-{'x' * 40}")
        self.data = ("\n".join(body) + "\n").encode("utf-8")
        self.src = self.write("big.csv", self.data)
        self.idx = self.path("big.idx")
        build_index(self.src, self.idx)

    def test_reads_only_header_and_requested_ranges(self):
        reads: list[tuple[int, int]] = []

        class RecordingRaw(io.FileIO):
            def read(self, size=-1):
                pos = self.tell()
                data = super().read(size)
                reads.append((pos, len(data)))
                return data

            def readinto(self, b):
                pos = self.tell()
                n = super().readinto(b)
                reads.append((pos, n))
                return n

        def factory(raw):
            return RecordingRaw(raw.fileno(), "rb", closefd=False)

        all_records_bytes = self.data
        header_len = all_records_bytes.index(b"\n") + 1
        out = self.path("o.csv")
        result = extract_ranges(
            self.src,
            self.idx,
            out,
            ["10:13", "400:402"],
            open_hook=lambda mode, path: open(path, mode),
            reader_factory=factory,
        )
        bytes_read = sum(n for _, n in reads)
        # Identity verification happens on a separate file handle, so the
        # recorded handle must not have read the whole file to find ranges.
        self.assertLess(bytes_read, len(self.data) // 2)
        expected = header_len + sum(
            end - start
            for start, end, _term, _fields in (
                self._record(i) for i in (10, 11, 12, 400, 401)
            )
        )
        self.assertEqual(bytes_read, expected)
        self.assertEqual(result.source_bytes_read_for_records, expected - header_len)
        self.assertEqual(result.source_read_ops, 5)
        self.assertEqual(self.read(out).count(b"\n"), header_len and (1 + 5))
        self.assertEqual(len([n for n in os.listdir(self.tmpdir) if n.startswith(".csvslice-")]), 0)

    def _record(self, i):
        from csvslice.index import IndexReader

        with IndexReader(self.idx) as reader:
            return reader.record(i)


if __name__ == "__main__":
    unittest.main()
