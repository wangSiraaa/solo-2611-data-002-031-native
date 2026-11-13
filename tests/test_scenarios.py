"""题目要求的增量场景：两侧同增、先删后加、键迁移、单价归零、
最后配对删除、重复键、幂等/冲突、局部性。"""

from __future__ import annotations

import json

from incjoin import generator, service
from incjoin.batches import canonical_json, content_hash, load_batch_document
from incjoin.errors import BatchConflictError, BatchValidationError

from .support import DatabaseTestCase


class ScenarioTests(DatabaseTestCase):
    def test_01_initial_load_and_duplicate_keys(self) -> None:
        self.load_initial()
        snap = self.assert_matches_python_model()
        # K2 左右各两行 -> 4 个配对
        k2 = [s for s in snap["summary"] if s[0] == "K2"][0]
        self.assertEqual(k2[1], 4)  # pair_count = 2*2
        # 数量按配对累加：(3+5)*2 = 16，而不是来源去重后的 8
        self.assertEqual(k2[2], 16)
        # R3 单价为 0 的配对金额为 0 但配对仍在
        zero_pairs = [d for d in snap["detail"] if d[4] == 0]
        self.assertTrue(zero_pairs)
        self.assertTrue(all(d[5] == 0 for d in zero_pairs))

    def test_02_both_sides_add_same_key_cross_pairs(self) -> None:
        self.load_initial()
        before = self.snapshot()
        self.apply_named("both_sides_add")
        snap = self.assert_matches_python_model()
        k3 = [s for s in snap["summary"] if s[0] == "K3"][0]
        # 原 L4 × R4 = 1 对；新增 L6、R5 后 K3 上 2×2 = 4 对
        self.assertEqual(k3[1], 4)
        # 交叉配对（新L×新R、新L×旧R、旧L×新R）都必须存在
        pairs = {(d[0], d[1]) for d in snap["detail"] if d[2] == "K3"}
        self.assertEqual(
            pairs,
            {("L4", "R4"), ("L4", "R5"), ("L6", "R4"), ("L6", "R5")},
        )
        # 未受影响键 K1 的明细 last_changed_batch 保持 load
        k1_meta = {d[6] for d in before["detail"] if d[2] == "K1"}
        k1_after = {d[6] for d in snap["detail"] if d[2] == "K1"}
        self.assertEqual(k1_meta, k1_after)

    def test_03_delete_then_add_same_row_in_one_batch(self) -> None:
        self.load_initial()
        result = self.apply_named("delete_then_add")
        self.assertEqual(result["status"], "committed")
        snap = self.assert_matches_python_model()
        # L1 最终 qty=8，与 R1 的配对金额 8*100=800
        l1 = [d for d in snap["detail"] if d[0] == "L1"]
        self.assertEqual(len(l1), 1)
        self.assertEqual(l1[0][3], 8)
        self.assertEqual(l1[0][5], 800)
        # 只发布批次后的结果：回执只有一条
        self.assertEqual(snap["receipts"].count("B2-delete-then-add"), 1)

    def test_04_key_migration_cleans_old_key_and_builds_new(self) -> None:
        self.load_initial()
        self.apply_named("key_migration")
        snap = self.assert_matches_python_model()
        # L4 已迁到 K5：K3 不再有 L4 的配对
        self.assertFalse(any(d[0] == "L4" and d[2] == "K3"
                             for d in snap["detail"]))
        # K5 上 L4×R6 新配对存在
        self.assertTrue(
            any(d[0] == "L4" and d[1] == "R6" and d[2] == "K5"
                for d in snap["detail"])
        )
        # 迁移后 K3 只剩 R4（无左行），汇总应消失；新键 K5 汇总出现
        keys = [s[0] for s in snap["summary"]]
        self.assertNotIn("K3", keys)
        self.assertIn("K5", keys)
        # R4 仍在右表，只是没有配对
        self.assertTrue(any(r[0] == "R4" for r in snap["right"]))

    def test_05_unit_price_zero_keeps_pair_zeros_amount(self) -> None:
        self.load_initial()
        self.apply_named("unit_price_zero")
        snap = self.assert_matches_python_model()
        k1 = [s for s in snap["summary"] if s[0] == "K1"][0]
        self.assertEqual(k1[1], 1)          # 配对仍在
        self.assertEqual(k1[3], 0)          # 金额合计归零
        pair = [d for d in snap["detail"] if d[2] == "K1"][0]
        self.assertEqual(pair[4], 0)
        self.assertEqual(pair[5], 0)

    def test_06_last_pair_removed_drops_detail_and_summary(self) -> None:
        self.load_initial()
        self.apply_named("unit_price_zero")
        self.apply_named("last_pair_removed")
        snap = self.assert_matches_python_model()
        # K1 在两侧已无共存：明细与汇总都不应残留
        self.assertFalse(any(d[2] == "K1" for d in snap["detail"]))
        self.assertFalse(any(s[0] == "K1" for s in snap["summary"]))

    def test_07_duplicate_key_delete_does_not_touch_other_pairs(self) -> None:
        self.load_initial()
        self.apply_named("duplicate_keys")
        snap = self.assert_matches_python_model()
        k2 = [s for s in snap["summary"] if s[0] == "K2"][0]
        # K2 现存 L3,L7 × R2,R7 => 4 对
        self.assertEqual(k2[1], 4)
        pairs = {(d[0], d[1]) for d in snap["detail"] if d[2] == "K2"}
        self.assertEqual(
            pairs,
            {("L3", "R2"), ("L3", "R7"), ("L7", "R2"), ("L7", "R7")},
        )
        # 被删的 L2/R3 配对全部消失
        self.assertFalse(
            any(d[0] == "L2" or d[1] == "R3" for d in snap["detail"])
        )

    def test_08_locality_unaffected_keys_untouched(self) -> None:
        self.load_initial()
        before = self.snapshot()
        self.apply_named("both_sides_add")  # 只影响 K3
        after = self.snapshot()
        # K1/K2/K4 的明细行（含 last_changed_batch）必须逐字节不变
        self.assertEqual(
            sorted(d for d in before["detail"] if d[2] != "K3"),
            sorted(d for d in after["detail"] if d[2] != "K3"),
        )
        self.assertEqual(
            sorted(s for s in before["summary"] if s[0] != "K3"),
            sorted(s for s in after["summary"] if s[0] != "K3"),
        )

    def test_09_replay_same_canonical_content_is_noop(self) -> None:
        self.load_initial()
        doc = generator.SCENARIOS["both_sides_add"]
        first = self.apply_doc(doc)
        self.assertEqual(first["status"], "committed")
        # 重新序列化（不同空白/键序），规范内容相同
        reserialized = json.dumps(
            {"operations": doc["operations"], "batch_id": doc["batch_id"]},
            indent=8,
        )
        second = service.apply_batch(
            self.dsn, load_batch_document(reserialized)
        )
        self.assertEqual(second["status"], "replayed")
        snap = self.snapshot()
        # 重放不产生新回执、不改 last_changed_batch
        self.assertEqual(snap["receipts"].count(doc["batch_id"]), 1)
        self.assertTrue(
            all(d[6] in ("load", doc["batch_id"]) for d in snap["detail"])
        )
        self.assertEqual(canonical_json(load_batch_document(reserialized)),
                         canonical_json(load_batch_document(json.dumps(doc))))
        self.assertEqual(
            content_hash(load_batch_document(reserialized)),
            content_hash(load_batch_document(json.dumps(doc))),
        )

    def test_10_same_batch_id_different_content_rejected(self) -> None:
        self.load_initial()
        self.apply_named("both_sides_add")
        evil = json.loads(json.dumps(generator.SCENARIOS["both_sides_add"]))
        evil["operations"][0]["qty"] = 42
        snap_before = self.snapshot()
        with self.assertRaises(BatchConflictError):
            self.apply_doc(evil)
        # 拒绝后状态与冲突前完全一致
        self.assertEqual(self.snapshot(), snap_before)

    def test_11_invalid_batches_roll_back_entirely(self) -> None:
        self.load_initial()
        before = self.snapshot()
        with self.assertRaises(BatchValidationError):
            self.apply_named("invalid_missing_delete")
        with self.assertRaises(BatchValidationError):
            self.apply_named("invalid_bad_qty")
        self.assertEqual(self.snapshot(), before)

    def test_12_static_validation_rules(self) -> None:
        cases = [
            {"batch_id": "b", "operations": []},                      # 空操作
            {"batch_id": "b", "operations": [
                {"seq": 1, "action": "put", "side": "X",
                 "row_id": "x", "join_key": "k", "qty": 1}]},         # 坏 side
            {"batch_id": "b", "operations": [
                {"seq": 1, "action": "put", "side": "L",
                 "row_id": "x", "join_key": "k", "qty": True}]},      # 布尔值
            {"batch_id": "b", "operations": [
                {"seq": 1, "action": "put", "side": "R",
                 "row_id": "x", "join_key": "k", "unit_cents": -1}]}, # 负数
            {"batch_id": "b", "operations": [
                {"seq": 2, "action": "put", "side": "L",
                 "row_id": "x", "join_key": "k", "qty": 1},
                {"seq": 2, "action": "delete", "side": "L",
                 "row_id": "x"}]},                                    # seq 重复
            {"batch_id": "b", "operations": [
                {"seq": 1, "action": "delete", "side": "L",
                 "row_id": "x", "qty": 1}]},                          # delete 带多余字段
            {"batch_id": "", "operations": [
                {"seq": 1, "action": "put", "side": "L",
                 "row_id": "x", "join_key": "k", "qty": 1}]},         # 空 batch_id
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(BatchValidationError):
                    load_batch_document(json.dumps(case))

    def test_13_multiple_modifications_same_row_same_batch(self) -> None:
        self.load_initial()
        doc = {
            "batch_id": "B-multi-mod",
            "operations": [
                {"seq": 1, "action": "put", "side": "L", "row_id": "L1",
                 "join_key": "K9", "qty": 100},
                {"seq": 2, "action": "put", "side": "L", "row_id": "L1",
                 "join_key": "K1", "qty": 20},
                {"seq": 3, "action": "put", "side": "L", "row_id": "L1",
                 "join_key": "K1", "qty": 30},
            ],
        }
        self.apply_doc(doc)
        snap = self.assert_matches_python_model()
        l1 = [r for r in snap["left"] if r[0] == "L1"][0]
        self.assertEqual((l1[1], l1[2]), ("K1", 30))
        # 中间态 K9 从未发布：无 K9 任何物化行
        self.assertFalse(any(d[2] == "K9" for d in snap["detail"]))
        self.assertFalse(any(s[0] == "K9" for s in snap["summary"]))
