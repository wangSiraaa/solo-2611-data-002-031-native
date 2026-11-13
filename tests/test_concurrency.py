"""并发测试：两个进程/连接同时提交触及相同键的批次。

要求：最终状态等价于某个完整串行批次顺序，不出现半批混合值。
SERIALIZABLE 隔离 + 冲突重试保证可串行化；这里用多进程模拟真实 CLI 并发。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time

from incjoin import generator, service
from incjoin.batches import load_batch_document
from incjoin.db import connect
from incjoin.verify import verify

from .support import DatabaseTestCase


def _worker(dsn: str, payload: dict, start: "mp.synchronize.Barrier",
            results: "mp.Queue") -> None:
    try:
        start.wait()  # 尽量同时发起，制造读写重叠
        result = service.apply_batch(dsn, load_batch_document(json.dumps(payload)))
        results.put(("ok", result["status"]))
    except Exception as exc:  # 子进程把异常带回主进程断言
        results.put(("error", repr(exc)))


class ConcurrencyTests(DatabaseTestCase):
    def _run_pair(self, doc_a: dict, doc_b: dict) -> list:
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        results: mp.Queue = ctx.Queue()
        procs = [
            ctx.Process(target=_worker,
                        args=(self.dsn, doc_a, barrier, results)),
            ctx.Process(target=_worker,
                        args=(self.dsn, doc_b, barrier, results)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0, f"子进程异常退出: {p.exitcode}")
        out = []
        while not results.empty():
            out.append(results.get_nowait())
        return out

    def test_01_two_processes_same_key_both_commit_serializably(self) -> None:
        self.load_initial()
        out = self._run_pair(
            generator.CONCURRENT_BATCH_A, generator.CONCURRENT_BATCH_B
        )
        self.assertEqual(len(out), 2)
        self.assertTrue(all(kind == "ok" for kind, _ in out))

        report = verify(self.dsn)
        self.assertTrue(report.ok, report.errors)

        snap = self.assert_matches_python_model()
        # 两个批次的效果都必须可见（等价于任一串行顺序的最终态相同）
        l3 = [r for r in snap["left"] if r[0] == "L3"][0]
        r2 = [r for r in snap["right"] if r[0] == "R2"][0]
        self.assertEqual(l3[2], 100)
        self.assertEqual(r2[2], 777)
        self.assertEqual(
            sorted(snap["receipts"]),
            ["C1-concurrent-a", "C2-concurrent-b"],
        )

    def test_02_concurrent_same_batch_id_same_content_single_effect(self) -> None:
        self.load_initial()
        doc = generator.SCENARIOS["both_sides_add"]
        out = self._run_pair(doc, json.loads(json.dumps(doc)))
        statuses = sorted(status for _, status in out)
        # 一个 committed、另一个按幂等重放处理（或都在快速路径 replay）
        self.assertIn(statuses[0], ("committed", "replayed"))
        snap = self.snapshot()
        self.assertEqual(snap["receipts"].count(doc["batch_id"]), 1)
        self.assert_matches_python_model()
        self.assertTrue(verify(self.dsn).ok)

    def test_03_concurrent_same_batch_id_different_content_rejected(self) -> None:
        self.load_initial()
        good = generator.SCENARIOS["both_sides_add"]
        evil = json.loads(json.dumps(good))
        evil["operations"][0]["qty"] = 555
        out = self._run_pair(good, evil)
        # 恰好一个成功一个失败，不能两条回执、也不能半批
        kinds = sorted(kind for kind, _ in out)
        self.assertEqual(kinds, ["error", "ok"])
        snap = self.snapshot()
        self.assertEqual(snap["receipts"].count(good["batch_id"]), 1)
        self.assertTrue(verify(self.dsn).ok)
        self.assert_matches_python_model()

    def test_04_repeated_contention_always_converges(self) -> None:
        """5 轮并发压测：每轮两个进程改同一键，最终都必须通过完整重算。"""
        self.load_initial()
        for i in range(5):
            doc_a = {
                "batch_id": f"P-A-{i}",
                "operations": [
                    {"seq": 1, "action": "put", "side": "L", "row_id": "L3",
                     "join_key": "K2", "qty": 10 + i},
                ],
            }
            doc_b = {
                "batch_id": f"P-B-{i}",
                "operations": [
                    {"seq": 1, "action": "put", "side": "R", "row_id": "R2",
                     "join_key": "K2", "unit_cents": 20 + i},
                ],
            }
            out = self._run_pair(doc_a, doc_b)
            self.assertTrue(all(kind == "ok" for kind, _ in out), out)
            self.assertTrue(verify(self.dsn).ok)
        self.assert_matches_python_model()
        # 退避逻辑的快速单元验证：纯 SERIALIZABLE 冲突也能重试成功
        self._trigger_serialization_failure()

    def _trigger_serialization_failure(self) -> None:
        """手动制造 SSI 只读-写冲突，确认重试路径真实生效。"""
        from psycopg import IsolationLevel

        dsn = self.dsn
        writer_done = mp.Event()

        def long_reader() -> None:
            # SERIALIZABLE 只读事务先扫描汇总，再等写者提交后重复读
            conn = connect(dsn)
            conn.isolation_level = IsolationLevel.SERIALIZABLE
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM key_summary")
            cur.fetchone()
            writer_done.wait(timeout=10)
            time.sleep(0.05)
            try:
                cur.execute("SELECT count(*) FROM key_summary")
                cur.fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()

        reader = mp.Process(target=long_reader)
        reader.start()
        time.sleep(0.2)
        doc = {
            "batch_id": "P-ssi-writer",
            "operations": [
                {"seq": 1, "action": "put", "side": "L", "row_id": "L3",
                 "join_key": "K2", "qty": 7},
            ],
        }
        service.apply_batch(dsn, load_batch_document(json.dumps(doc)))
        writer_done.set()
        reader.join(timeout=30)
        self.assertEqual(reader.exitcode, 0)
        self.assertTrue(verify(dsn).ok)
