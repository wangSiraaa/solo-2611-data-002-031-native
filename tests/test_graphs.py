"""图结构与增量闭包：链、菱形、多替代路径、孤点、整批反转、标记保留。"""

from __future__ import annotations

import unittest

from depgraph import repo, synthetic
from depgraph.repo import BatchRejected

from tests._helpers import DepgraphTestCase


class ChainTest(DepgraphTestCase):
    def test_chain_closure_and_queries(self) -> None:
        nodes, edges = synthetic.chain(6)
        conn = self.init_graph(nodes, edges)
        self.assert_closure_matches_reference(conn, nodes, edges)
        self.assertEqual(repo.descendants(conn, 1), [2, 3, 4, 5, 6])
        self.assertEqual(repo.ancestors(conn, 6), [1, 2, 3, 4, 5])
        self.assertEqual(repo.ancestors(conn, 1), [])
        # 查询不存在的节点应明确报错，而不是静默返回空
        with self.assertRaises(BatchRejected):
            repo.ancestors(conn, 999)
        with self.assertRaises(BatchRejected):
            repo.descendants(conn, 999)
        conn.close()

    def test_remove_middle_edge_splits_chain(self) -> None:
        nodes, edges = synthetic.chain(6)
        conn = self.init_graph(nodes, edges)
        receipt = repo.apply_change(
            conn, "op-cut", [{"seq": 1, "op": "remove",
                              "prerequisite": 3, "dependent": 4}]
        )
        # 3->4 断开后，所有跨越断点的可达对都消失
        self.assertEqual(
            receipt["removed_pairs"],
            sorted((a, d) for a in (1, 2, 3) for d in (4, 5, 6)),
        )
        self.assertEqual(receipt["added_pairs"], [])
        self.assert_closure_matches_reference(conn, nodes, repo.load_edges(conn))
        self.assertEqual(repo.descendants(conn, 3), [])
        # 断点右侧 4->5->6：4 是该段起点，没有祖先；5 的祖先只有 4
        self.assertEqual(repo.ancestors(conn, 4), [])
        self.assertEqual(repo.ancestors(conn, 5), [4])
        self.assertEqual(repo.descendants(conn, 4), [5, 6])
        conn.close()


class DiamondTest(DepgraphTestCase):
    def test_diamond_alternative_path_keeps_pairs(self) -> None:
        nodes = [1, 2, 3, 4, 5, 6]
        edges = [(1, 2), (1, 3), (2, 4), (3, 4), (4, 5), (5, 6)]
        conn = self.init_graph(nodes, edges)
        before = self.closure_snapshot(conn)

        receipt = repo.apply_change(
            conn, "op-drop-1-2",
            [{"seq": 1, "op": "remove",
              "prerequisite": 1, "dependent": 2}],
        )
        # 边 (2,4) 本身仍在，故 (2,4)/(2,5)/(2,6) 继续可达；
        # 真正消失的只有以被删边为唯一支撑的 (1,2)。
        # (1,4)/(1,5)/(1,6) 因 1->3->4 这条替代路径而保留。
        self.assertEqual(receipt["removed_pairs"], [(1, 2)])
        self.assertEqual(receipt["added_pairs"], [])
        self.assert_closure_matches_reference(conn, nodes, repo.load_edges(conn))
        after = self.closure_snapshot(conn)
        # 替代路径支撑的可达对标记必须保持 INIT，不被重写
        for pair in [(1, 4), (1, 5), (1, 6), (3, 4), (3, 5), (3, 6),
                     (2, 4), (2, 5), (2, 6)]:
            self.assertEqual(after[pair], "INIT", msg=f"{pair} 标记被重写")
        # 所有保留下来的闭包行（集合交集）标记都必须与变更前一致
        for pair in set(before) & set(after):
            self.assertEqual(after[pair], before[pair],
                             msg=f"{pair} 标记被意外重写")
        # 消失的对恰好只有 (1,2)
        self.assertEqual(set(before) - set(after), {(1, 2)})
        conn.close()

    def test_remove_full_branch_drops_indirect_pairs(self) -> None:
        # 同一菱形：把经过 2 的整条支路 (1,2) 与 (2,4) 一起删掉，
        # 此时经 2 中转的间接对应随支路一并消失
        nodes = [1, 2, 3, 4, 5, 6]
        edges = [(1, 2), (1, 3), (2, 4), (3, 4), (4, 5), (5, 6)]
        conn = self.init_graph(nodes, edges)
        receipt = repo.apply_change(
            conn, "op-drop-branch",
            [
                {"seq": 1, "op": "remove",
                 "prerequisite": 1, "dependent": 2},
                {"seq": 2, "op": "remove",
                 "prerequisite": 2, "dependent": 4},
            ],
        )
        self.assertEqual(
            receipt["removed_pairs"],
            [(1, 2), (2, 4), (2, 5), (2, 6)],
        )
        # 替代路径 (1->3->4...) 上的可达对全部保留
        after = self.closure_snapshot(conn)
        for pair in [(1, 3), (1, 4), (1, 5), (1, 6), (3, 4), (3, 5), (3, 6)]:
            self.assertIn(pair, after)
        self.assert_closure_matches_reference(conn, nodes, repo.load_edges(conn))
        conn.close()

    def test_add_edge_creates_transitive_pairs_with_new_marker(self) -> None:
        nodes = [1, 2, 3, 4]
        edges = [(1, 2), (3, 4)]  # 两个不相连组件
        conn = self.init_graph(nodes, edges)
        receipt = repo.apply_change(
            conn, "op-bridge",
            [{"seq": 1, "op": "add", "prerequisite": 2, "dependent": 3}],
        )
        self.assertEqual(
            receipt["added_pairs"],
            [(1, 3), (1, 4), (2, 3), (2, 4)],
        )
        self.assertEqual(receipt["removed_pairs"], [])
        self.assert_closure_matches_reference(conn, nodes, repo.load_edges(conn))
        snap = self.closure_snapshot(conn)
        for pair in receipt["added_pairs"]:
            self.assertEqual(snap[pair], "op-bridge")
        # 原有对保留 INIT 标记
        self.assertEqual(snap[(1, 2)], "INIT")
        self.assertEqual(snap[(3, 4)], "INIT")
        conn.close()


