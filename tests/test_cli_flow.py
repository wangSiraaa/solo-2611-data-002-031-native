"""端到端测试：CLI 子进程 + 随机批次流 + 每批完整重算守恒。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from incjoin import generator
from incjoin.verify import verify

from .support import DatabaseTestCase


class CliAndRandomFlowTests(DatabaseTestCase):
    def _cli(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "incjoin", "--dsn", self.dsn, *args],
            capture_output=True, text=True, cwd=Path.cwd(), timeout=120,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc.returncode, payload

    def test_01_full_cli_flow_with_random_batches(self) -> None:
        work = Path(".runtime/e2e")
        files = generator.generate_workspace(work)

        code, payload = self._cli(
            "load", "--left", files["left"], "--right", files["right"]
        )
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verify"]["ok"])

        # 命名场景依次跑（含两个非法批次，必须失败且不污染状态）
        for name, path in files.items():
            if not name.startswith(("both", "delete", "key_m", "unit",
                                    "last", "duplicate", "invalid")):
                continue
            code, payload = self._cli("apply", path)
            if name.startswith("invalid"):
                self.assertEqual(code, 1)
                self.assertEqual(payload["status"], "error")
            else:
                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["verify"]["ok"], payload)

        # 随机批次引用 random_snapshot 的行，需先装载随机快照
        code, payload = self._cli(
            "load", "--left", files["random_left"],
            "--right", files["random_right"]
        )
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verify"]["ok"])

        # 20 个随机批次，每个提交后 CLI 内部都做一次完整重算
        for i in range(20):
            path = files[f"random_batch_{i:03d}"]
            code, payload = self._cli("apply", path)
            self.assertEqual(code, 0, payload)
            self.assertTrue(payload["verify"]["ok"], payload)

        # 最终再独立 verify + Python 参照模型双保险
        self.assertTrue(verify(self.dsn).ok)
        self.assert_matches_python_model()

        code, receipts = self._cli("query", "receipts")
        self.assertEqual(code, 0)
        # load 会清空回执；当前只有 20 个随机批次回执
        self.assertEqual(len(receipts), 20)

        code, summary = self._cli("query", "summary")
        self.assertEqual(code, 0)
        self.assertIsInstance(summary, list)

    def test_02_random_snapshot_load(self) -> None:
        work = Path(".runtime/e2e_random")
        files = generator.generate_workspace(work)
        code, payload = self._cli(
            "load", "--left", files["random_left"],
            "--right", files["random_right"]
        )
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["verify"]["ok"])
        self.assert_matches_python_model()

    def test_03_verify_command_on_empty_database(self) -> None:
        # 同库用例可能已写入数据，这里显式清空后验证空库重算
        from incjoin.db import connect

        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "TRUNCATE batch_receipt, key_summary, join_detail, "
                "l_source, r_source"
            )
            conn.commit()
        code, payload = self._cli("verify")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["detail_expected"], 0)
        self.assertEqual(payload["summary_expected"], 0)
