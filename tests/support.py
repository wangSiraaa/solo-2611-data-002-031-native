"""测试公共工具：独立测试库、状态重置、独立 Python 参照实现。

参照模型完全用 Python 字典实现连接与汇总，断言时与数据库物化结果逐行比对；
它和 verify.py 的 SQL 重算相互独立，形成第二重交叉验证。
"""

from __future__ import annotations

import os
import unittest

from incjoin import generator, service
from incjoin.batches import load_batch_document
from incjoin.db import connect, migrate

TEST_DSN = os.environ.get("INCJOIN_TEST_DSN", "postgresql://127.0.0.1:55432/taskdb_test")


def reset_database(dsn: str = TEST_DSN) -> None:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE batch_receipt, key_summary, join_detail, "
            "l_source, r_source"
        )
        conn.commit()


class DatabaseTestCase(unittest.TestCase):
    """每个用例在独立测试库上从空库开始。"""

    dsn: str = TEST_DSN

    @classmethod
    def setUpClass(cls) -> None:
        from incjoin.db import ensure_database

        ensure_database(cls.dsn)
        migrate(cls.dsn)

    def setUp(self) -> None:
        reset_database(self.dsn)

    # -- 便捷封装 --------------------------------------------------------

    def load_initial(self) -> None:
        service.load_snapshot(
            self.dsn,
            generator.INITIAL_LEFT,
            generator.INITIAL_RIGHT,
        )

    def apply_doc(self, doc: dict) -> dict:
        return service.apply_batch(self.dsn, load_batch_document(
            __import__("json").dumps(doc)
        ))

    def apply_named(self, name: str) -> dict:
        return self.apply_doc(generator.SCENARIOS[name])

    # -- 数据库快照 ------------------------------------------------------

    def snapshot(self) -> dict:
        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT row_id, join_key, qty, last_changed_batch "
                "FROM l_source ORDER BY row_id"
            )
            left = cur.fetchall()
            cur.execute(
                "SELECT row_id, join_key, unit_cents, last_changed_batch "
                "FROM r_source ORDER BY row_id"
            )
            right = cur.fetchall()
            cur.execute(
                "SELECT l_row_id, r_row_id, join_key, qty, unit_cents, "
                "amount_cents, last_changed_batch FROM join_detail "
                "ORDER BY l_row_id, r_row_id"
            )
            detail = cur.fetchall()
            cur.execute(
                "SELECT join_key, pair_count, qty_total, amount_total_cents, "
                "last_changed_batch FROM key_summary ORDER BY join_key"
            )
            summary = cur.fetchall()
            cur.execute("SELECT batch_id FROM batch_receipt ORDER BY batch_id")
            receipts = [r[0] for r in cur.fetchall()]
        return {
            "left": left, "right": right, "detail": detail,
            "summary": summary, "receipts": receipts,
        }

    # -- 纯 Python 参照模型 ----------------------------------------------

    @staticmethod
    def expected_materialization(left_rows, right_rows) -> tuple[dict, dict]:
        by_key_l: dict[str, list] = {}
        by_key_r: dict[str, list] = {}
        for row_id, key, qty, _ in left_rows:
            by_key_l.setdefault(key, []).append((row_id, qty))
        for row_id, key, price, _ in right_rows:
            by_key_r.setdefault(key, []).append((row_id, price))

        detail: dict[tuple, tuple] = {}
        summary: dict[str, tuple] = {}
        for key in sorted(set(by_key_l) & set(by_key_r)):
            pairs = 0
            qty_total = 0
            amount_total = 0
            for l_id, qty in sorted(by_key_l[key]):
                for r_id, price in sorted(by_key_r[key]):
                    amount = qty * price
                    detail[(l_id, r_id)] = (
                        l_id, r_id, key, qty, price, amount
                    )
                    pairs += 1
                    qty_total += qty          # 按每个实际配对累加
                    amount_total += amount
            summary[key] = (key, pairs, qty_total, amount_total)
        return detail, summary

    def assert_matches_python_model(self) -> None:
        snap = self.snapshot()
        exp_detail, exp_summary = self.expected_materialization(
            snap["left"], snap["right"]
        )
        act_detail = {
            (d[0], d[1]): (d[0], d[1], d[2], d[3], d[4], d[5])
            for d in snap["detail"]
        }
        self.assertEqual(act_detail, exp_detail, "明细与 Python 参照模型不一致")
        act_summary = {
            s[0]: (s[0], s[1], s[2], s[3]) for s in snap["summary"]
        }
        self.assertEqual(act_summary, exp_summary, "汇总与 Python 参照模型不一致")
        return snap
