"""单元测试：清单核验、字节偏移、切分状态矩阵、歧义见证、
类型转换、两版对比。预期值均从 fixtures 手工推导。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from logseg.compare import compare_revisions, compare_source
from logseg.convert import convert_field
from logseg.manifest import load_manifest, read_and_verify
from logseg.report import segment_all
from logseg.rules import load_ruleset

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def _source(seg: dict, name: str) -> dict:
    return next(s for s in seg["sources"] if s["path"].endswith(name))


def _statuses(source: dict) -> list[str]:
    units = source.get("blocks") or source.get("objects") or []
    return [u["status"] for u in units]


class SegmentationFixture(unittest.TestCase):
    """两版规则下全部冻结源的切分结果（类级共享，只算一次）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.seg_a = segment_all(FIXTURES, FIXTURES / "templates-a.json")
        cls.seg_b = segment_all(FIXTURES, FIXTURES / "templates-b.json")
        cls.cmp = compare_revisions(cls.seg_a, cls.seg_b)

    def test_all_sources_verified(self):
        for seg in (self.seg_a, self.seg_b):
            for src in seg["sources"]:
                self.assertTrue(src["verification"]["ok"], src["path"])
                self.assertEqual(src["status"], "ok", src["path"])

    def test_status_matrix(self):
        expected = {
            "app.log": (["confirmed", "unknown", "unknown"],
                        ["confirmed", "confirmed", "unknown"]),
            "grammar.log": (["confirmed", "ambiguous", "confirmed", "confirmed"],
                            ["ambiguous", "ambiguous", "confirmed", "confirmed"]),
            "incomplete.log": (["incomplete"], ["incomplete"]),
            "no_newline.log": (["confirmed"], ["confirmed"]),
            "orphan.log": (["unknown", "confirmed"], ["unknown", "confirmed"]),
            "structured.jsonl": (["confirmed", "unknown"],
                                 ["confirmed", "confirmed"]),
            "unknown_tail.log": (["unknown_completeness"],
                                 ["unknown_completeness"]),
        }
        for name, (exp_a, exp_b) in expected.items():
            self.assertEqual(_statuses(_source(self.seg_a, name)), exp_a,
                             f"A/{name}")
            self.assertEqual(_statuses(_source(self.seg_b, name)), exp_b,
                             f"B/{name}")

    def test_local_two_candidates_global_unique(self):
        """grammar.log 块0（A 版）：行1 局部有 head(1行) 与 job(3行)
        两个候选，但只有 [job] 能覆盖整块 -> 全局唯一确认。"""
        block = _source(self.seg_a, "grammar.log")["blocks"][0]
        self.assertEqual(block["status"], "confirmed")
        first_line_cands = [c for c in block["candidates"]
                            if c["lines"][0] == 1 and c["matched_rules"]]
        self.assertEqual({tuple(c["matched_rules"]) for c in first_line_cands},
                         {("head",), ("job",)})
        self.assertEqual(len(block["events"]), 1)
        self.assertEqual(block["events"][0]["rule"], "job")
        self.assertEqual(block["events"][0]["lines"], [1, 3])

    def test_no_local_greedy_choice(self):
        """app.log 块0（A/B）：1 行与 2 行候选都命中 payment_E42，
        贪心选短会留下无法解释的正文行；唯一完备解释是 2 行事件。"""
        for seg in (self.seg_a, self.seg_b):
            block = _source(seg, "app.log")["blocks"][0]
            self.assertEqual(block["status"], "confirmed")
            self.assertEqual(block["events"][0]["lines"], [1, 2])
            self.assertEqual(block["events"][0]["span"],
                             {"start": 0, "end": 80})

    def test_genuine_ambiguity_two_witnesses(self):
        """grammar.log 块1（A、B 两版同样）：[short_job] 与
        [head, end] 两套完备切分 -> 歧义，保留两套见证。"""
        for seg in (self.seg_a, self.seg_b):
            block = _source(seg, "grammar.log")["blocks"][1]
            self.assertEqual(block["status"], "ambiguous")
            self.assertEqual(block["interpretation_count"], 2)
            shapes = [[(w["rule"], tuple(w["lines"])) for w in witness]
                      for witness in block["witnesses"]]
            self.assertIn([("short_job", (5, 6))], shapes)
            self.assertIn([("head", (5, 5)), ("end", (6, 6))], shapes)

    def test_revision_b_introduces_new_ambiguity(self):
        """grammar.log 块0：A 版唯一 [job]；B 版新增 part_end 后
        [job] 与 [head, part_end] 并存 -> 歧义（规则改版制造的多解）。"""
        block_b = _source(self.seg_b, "grammar.log")["blocks"][0]
        self.assertEqual(block_b["status"], "ambiguous")
        shapes = [[(w["rule"], tuple(w["lines"])) for w in witness]
                  for witness in block_b["witnesses"]]
        self.assertIn([("job", (1, 3))], shapes)
        self.assertIn([("head", (1, 1)), ("part_end", (2, 3))], shapes)

    def test_unknown_block_keeps_candidate_evidence(self):
        """声明 complete 不免除语法检查：worker 行无规则命中 ->
        UNKNOWN，候选证据保留且 matched_rules 为空。"""
        block = _source(self.seg_a, "app.log")["blocks"][2]
        self.assertEqual(block["status"], "unknown")
        self.assertEqual(block["interpretation_count"], 0)
        self.assertEqual(len(block["candidates"]), 1)
        self.assertEqual(block["candidates"][0]["matched_rules"], [])

    def test_orphan_block_has_no_candidates(self):
        block = _source(self.seg_a, "orphan.log")["blocks"][0]
        self.assertEqual(block["status"], "unknown")
        self.assertEqual(block["candidates"], [])

    def test_tail_isolation_by_declaration(self):
        """incomplete.log 声明 false -> 隔离为 incomplete；
        unknown_tail.log 未声明 -> 隔离为 unknown_completeness；
        两者都不做候选分析，即使内容形似合法事件。"""
        inc = _source(self.seg_a, "incomplete.log")["blocks"][0]
        self.assertEqual(inc["status"], "incomplete")
        self.assertNotIn("candidates", inc)
        unk = _source(self.seg_a, "unknown_tail.log")["blocks"][0]
        self.assertEqual(unk["status"], "unknown_completeness")
        self.assertNotIn("candidates", unk)

    def test_no_newline_complete_parses(self):
        """无末尾换行但声明 complete -> 正常解析。"""
        event = _source(self.seg_a, "no_newline.log")["events"][0]
        self.assertEqual(event["rule"], "payment_E42")
        self.assertEqual(event["span"], {"start": 0, "end": 58})

    def test_byte_offsets_are_bytes_not_chars(self):
        """JSONL 对象含 3 字节 UTF-8 字符"多"：区间必须是字节长度。"""
        raw = (FIXTURES / "sources" / "structured.jsonl").read_bytes()
        first_line = raw.split(b"\n")[0]
        src = _source(self.seg_a, "structured.jsonl")
        obj0, obj1 = src["objects"]
        self.assertEqual(obj0["span"], {"start": 0, "end": len(first_line) + 1})
        self.assertEqual(obj1["span"]["start"], len(first_line) + 1)
        self.assertEqual(obj1["span"]["end"], len(raw))
        # 字节长度 != 字符长度（"多" 1 字符 = 3 字节）
        char_len = len(first_line.decode("utf-8"))
        self.assertEqual(len(first_line) - char_len, 2)
        self.assertEqual(obj0["span"]["end"] - obj0["span"]["start"],
                         len(first_line) + 1)

    def test_event_spans_tile_block(self):
        block = _source(self.seg_a, "grammar.log")["blocks"][3]
        events = block["events"]
        self.assertEqual([e["rule"] for e in events],
                         ["phase_begin", "phase_done"])
        self.assertEqual(events[0]["span"]["end"], events[1]["span"]["start"])
        self.assertEqual(events[0]["span"]["start"], block["span"]["start"])
        self.assertEqual(events[1]["span"]["end"], block["span"]["end"])

    def test_jsonl_objects_never_concatenated(self):
        """每个 JSONL 对象独立匹配：A 版 obj1 的 E43 无规则 -> unknown，
        绝不与 obj0 拼接。"""
        src = _source(self.seg_a, "structured.jsonl")
        self.assertEqual(src["objects"][0]["status"], "confirmed")
        self.assertEqual(src["objects"][1]["status"], "unknown")
        self.assertEqual(src["objects"][1]["matched_rules"], [])


class Conversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seg_b = segment_all(FIXTURES, FIXTURES / "templates-b.json")

    def test_string_keeps_leading_zeros(self):
        self.assertEqual(convert_field("001", "string")["value"], "001")
        event = _source(self.seg_b, "no_newline.log")["events"][0]
        self.assertEqual(event["fields"]["user"]["value"], "003")

    def test_decimal_is_exact(self):
        self.assertEqual(convert_field("0.10", "decimal")["value"], "0.10")
        self.assertEqual(convert_field("0.00", "decimal")["value"], "0.00")
        event = _source(self.seg_b, "no_newline.log")["events"][0]
        self.assertEqual(event["fields"]["amount"]["value"], "0.00")

    def test_decimal_failure_keeps_raw_and_reason(self):
        failed = convert_field("bad", "decimal")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["value"], "bad")
        self.assertIn("bad", failed["error"])
        event = _source(self.seg_b, "orphan.log")["events"][0]
        amount = event["fields"]["amount"]
        self.assertFalse(amount["ok"])
        self.assertEqual(amount["value"], "bad")

    def test_int_conversion(self):
        self.assertEqual(convert_field("42", "int")["value"], 42)
        self.assertFalse(convert_field("4.2", "int")["ok"])

    def test_conversion_never_changes_segmentation(self):
        """orphan.log 的 amount=bad 在 B 版转换失败，但切分与 A 版
        完全一致（转换成败不暗选切分）。"""
        seg_a = segment_all(FIXTURES, FIXTURES / "templates-a.json")
        blk_a = _source(seg_a, "orphan.log")["blocks"][1]
        blk_b = _source(self.seg_b, "orphan.log")["blocks"][1]
        self.assertEqual(blk_a["status"], blk_b["status"])
        self.assertEqual(blk_a["events"][0]["span"],
                         blk_b["events"][0]["span"])


class ComparisonFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seg_a = segment_all(FIXTURES, FIXTURES / "templates-a.json")
        cls.seg_b = segment_all(FIXTURES, FIXTURES / "templates-b.json")
        cls.cmp = compare_revisions(cls.seg_a, cls.seg_b)

    def _file(self, name: str) -> dict:
        return next(f for f in self.cmp["files"] if f["path"].endswith(name))

    def test_split_and_merge(self):
        groups = self._file("grammar.log")["groups"]
        kinds = {(g["kind"], g["span"]["start"]) for g in groups}
        self.assertIn(("split", 168), kinds)   # pair -> pair_part x2
        self.assertIn(("merge", 245), kinds)   # phase_begin+done -> phase
        split = next(g for g in groups if g["kind"] == "split")
        self.assertEqual(len(split["a"]["events"]), 1)
        self.assertEqual(len(split["b"]["events"]), 2)
        merge = next(g for g in groups if g["kind"] == "merge")
        self.assertEqual(len(merge["a"]["events"]), 2)
        self.assertEqual(len(merge["b"]["events"]), 1)

    def test_type_change_only_on_same_boundary(self):
        group = self._file("no_newline.log")["groups"][0]
        self.assertEqual(group["kind"], "same_boundary")
        diff = group["field_diffs"][0]
        self.assertEqual(diff["kind"], "type_changed")
        self.assertEqual(diff["a"]["type"], "string")
        self.assertEqual(diff["b"]["type"], "decimal")
        self.assertEqual(diff["a"]["value"], diff["b"]["value"])

    def test_only_in_b_with_opposite_context(self):
        only_b = self._file("app.log")["only_in_b"]
        self.assertEqual(len(only_b), 1)
        self.assertEqual(only_b[0]["rule"], "payment_E43")
        self.assertEqual(only_b[0]["opposite"]["status"], "unknown")

    def test_confirmed_event_lost_to_ambiguity(self):
        only_a = self._file("grammar.log")["only_in_a"]
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["rule"], "job")
        self.assertEqual(only_a[0]["opposite"]["status"], "ambiguous")

    def test_conversion_failure_visible_in_report(self):
        group = self._file("orphan.log")["groups"][0]
        diff = group["field_diffs"][0]
        self.assertEqual(diff["kind"], "type_changed")
        self.assertFalse(diff["b"]["ok"])
        self.assertEqual(diff["b"]["value"], "bad")

    def test_unresolvable_overlap(self):
        """合成的部分重叠：两边覆盖无法拼到共同终点。"""
        src_a = {"path": "x", "events": [
            {"id": "x#e1", "rule": "r", "span": {"start": 0, "end": 10},
             "fields": {}}]}
        src_b = {"path": "x", "events": [
            {"id": "x#e1", "rule": "r", "span": {"start": 0, "end": 6},
             "fields": {}},
            {"id": "x#e2", "rule": "r", "span": {"start": 8, "end": 15},
             "fields": {}}]}
        result = compare_source(src_a, src_b)
        self.assertEqual(result["groups"][0]["kind"], "unresolvable")