class MultiAlternativePathTest(DepgraphTestCase):
    def test_three_disjoint_paths(self) -> None:
        # 1 经 2/3/4 三条互不相交路径到 5
        nodes = [1, 2, 3, 4, 5]
        edges = [
            (1, 2), (1, 3), (1, 4),
            (2, 5), (3, 5), (4, 5),
        ]
        conn = self.init_graph(nodes, edges)
        # 依次删掉两条路径上的边，(1,5) 始终存活
        for op_id, edge in (("op-rm-2", (1, 2)), ("op-rm-3", (1, 3))):
            receipt = repo.apply_change(
                conn, op_id,
                [{"seq": 1, "op": "remove",
                  "prerequisite": edge[0], "dependent": edge[1]}],
            )
            self.assertNotIn((1, 5), receipt["removed_pairs"])
            self.assert_closure_matches_reference(
                conn, nodes, repo.load_edges(conn)
            )
        # 最后一条路径也删掉，(1,5) 才消失
        receipt = repo.apply_change(
            conn, "op-rm-4",
            [{"seq": 1, "op": "remove", "prerequisite": 1, "dependent": 4}],
        )
        self.assertIn((1, 5), receipt["removed_pairs"])
        self.assert_closure_matches_reference(
            conn, nodes, repo.load_edges(conn)
        )
        conn.close()


class IsolatedNodeTest(DepgraphTestCase):
    def test_isolated_nodes_preserved_and_queriable(self) -> None:
        nodes, edges = synthetic.chain(8, isolated=3)  # 6,7,8 孤立
        conn = self.init_graph(nodes, edges)
        self.assertEqual(repo.load_nodes(conn), list(range(1, 9)))
        for iso in (6, 7, 8):
            self.assertEqual(repo.ancestors(conn, iso), [])
            self.assertEqual(repo.descendants(conn, iso), [])
        self.assert_closure_matches_reference(conn, nodes, edges)
        # 变更不涉及孤立节点时，它们依然孤立
        repo.apply_change(
            conn, "op-move",
            [
                {"seq": 1, "op": "remove", "prerequisite": 2, "dependent": 3},
                {"seq": 2, "op": "add", "prerequisite": 1, "dependent": 3},
            ],
        )
        for iso in (6, 7, 8):
            self.assertEqual(repo.ancestors(conn, iso), [])
        self.assert_closure_matches_reference(
            conn, nodes, repo.load_edges(conn)
        )
        conn.close()


