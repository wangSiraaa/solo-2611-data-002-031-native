"""崩溃演练：在事务三个关键位置 kill -9 进程，再重试核对状态。

- before_commit：直接边与闭包都已修改、COMMIT 之前 -> 整体回滚，重试从零提交
- mid_closure：闭包删除完成、插入之前 -> 整体回滚，物化表保持提交前状态
- after_commit：COMMIT 完成、回执返回之前 -> 数据已落盘，重试回放原回执
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from depgraph import repo

from tests._helpers import (
    DepgraphTestCase,
    TEST_DSN,
    connect_with_recovery_retry,
)

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "depgraph.cli", "--dsn", TEST_DSN]


class CrashRecoveryTest(DepgraphTestCase):
    def setUp(self) -> None:
        # 两条互不相交的 50 节点链：1..50 与 51..100。
        # 桥接边 50->51 会新增 50*50=2500 个跨链可达对，差异确定、便于核对。
        self.nodes = list(range(1, 101))
        self.edges = [(i, i + 1) for i in range(1, 50)]
        self.edges += [(i, i + 1) for i in range(51, 100)]
        conn = self.init_graph(self.nodes, self.edges)
        conn.close()
        self.new_pairs = 50 * 50
        self.batch = {
            "operation_id": "op-crash",
            "operations": [
                {"seq": 1, "op": "add",
                 "prerequisite": 50, "dependent": 51},
            ],
        }
        self._batch_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        json.dump(self.batch, self._batch_file)
        self._batch_file.close()

    def tearDown(self) -> None:
        os.unlink(self._batch_file.name)

    def _run_cli(self, *extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["DEPGRAPH_DSN"] = TEST_DSN
        return subprocess.run(
            CLI + ["change", self._batch_file.name, *extra],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=60,
        )

    def _state_signature(self) -> tuple[list, int, int]:
        with connect_with_recovery_retry(TEST_DSN) as conn:
            edges = repo.load_edges(conn)
            report = repo.verify(conn)
            ops = conn.execute("SELECT count(*) FROM operations").fetchone()[0]
        self.assertTrue(report["ok"], msg=str(report))
        return edges, report["closure_rows"], ops

    def test_crash_before_commit_then_retry(self) -> None:
        before = self._state_signature()

        proc = self._run_cli("--crash-at", "before_commit")
        self.assertLess(proc.returncode, 0)  # 被 SIGKILL（负值信号号）

        # 事务随连接断开而回滚：边/闭包/回执表与崩溃前完全一致
        self.assertEqual(self._state_signature(), before)

        # 不带注入重试：正常提交
        proc = self._run_cli()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["receipt"]["replayed"])

        edges_after, closure_rows, ops = self._state_signature()
        self.assertEqual(ops, 1)
        self.assertIn((50, 51), edges_after)
        # 桥接两条 50 节点链，新增 50*50 个跨链可达对，无消失对
        self.assertEqual(
            len(payload["receipt"]["added_pairs"]), self.new_pairs
        )
        self.assertEqual(payload["receipt"]["removed_pairs"], [])
        self.assertEqual(closure_rows, before[1] + self.new_pairs)

    def test_crash_mid_closure_then_retry(self) -> None:
        before = self._state_signature()

        proc = self._run_cli("--crash-at", "mid_closure")
        self.assertLess(proc.returncode, 0)

        # 闭包删了一半也必须整体回滚，不能留下残缺闭包
        self.assertEqual(self._state_signature(), before)

        proc = self._run_cli()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self._state_signature()  # 再次校验闭包完整一致

    def test_crash_after_commit_then_retry_replays(self) -> None:
        # 首次尝试即注入 after_commit：COMMIT 已落盘，进程在返回回执前被杀
        proc = self._run_cli("--crash-at", "after_commit")
        self.assertLess(proc.returncode, 0)

        # 数据已落盘；重试返回原回执（replayed=True）
        retry = self._run_cli()
        self.assertEqual(retry.returncode, 0, msg=retry.stderr)
        replay = json.loads(retry.stdout)["receipt"]
        self.assertTrue(replay["replayed"])
        # 回执内容与独立预期一致：桥接边新增 50*50 个可达对
        self.assertEqual(len(replay["added_pairs"]), self.new_pairs)
        self.assertIn([50, 51], replay["added_pairs"])
        self.assertEqual(replay["removed_pairs"], [])

        with connect_with_recovery_retry(TEST_DSN) as conn:
            # 回执只有一行，闭包标记没有被重试重写
            self.assertEqual(
                conn.execute("SELECT count(*) FROM operations").fetchone()[0],
                1,
            )
            markers = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT last_changed_operation FROM closure "
                    "WHERE ancestor = 50 AND descendant = 51"
                ).fetchall()
            }
            self.assertEqual(markers, {"op-crash"})


if __name__ == "__main__":
    unittest.main()
