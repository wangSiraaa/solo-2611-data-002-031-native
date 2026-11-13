"""幂等回执：相同 operation_id + 同内容回放，换内容拒绝。"""

from __future__ import annotations

import unittest

from depgraph import repo
from depgraph.repo import IdempotencyConflict

from tests._helpers import DepgraphTestCase


class IdempotencyTest(DepgraphTestCase):
    def _batch(self) -> list[dict]:
        # 第二条是“remove 不存在的边”的 no-op，端点均为已知节点
        return [
            {"seq": 1, "op": "add", "prerequisite": 1, "dependent": 3},
            {"seq": 2, "op": "remove", "prerequisite": 3, "dependent": 1},
        ]

    def test_replay_returns_original_receipt(self) -> None:
        conn = self.init_graph([1, 2, 3], [(1, 2), (2, 3)])
        first = repo.apply_change(conn, "op-idem", self._batch())
        self.assertFalse(first["replayed"])

        second = repo.apply_change(conn, "op-idem", self._batch())
        self.assertTrue(second["replayed"])
        self.assertEqual(second["added_pairs"], first["added_pairs"])
        self.assertEqual(second["removed_pairs"], first["removed_pairs"])

        # 第三次（乱序给出同内容，规范化后指纹一致）同样回放
        shuffled = list(reversed(self._batch()))
        third = repo.apply_change(conn, "op-idem", shuffled)
        self.assertTrue(third["replayed"])
        conn.close()

    def test_same_operation_id_new_content_rejected(self) -> None:
        conn = self.init_graph([1, 2, 3], [(1, 2)])
        repo.apply_change(conn, "op-taken", [
            {"seq": 1, "op": "add", "prerequisite": 2, "dependent": 3},
        ])
        with self.assertRaises(IdempotencyConflict):
            repo.apply_change(
                conn, "op-taken",
                [{"seq": 1, "op": "add", "prerequisite": 1, "dependent": 3}],
            )
        # 冲突不产生新回执行
        count = conn.execute(
            "SELECT count(*) FROM operations WHERE operation_id = 'op-taken'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        conn.close()

    def test_canonical_hash_order_independent(self) -> None:
        ops_a = repo.normalize_operations(self._batch())
        ops_b = repo.normalize_operations(list(reversed(self._batch())))
        self.assertEqual(
            repo.canonical_hash(ops_a), repo.canonical_hash(ops_b)
        )

    def test_receipt_persisted_as_json(self) -> None:
        conn = self.init_graph([1, 2, 3], [(1, 2)])
        receipt = repo.apply_change(
            conn, "op-json",
            [{"seq": 1, "op": "add", "prerequisite": 2, "dependent": 3}],
        )
        row = conn.execute(
            "SELECT added_pairs, removed_pairs FROM operations "
            "WHERE operation_id = 'op-json'"
        ).fetchone()
        # JSONB 经 psycopg 解析为嵌套 list；回执内部也是 list 形式
        self.assertEqual([list(p) for p in receipt["added_pairs"]], row[0])
        self.assertEqual(row[1], [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