class BatchReversalTest(DepgraphTestCase):
    def test_temporary_cycle_then_legal_final_dag(self) -> None:
        nodes = [1, 2, 3, 4]
        edges = [(1, 2), (2, 3), (3, 4)]
        conn = self.init_graph(nodes, edges)
        # 先加两条反向边（中间态有环），再删正向边，最终得到 4->3->2->1
        receipt = repo.apply_change(
            conn, "op-reverse",
            [
                {"seq": 1, "op": "add", "prerequisite": 2, "dependent": 1},
                {"seq": 2, "op": "add", "prerequisite": 3, "dependent": 2},
                {"seq": 3, "op": "add", "prerequisite": 4, "dependent": 3},
                {"seq": 4, "op": "remove", "prerequisite": 1, "dependent": 2},
                {"seq": 5, "op": "remove", "prerequisite": 2, "dependent": 3},
                {"seq": 6, "op": "remove", "prerequisite": 3, "dependent": 4},
            ],
        )
        self.assertEqual(
            receipt["added_pairs"],
            [(2, 1), (3, 1), (3, 2), (4, 1), (4, 2), (4, 3)],
        )
        self.assertEqual(
            receipt["removed_pairs"],
            [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)],
        )
        self.assert_closure_matches_reference(
            conn, nodes, repo.load_edges(conn)
        )
        conn.close()

    def test_noop_batch_returns_empty_diff(self) -> None:
        nodes = [1, 2, 3]
        edges = [(1, 2)]
        conn = self.init_graph(nodes, edges)
        receipt = repo.apply_change(
            conn, "op-noop",
            [
                {"seq": 1, "op": "add", "prerequisite": 1, "dependent": 2},
                {"seq": 2, "op": "remove", "prerequisite": 2, "dependent": 3},
            ],
        )
        self.assertEqual(receipt["added_pairs"], [])
        self.assertEqual(receipt["removed_pairs"], [])
        self.assert_closure_matches_reference(conn, nodes, edges)
        conn.close()


class RejectionTest(DepgraphTestCase):
    def test_final_cycle_rejected_with_verifiable_path(self) -> None:
        nodes = [1, 2, 3, 4]
        edges = [(1, 2), (2, 3), (3, 4)]
        conn = self.init_graph(nodes, edges)
        with self.assertRaises(BatchRejected) as ctx:
            repo.apply_change(
                conn, "op-cycle",
                [{"seq": 1, "op": "add", "prerequisite": 4, "dependent": 1}],
            )
        cycle = ctx.exception.cycle
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        # 环路径每条边都能在“假设应用后”的边集中找到
        final_edges = set(edges) | {(4, 1)}
        for a, b in zip(cycle, cycle[1:]):
            self.assertIn((a, b), final_edges)
        # 整批回滚：边与闭包原样
        self.assertEqual(repo.load_edges(conn), edges)
        self.assert_closure_matches_reference(conn, nodes, edges)
        conn.close()

    def test_unknown_node_and_self_loop_rejected(self) -> None:
        nodes, edges = synthetic.chain(4)
        conn = self.init_graph(nodes, edges)
        with self.assertRaises(BatchRejected):
            repo.apply_change(
                conn, "op-self",
                [{"seq": 1, "op": "add", "prerequisite": 2, "dependent": 2}],
            )
        with self.assertRaises(BatchRejected):
            repo.apply_change(
                conn, "op-unknown",
                [{"seq": 1, "op": "add", "prerequisite": 2, "dependent": 99}],
            )
        self.assertEqual(repo.load_edges(conn), edges)
        conn.close()

    def test_init_with_cycle_rejected(self) -> None:
        conn = self.reset()
        with self.assertRaises(BatchRejected):
            repo.initialize_graph(
                conn, [1, 2, 3], [(1, 2), (2, 3), (3, 1)]
            )
        self.assertEqual(repo.load_nodes(conn), [])
        conn.close()


class MarkerStabilityTest(DepgraphTestCase):
    def test_unaffected_closure_rows_never_rewritten(self) -> None:
        # 大图上做若干次小支路修改，逐次核查无影响行标记不被重写
        nodes, edges = synthetic.layered_dag(
            60, isolated=2, extra_edges=80, seed=11
        )
        conn = self.init_graph(nodes, edges)
        candidates = [e for e in edges if e[0] > 20]
        history: list[tuple[str, set]] = []
        for idx, edge in enumerate(candidates[:6]):
            before = self.closure_snapshot(conn)
            op_id = f"op-tweak-{idx}"
            repo.apply_change(
                conn, op_id,
                [
                    {"seq": 1, "op": "remove",
                     "prerequisite": edge[0], "dependent": edge[1]},
                    {"seq": 2, "op": "add",
                     "prerequisite": edge[0], "dependent": edge[1]},
                ],
            )
            after = self.closure_snapshot(conn)
            # 先删后加同一条边：最终闭包集合应完全恢复
            self.assertEqual(set(before), set(after))
            changed_rows = {p for p in before if before[p] != after[p]}
            history.append((op_id, changed_rows))
            self.assert_closure_matches_reference(
                conn, nodes, repo.load_edges(conn)
            )
        # 所有曾经存在、且未被本次操作真正增删的行，标记必须保持
        final_snap = self.closure_snapshot(conn)
        initial_only = {
            p for p, m in final_snap.items() if m == "INIT"
        }
        self.assertTrue(initial_only)  # 远离小支路的 INIT 行仍然存在
        conn.close()


if __name__ == "__main__":
    unittest.main()