class ManifestVerification(unittest.TestCase):
    def test_tampered_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            shutil.copytree(FIXTURES, tmpdir / "fixtures")
            target = tmpdir / "fixtures" / "sources" / "app.log"
            target.write_bytes(target.read_bytes() + b"X")  # 长度不符
            seg = segment_all(tmpdir / "fixtures",
                              tmpdir / "fixtures" / "templates-a.json")
            failed = _source(seg, "app.log")
            self.assertEqual(failed["status"], "failed")
            self.assertIn("byte length mismatch", failed["error"])
            # 其他源不受影响
            self.assertEqual(_source(seg, "grammar.log")["status"], "ok")

    def test_sha_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            shutil.copytree(FIXTURES, tmpdir / "fixtures")
            target = tmpdir / "fixtures" / "sources" / "app.log"
            raw = bytearray(target.read_bytes())
            raw[0] ^= 0xFF  # 同长度改一个字节 -> sha256 不符
            target.write_bytes(bytes(raw))
            manifest = load_manifest(tmpdir / "fixtures")
            spec = next(s for s in manifest.sources if "app.log" in s.path)
            raw_bytes, verification = read_and_verify(spec, manifest.root)
            self.assertIsNone(raw_bytes)
            self.assertFalse(verification["ok"])
            self.assertEqual(verification["error"], "sha256 mismatch")


class RuleAmbiguity(unittest.TestCase):
    def test_same_boundary_different_rules_is_ambiguous(self):
        """同一边界命中两条不同规则 -> 歧义（合成微型规则集）。"""
        with tempfile.TemporaryDirectory() as tmp:
            rules_path = Path(tmp) / "rules.json"
            rules_path.write_text(json.dumps({
                "revision": "T", "match": "fullmatch_dotall",
                "max_candidate_lines": 2, "block_boundary": "blank_line",
                "rules": [
                    {"id": "r1", "pattern": "x=(?P<v>\\d+)",
                     "fields": {"v": "int"}},
                    {"id": "r2", "pattern": "x=(?P<v>\\d+)",
                     "fields": {"v": "int"}},
                ],
            }), encoding="utf-8")
            ruleset = load_ruleset(rules_path)
            from logseg.model import Block, Line
            from logseg.segment import segment_block
            content = b"2026-11-13T00:00:00Z x=1"
            line = Line(1, 0, len(content), content, b"")
            block = Block(0, (line,))
            result = segment_block(block, ruleset, "synthetic", [0])
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(result["interpretation_count"], 2)
            rules_in_witnesses = {w[0]["rule"] for w in result["witnesses"]}
            self.assertEqual(rules_in_witnesses, {"r1", "r2"})
            spans = {tuple(w[0]["span"].values()) for w in result["witnesses"]}
            self.assertEqual(len(spans), 1)  # 同边界


if __name__ == "__main__":
    unittest.main()
