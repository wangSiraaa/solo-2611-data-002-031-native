"""并发测试：两个进程/连接同时加边，合起来成环时至多一个成功。

使用两个真实数据库连接 + 屏障，模拟两进程同时提交。咨询锁把写批次
串行化，后到者基于最新直接图重新判环并被拒绝。
"""

from __future__ import annotations

import threading
import unittest

from depgraph import reference, repo
from depgraph.repo import BatchRejected

from tests._helpers import DepgraphTestCase, connect_with_recovery_retry


class ConcurrentCycleTest(DepgraphTestCase):
    def test_A_to_B_and_B_to_A_only_one_commits(self) -> None:
        nodes = [1, 2]
        edges: list[tuple[int, int]] = []
        conn = self.init_graph(nodes, edges)
        conn.close()

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(name: str, op_id: str, edge: tuple[int, int]) -> None:
            local = connect_with_recovery_retry(self.dsn)
            try:
                barrier.wait()  # 两个线程同时进入 apply_change
                receipt = repo.apply_change(
                    local,
                    op_id,
                    [{"seq": 1, "op": "add",
                      "prerequisite": edge[0], "dependent": edge[1]}],
                )
                results[name] = ("ok", receipt)
            except BatchRejected as exc:
                results[name] = ("rejected", exc.reason, exc.cycle)
            except Exception as exc:  # pragma: no cover - 失败诊断
                results[name] = ("error", repr(exc))
            finally:
                local.close()

        t1 = threading.Thread(
            target=worker, args=("alpha", "op-a", (1, 2))
        )
        t2 = threading.Thread(
            target=worker, args=("beta", "op-b", (2, 1))
        )
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        statuses = {name: value[0] for name, value in results.items()}
        self.assertEqual(len(results), 2)
        # 恰好一个成功、一个因环被拒
        self.assertEqual(sorted(statuses.values()), ["ok", "rejected"])

        with connect_with_recovery_retry(self.dsn) as check:
            final_edges = repo.load_edges(check)
            # 无环：最终只能有一条边
            self.assertEqual(len(final_edges), 1)
            adj = reference.build_adjacency(final_edges, nodes)
            self.assertIsNone(reference.find_cycle(adj))
            # 闭包与直接图一致
            report = repo.verify(check)
            self.assertTrue(report["ok"], msg=str(report))
            # 只有一个成功批次登记
            count = check.execute(
                "SELECT count(*) FROM operations"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_concurrent_disjoint_edges_both_succeed(self) -> None:
        # 初始边 2->3：并发新增 1->2（接成 1->2->3）与 3->4（接成 2->3->4），
        # 两批各自安全，合起来为链 1->2->3->4，仍为 DAG，均应成功
        nodes = [1, 2, 3, 4]
        edges = [(2, 3)]
        conn = self.init_graph(nodes, edges)
        conn.close()

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(op_id: str, edge: tuple[int, int]) -> None:
            local = connect_with_recovery_retry(self.dsn)
            try:
                barrier.wait()
                repo.apply_change(
                    local, op_id,
                    [{"seq": 1, "op": "add",
                      "prerequisite": edge[0], "dependent": edge[1]}],
                )
                with lock:
                    outcomes.append("ok")
            except Exception as exc:  # pragma: no cover
                with lock:
                    outcomes.append(f"error: {exc!r}")
            finally:
                local.close()

        # 两条边互不相交（1->2 与 3->4），最终图仍为 DAG，均应成功
        t1 = threading.Thread(
            target=worker, args=("op-x", (1, 2))
        )
        t2 = threading.Thread(
            target=worker, args=("op-y", (3, 4))
        )
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        self.assertEqual(outcomes, ["ok", "ok"])

        with connect_with_recovery_retry(self.dsn) as check:
            self.assertEqual(
                sorted(repo.load_edges(check)),
                sorted(edges + [(1, 2), (3, 4)]),
            )
            self.assertTrue(repo.verify(check)["ok"])
            # 两个批次都登记成功
            self.assertEqual(
                check.execute("SELECT count(*) FROM operations").fetchone()[0],
                2,
            )


if __name__ == "__main__":
    unittest.main()
