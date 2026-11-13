"""提交前/后进程被杀的重试测试。

通过 INCJOIN_CRASH_BEFORE_COMMIT / INCJOIN_CRASH_AFTER_COMMIT 环境变量，
让 apply 在对应批次的提交点前后 os._exit(137)，模拟 kill -9 / 掉电：
* 提交前被杀 -> 事务随连接断开回滚，重放整批后结果正确、只有一条回执；
* 提交后被杀 -> 数据已落盘但回执读取/输出未完成，重放走幂等路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from incjoin import generator
from incjoin.db import connect
from incjoin.verify import verify

from .support import DatabaseTestCase

CRASH_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, %r)
from incjoin import service
from incjoin.batches import load_batch_document
doc = json.loads(sys.argv[1])
service.apply_batch(%r, load_batch_document(json.dumps(doc)))
"""


class CrashRecoveryTests(DatabaseTestCase):
    def _crash_subprocess(self, hook: str, doc: dict) -> None:
        env = dict(os.environ)
        env[hook] = doc["batch_id"]
        code = CRASH_SCRIPT % (str(Path.cwd()), self.dsn)
        proc = subprocess.run(
            [sys.executable, "-c", code, json.dumps(doc)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 137, proc.stderr)

    def _assert_no_receipt(self, batch_id: str) -> None:
        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM batch_receipt WHERE batch_id = %s", (batch_id,)
            )
            self.assertIsNone(cur.fetchone())

    def _assert_one_receipt(self, batch_id: str) -> None:
        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM batch_receipt WHERE batch_id = %s",
                (batch_id,),
            )
            self.assertEqual(cur.fetchone()[0], 1)

    def test_01_crash_before_commit_then_retry_succeeds(self) -> None:
        self.load_initial()
        doc = generator.SCENARIOS["both_sides_add"]
        self._crash_subprocess("INCJOIN_CRASH_BEFORE_COMMIT", doc)

        # 回滚应干净：无回执、无该批次痕迹、完整重算仍通过
        self._assert_no_receipt(doc["batch_id"])
        self.assertTrue(verify(self.dsn).ok)
        snap_before = self.snapshot()
        # 源表也不应有 L6/R5
        self.assertFalse(any(r[0] in ("L6", "R5")
                             for r in snap_before["left"] + snap_before["right"]))

        # 重试同一批次：正常提交
        result = self.apply_doc(doc)
        self.assertEqual(result["status"], "committed")
        self._assert_one_receipt(doc["batch_id"])
        self.assertTrue(verify(self.dsn).ok)
        self.assert_matches_python_model()

    def test_02_crash_after_commit_then_retry_is_idempotent(self) -> None:
        self.load_initial()
        doc = generator.SCENARIOS["duplicate_keys"]
        self._crash_subprocess("INCJOIN_CRASH_AFTER_COMMIT", doc)

        # 提交已落盘：回执与物化结果都在
        self._assert_one_receipt(doc["batch_id"])
        self.assertTrue(verify(self.dsn).ok)

        # 重试：幂等重放，不产生第二条回执，状态不变
        before = self.snapshot()
        result = self.apply_doc(doc)
        self.assertEqual(result["status"], "replayed")
        self.assertEqual(self.snapshot(), before)
        self._assert_one_receipt(doc["batch_id"])

    def test_03_crash_hook_does_not_fire_for_other_batches(self) -> None:
        self.load_initial()
        doc = generator.SCENARIOS["key_migration"]
        env = dict(os.environ)
        env["INCJOIN_CRASH_BEFORE_COMMIT"] = "some-other-batch"
        code = CRASH_SCRIPT % (str(Path.cwd()), self.dsn)
        proc = subprocess.run(
            [sys.executable, "-c", code, json.dumps(doc)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_one_receipt(doc["batch_id"])
        self.assertTrue(verify(self.dsn).ok)
