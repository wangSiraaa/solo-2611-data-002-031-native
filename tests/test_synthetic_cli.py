"""合成数据与规模上限、verify 损坏探测、CLI 端到端。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from depgraph import repo, synthetic

from tests._helpers import DepgraphTestCase, TEST_DSN

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "depgraph.cli", "--dsn", TEST_DSN]


class SyntheticTest(DepgraphTestCase):
    def test_all_styles_are_dag_and_verified(self) -> None:
        for style in ("chain", "diamond", "layered"):
            nodes, edges = synthetic.build(
                style, 120, isolated=4, extra_edges=120, seed=3
            )
            conn = self.init_graph(nodes, edges)
            report = repo.verify(conn)
            self.assertTrue(report["ok"], msg=f"{style}: {report}")
            conn.close()

    def test_scale_cap_2000(self) -> None:
        with self.assertRaises(ValueError):
            synthetic.layered_dag(2001)
        nodes, edges = synthetic.layered_dag(2000, isolated=5,
                                             extra_edges=400, seed=5)
        self.assertEqual(len(nodes), 2000)
        conn = self.init_graph(nodes, edges)
        self.assertTrue(repo.verify(conn)["ok"])
        conn.close()

    def test_large_layered_incremental_change(self) -> None:
        # 大图层上做一次小支路修改，确认区域化更新与参考遍历一致，
        # 且绝大多数闭包行保留 INIT 标记
        nodes, edges = synthetic.layered_dag(
            500, isolated=3, extra_edges=300, seed=9
        )
        conn = self.init_graph(nodes, edges)
        before = self.closure_snapshot(conn)
        target = max(edges, key=lambda e: e[0])  # 选一条小编号无关的边
        repo.apply_change(
            conn, "op-large",
            [{"seq": 1, "op": "remove",
              "prerequisite": target[0], "dependent": target[1]}],
        )
        after = self.closure_snapshot(conn)
        self.assertTrue(repo.verify(conn)["ok"])
        rewritten = {p for p in before if p in after and before[p] != after[p]}
        self.assertEqual(rewritten, set())  # 没有任何保留行被重写标记
        init_rows = sum(1 for m in after.values() if m == "INIT")
        self.assertGreater(init_rows, len(after) * 0.9)
        conn.close()


class VerifyCorruptionTest(DepgraphTestCase):
    def test_verify_detects_missing_and_extra_rows(self) -> None:
        nodes, edges = synthetic.chain(10)
        conn = self.init_graph(nodes, edges)

        # 人为删除一行闭包 -> verify 必须报 missing
        conn.execute("DELETE FROM closure WHERE ancestor = 1 AND descendant = 2")
        conn.commit()
        report = repo.verify(conn)
        self.assertFalse(report["ok"])
        self.assertIn((1, 2), [tuple(p) for p in report["missing_pairs"]])

        # 再人为插入一条假可达对 -> verify 报 extra
        conn.execute(
            "INSERT INTO closure (ancestor, descendant, last_changed_operation) "
            "VALUES (9, 1, 'TAMPER')"
        )
        conn.commit()
        report = repo.verify(conn)
        self.assertIn((9, 1), [tuple(p) for p in report["extra_pairs"]])
        conn.close()

    def test_verify_detects_cycle_in_direct_edges(self) -> None:
        nodes, edges = synthetic.chain(6)
        conn = self.init_graph(nodes, edges)
        # 绕过工具直接制造环（仅用于检验 verify 的独立判环能力）
        conn.execute(
            "INSERT INTO edges (prerequisite, dependent, last_changed_operation) "
            "VALUES (6, 1, 'TAMPER')"
        )
        conn.commit()
        report = repo.verify(conn)
        self.assertFalse(report["ok"])
        self.assertIsNotNone(report["cycle"])
        self.assertEqual(report["cycle"][0], report["cycle"][-1])
        conn.close()


class CliEndToEndTest(DepgraphTestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        import os
        env = dict(os.environ, DEPGRAPH_DSN=TEST_DSN)
        return subprocess.run(
            CLI + list(args), capture_output=True, text=True,
            env=env, cwd=ROOT, timeout=60,
        )

    def test_cli_full_lifecycle(self) -> None:
        graph_file = ROOT / ".runtime" / "cli_graph.json"
        batch_file = ROOT / ".runtime" / "cli_batch.json"
        graph_file.parent.mkdir(exist_ok=True)
        nodes, edges = synthetic.diamond(12, isolated=1)
        graph_file.write_text(json.dumps(
            {"nodes": nodes, "edges": [list(e) for e in edges]}
        ), encoding="utf-8")
        batch_file.write_text(json.dumps({
            "operation_id": "op-cli",
            "operations": [
                {"seq": 1, "op": "add",
                 "prerequisite": nodes[0], "dependent": nodes[-2]},
            ],
        }), encoding="utf-8")

        proc = self._run("init", str(graph_file), "--reset")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        proc = self._run("change", str(batch_file))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        receipt = json.loads(proc.stdout)["receipt"]
        self.assertEqual(receipt["status"], "committed")

        proc = self._run("verify")
        self.assertEqual(proc.returncode, 0, msg=proc.stdout)

        proc = self._run("ancestors", str(nodes[-2]))
        self.assertEqual(proc.returncode, 0)
        self.assertIn(nodes[0], json.loads(proc.stdout)["ancestors"])

        # synthetic 命令落盘文件
        out = ROOT / ".runtime" / "synth.json"
        proc = self._run("synthetic", "chain", "50", "--isolated", "2",
                         "--out", str(out))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["nodes"]), 50)
        self.assertEqual(len(payload["isolated"]), 2)


if __name__ == "__main__":
    unittest.main()
