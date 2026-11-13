"""End-to-end CLI tests via ``python -m csvslice``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from helpers import TempDirTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args: str, cwd: str | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "csvslice", *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc


class CliTests(TempDirTestCase):
    def test_generate_build_verify_extract_roundtrip(self):
        csv_path = self.path("data.csv")
        idx_path = self.path("data.idx")
        out_path = self.path("out.csv")

        gen = run_cli(
            "generate",
            csv_path,
            "--rows",
            "12",
            "--multiline-every",
            "3",
            "--quoted-comma-every",
            "4",
            "--quoted-quote-every",
            "5",
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)
        payload = json.loads(gen.stdout)
        self.assertEqual(payload["data_records"], 12)

        build = run_cli("build", csv_path, idx_path, "--chunk-size", "7")
        self.assertEqual(build.returncode, 0, build.stderr)
        built = json.loads(build.stdout)
        self.assertEqual(built["data_records"], 12)
        self.assertEqual(built["header_fields"], 5)

        verify = run_cli("verify", idx_path, "--source", csv_path)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["source_checksum_ok"])

        extract = run_cli("extract", csv_path, idx_path, out_path, "--ranges", "2:5,0:1")
        self.assertEqual(extract.returncode, 0, extract.stderr)
        result = json.loads(extract.stdout)
        self.assertEqual(result["records_written"], 4)
        self.assertEqual(result["terminator_padding"], None)
        with open(csv_path, "rb") as f:
            source = f.read()
        with open(out_path, "rb") as f:
            out = f.read()
        self.assertTrue(out.startswith(source.split(b"\n", 1)[0] + b"\n"))

    def test_generate_bom_and_no_final_newline(self):
        csv_path = self.path("x.csv")
        idx_path = self.path("x.idx")
        out_path = self.path("x.out")
        gen = run_cli(
            "generate", csv_path, "--rows", "4", "--bom", "--no-final-newline"
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)
        with open(csv_path, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertFalse(data.endswith(b"\n"))

        self.assertEqual(run_cli("build", csv_path, idx_path).returncode, 0)
        # Put the unterminated last record first: the CLI must report padding.
        proc = run_cli("extract", csv_path, idx_path, out_path, "--ranges", "3:4,0:1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIsNotNone(payload["terminator_padding"])
        self.assertEqual(payload["terminator_padding"]["added_bytes"], "0a")
        with open(out_path, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xef\xbb\xbfid,name,note,city,amount\n"))

    def test_generate_target_size(self):
        csv_path = self.path("t.csv")
        proc = run_cli(
            "generate", csv_path, "--target-size", "200KiB", "--chunk-size", "512"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        size = os.path.getsize(csv_path)
        # Generation writes until the target is reached, so it lands in
        # [target, target + one record).
        self.assertGreaterEqual(size, 200 * 1024)
        self.assertLessEqual(size, 200 * 1024 + 4096)

    def test_build_reports_syntax_error_location_on_stderr(self):
        src = self.write("bad.csv", b'a,b\n1,"open\n2,3\n')
        idx = self.path("bad.idx")
        proc = run_cli("build", src, idx)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unterminated", proc.stderr)
        self.assertIn("offset 6", proc.stderr)
        self.assertFalse(os.path.exists(idx))

    def test_build_reports_column_mismatch(self):
        src = self.write("bad.csv", b"a,b\n1,2\n3\n")
        proc = run_cli("build", src, self.path("bad.idx"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("data record 1", proc.stderr)
        self.assertIn("offset 8", proc.stderr)

    def test_extract_requires_force_for_existing_output(self):
        src = self.write("s.csv", b"a,b\n1,2\n")
        idx = self.path("s.idx")
        self.assertEqual(run_cli("build", src, idx).returncode, 0)
        out = self.write("o.csv", b"old")
        proc = run_cli("extract", src, idx, out, "--ranges", "0:1")
        self.assertNotEqual(proc.returncode, 0)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"old")
        proc = run_cli("extract", src, idx, out, "--ranges", "0:1", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_extract_rejects_modified_source(self):
        src = self.write("s.csv", b"a,b\n1,2\n")
        idx = self.path("s.idx")
        self.assertEqual(run_cli("build", src, idx).returncode, 0)
        self.write("s.csv", b"a,b\n1,2\n3,4\n")
        proc = run_cli("extract", src, idx, self.path("o.csv"), "--ranges", "0:1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match index", proc.stderr)

    def test_verify_fails_on_tampered_index(self):
        src = self.write("s.csv", b"a,b\n1,2\n")
        idx = self.path("s.idx")
        self.assertEqual(run_cli("build", src, idx).returncode, 0)
        with open(idx, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * 8)
        proc = run_cli("verify", idx)
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_subcommand_is_usage_error(self):
        proc = run_cli()
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
