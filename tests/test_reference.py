"""独立参考算法自身的单测：找环、可达、祖先/后代、输入校验。"""

from __future__ import annotations

import unittest

from depgraph import reference
from depgraph.reference import ValidationError


class ReferenceAlgorithmTest(unittest.TestCase):
    def test_find_cycle_returns_verifiable_path(self) -> None:
        # 2 -> 3 -> 5 -> 2 成环，且路径首尾相同、每条边真实存在
        edges = [(1, 2), (2, 3), (3, 4), (3, 5), (5, 2), (4, 6)]
        adj = reference.build_adjacency(edges, [1, 2, 3, 4, 5, 6])
        cycle = reference.find_cycle(adj)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        edge_set = set(edges)
        for a, b in zip(cycle, cycle[1:]):
            self.assertIn((a, b), edge_set)

    def test_dag_has_no_cycle(self) -> None:
        edges = [(1, 2), (1, 3), (2, 4), (3, 4), (4, 5)]
        adj = reference.build_adjacency(edges, range(1, 6))
        self.assertIsNone(reference.find_cycle(adj))

    def test_reachable_pairs_excludes_self_and_includes_indirect(self) -> None:
        edges = [(1, 2), (2, 3), (3, 4)]
        pairs = reference.reachable_pairs(edges, range(1, 5))
        expected = {
            (1, 2), (1, 3), (1, 4),
            (2, 3), (2, 4),
            (3, 4),
        }
        self.assertEqual(pairs, expected)
        self.assertTrue(all(a != d for a, d in pairs))

    def test_isolated_node_kept_in_adjacency(self) -> None:
        adj = reference.build_adjacency([(1, 2)], [1, 2, 3])
        self.assertEqual(adj[3], [])
        self.assertEqual(reference.reachable_pairs([(1, 2)], [1, 2, 3]),
                         {(1, 2)})

    def test_duplicate_edges_collapsed(self) -> None:
        adj = reference.build_adjacency([(1, 2), (1, 2)], [1, 2])
        self.assertEqual(adj[1], [2])

    def test_unknown_node_and_self_loop_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            reference.build_adjacency([(1, 9)], [1, 2])
        with self.assertRaises(ValidationError):
            reference.build_adjacency([(2, 2)], [2])

    def test_ancestors_descendants_sorted(self) -> None:
        edges = [(1, 3), (2, 3), (3, 4), (3, 5)]
        adj = reference.build_adjacency(edges, range(1, 6))
        self.assertEqual(reference.ancestors_of(adj, 4), [1, 2, 3])
        self.assertEqual(reference.descendants_of(adj, 3), [4, 5])


if __name__ == "__main__":
    unittest.main()
