"""端到端与单元测试。samples/ 下的小样例同时充当移动、重复句子、
插入点、组合字符、局部删改的演示素材。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cmtool.decisions import apply_decisions, check_binding
from cmtool.migrate import run_migration
from cmtool.model import (document_from_dict, load_comments, load_document,
                          load_json)

FIXTURES = ROOT / "fixtures"
SAMPLES = ROOT / "samples"


def run_paths(old, new, comments):
    old_doc = load_document(old)
    new_doc = load_document(new)
    return run_migration(old_doc, new_doc, load_comments(comments))


def migrated_map(result):
    return {m["comment_id"]: m for m in result["migrated"]}


def unresolved_map(result):
    return {u["comment_id"]: u for u in result["unresolved"]}


class FixtureTest(unittest.TestCase):
    """提供的 fixtures：移动、局部改写、重复句、组合字符、插入点、删除。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_paths(FIXTURES / "old.json", FIXTURES / "new.json",
                               FIXTURES / "comments.json")
        cls.mig = migrated_map(cls.result)
        cls.unr = unresolved_map(cls.result)

    def test_expected_outcome(self):
        self.assertEqual(
            {cid: (m["paragraph_id"], m["start"], m["end"])
             for cid, m in self.mig.items()},
            {"C1": ("b4", 13, 22), "C2": ("b8", 7, 16), "C4": ("b7", 19, 21),
             "C5": ("b3", 5, 10), "C6": ("b4", 23, 23)},
        )
        self.assertEqual(set(self.unr), {"C3"})
        self.assertEqual(self.unr["C3"]["reason"], "deleted")

    def test_every_comment_has_disposition(self):
        self.assertEqual(len(self.mig) + len(self.unr), 6)

    def test_duplicate_resolved_by_chapter_not_file_order(self):
        # b2 在新文件中排在 b3 前面，但 a2 属于“采购”章，应对应 b3
        self.assertEqual(self.mig["C5"]["paragraph_id"], "b3")
        ev = "；".join(self.mig["C5"]["migration"]["evidence"])
        self.assertIn("重复段落", ev)

    def test_combining_quote_preserved_verbatim(self):
        self.assertEqual(self.mig["C4"]["quote"], "é")
        self.assertEqual(len(self.mig["C4"]["quote"]), 2)  # e + 组合符

    def test_local_edit_shifts_by_alignment_not_length(self):
        # a1->b4 在引用前插入“完整”二字：偏移 +2，而非按总长度差
        self.assertEqual((self.mig["C1"]["start"], self.mig["C1"]["end"]), (13, 22))


class SamplesTest(unittest.TestCase):
    """samples/ 下每个小样例与 expect.json 对照。"""

    def test_all_samples(self):
        for d in sorted(SAMPLES.iterdir()):
            if not d.is_dir():
                continue
            with self.subTest(sample=d.name):
                result = run_paths(d / "old.json", d / "new.json",
                                   d / "comments.json")
                expect = load_json(d / "expect.json")
                got_mig = {cid: [m["paragraph_id"], m["start"], m["end"]]
                           for cid, m in migrated_map(result).items()}
                got_unr = {cid: u["reason"]
                           for cid, u in unresolved_map(result).items()}
                self.assertEqual(got_mig, expect["migrated"])
                self.assertEqual(got_unr, expect["unresolved"])


def make_doc(chapters):
    return document_from_dict({
        "document_id": "t", "version": "x",
        "chapters": [
            {"chapter_id": cid, "title": title,
             "paragraphs": [{"id": pid, "text": text}
                            for pid, text in paras]}
            for cid, title, paras in chapters
        ],
    })


def make_comment(cid, pid, s, e, quote, before="", after=""):
    return load_comments_from([{
        "comment_id": cid, "paragraph_id": pid, "start": s, "end": e,
        "quote": quote, "before": before, "after": after, "note": "",
    }])


def load_comments_from(raw):
    import cmtool.model as m
    comments = []
    for item in raw:
        comments.append(m.Comment(
            cid=item["comment_id"], paragraph_id=item["paragraph_id"],
            start=item["start"], end=item["end"], quote=item.get("quote", ""),
            before=item.get("before", ""), after=item.get("after", ""),
            note=item.get("note", ""), raw=item,
        ))
    return comments


class SplitMergeTest(unittest.TestCase):
    def test_split_detected(self):
        old = make_doc([("c1", "章", [("p1", "甲句。乙句。")])])
        new = make_doc([("n1", "章", [("q1", "甲句。"), ("q2", "乙句。")])])
        comments = make_comment("C1", "p1", 3, 5, "乙句")
        result = run_migration(old, new, comments)
        unr = unresolved_map(result)
        self.assertEqual(unr["C1"]["reason"], "split_suspected")

    def test_merge_detected(self):
        old = make_doc([("c1", "章", [("p1", "甲句。"), ("p2", "乙句。")])])
        new = make_doc([("n1", "章", [("q1", "甲句。乙句。")])])
        comments = make_comment("C1", "p1", 0, 2, "甲句")
        result = run_migration(old, new, comments)
        unr = unresolved_map(result)
        self.assertEqual(unr["C1"]["reason"], "merge_suspected")


class ShuffleTest(unittest.TestCase):
    """打乱输入对象键顺序与批注数组顺序，结果应保持规范一致。"""

    def test_shuffle_invariant(self):
        def shuffled(obj, seed):
            import random
            rnd = random.Random(seed)
            if isinstance(obj, dict):
                items = list(obj.items())
                rnd.shuffle(items)
                return {k: shuffled(v, seed + i) for i, (k, v) in enumerate(items)}
            if isinstance(obj, list):
                return [shuffled(v, seed + i) for i, v in enumerate(obj)]
            return obj

        base = run_paths(FIXTURES / "old.json", FIXTURES / "new.json",
                         FIXTURES / "comments.json")
        for seed in (1, 2, 3):
            old_raw = shuffled(load_json(FIXTURES / "old.json"), seed)
            new_raw = shuffled(load_json(FIXTURES / "new.json"), seed + 100)
            comments_raw = load_json(FIXTURES / "comments.json")
            import random
            random.Random(seed).shuffle(comments_raw)
            comments_raw = shuffled(comments_raw, seed + 200)
            result = run_migration(
                document_from_dict(old_raw), document_from_dict(new_raw),
                load_comments_from(comments_raw))
            self.assertEqual(result["migrated"], base["migrated"])
            self.assertEqual(result["unresolved"], base["unresolved"])

    def test_digest_key_order_invariant(self):
        from cmtool.model import digest_json
        a = load_json(FIXTURES / "old.json")
        b = json.loads(json.dumps(a), object_pairs_hook=dict)
        # 反转所有对象的键顺序
        def rev(o):
            if isinstance(o, dict):
                return {k: rev(v) for k, v in reversed(list(o.items()))}
            if isinstance(o, list):
                return [rev(v) for v in o]
            return o
        self.assertEqual(digest_json(a), digest_json(rev(b)))


class DecisionsTest(unittest.TestCase):
    """人工决定：绑定摘要、校验引用、拒绝过期与错误决定。"""

    def setUp(self):
        self.old_doc = load_document(SAMPLES / "duplicate" / "old.json")
        self.new_doc = load_document(SAMPLES / "duplicate" / "new.json")
        self.comments = load_comments(SAMPLES / "duplicate" / "comments.json")
        self.by_id = {c.cid: c for c in self.comments}
        result = run_migration(self.old_doc, self.new_doc, self.comments)
        self.migrated = result["migrated"]
        self.unresolved = result["unresolved"]

    def _decisions(self, decisions, old_d=None, new_d=None):
        return {
            "source_digests": {
                "old": old_d if old_d is not None else self.old_doc.digest,
                "new": new_d if new_d is not None else self.new_doc.digest,
            },
            "decisions": decisions,
        }

    def test_choose_candidate_applies(self):
        dec = self._decisions([
            {"comment_id": "C3", "action": "choose_candidate",
             "paragraph_id": "qb1"},
        ])
        self.assertEqual(check_binding(dec, self.old_doc, self.new_doc), [])
        applied, rejected, remaining = apply_decisions(
            dec, self.old_doc, self.new_doc, self.by_id,
            self.migrated, self.unresolved)
        self.assertEqual(len(applied), 1)
        self.assertEqual((applied[0]["paragraph_id"], applied[0]["start"],
                          applied[0]["end"]), ("qb1", 0, 3))
        self.assertEqual(rejected, [])
        self.assertEqual(remaining, [])

    def test_set_range_applies_and_reslices(self):
        dec = self._decisions([
            {"comment_id": "C3", "action": "set_range",
             "paragraph_id": "qa1", "start": 0, "end": 2, "quote": "重复"},
        ])
        applied, rejected, remaining = apply_decisions(
            dec, self.old_doc, self.new_doc, self.by_id,
            self.migrated, self.unresolved)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["quote"], "重复")

    def test_wrong_quote_rejected(self):
        dec = self._decisions([
            {"comment_id": "C3", "action": "set_range",
             "paragraph_id": "qa1", "start": 0, "end": 3, "quote": "错误引用"},
        ])
        applied, rejected, remaining = apply_decisions(
            dec, self.old_doc, self.new_doc, self.by_id,
            self.migrated, self.unresolved)
        self.assertEqual(applied, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("不符", rejected[0]["reason"])
        self.assertEqual(len(remaining), 1)  # 未解决项不消失

    def test_stale_decisions_rejected(self):
        dec = self._decisions(
            [{"comment_id": "C3", "action": "choose_candidate",
              "paragraph_id": "qb1"}],
            new_d="0" * 64)
        problems = check_binding(dec, self.old_doc, self.new_doc)
        self.assertTrue(problems)

    def test_unknown_and_out_of_range_rejected(self):
        dec = self._decisions([
            {"comment_id": "C9", "action": "choose_candidate",
             "paragraph_id": "qb1"},
            {"comment_id": "C3", "action": "set_range",
             "paragraph_id": "qa1", "start": 0, "end": 99},
        ])
        applied, rejected, remaining = apply_decisions(
            dec, self.old_doc, self.new_doc, self.by_id,
            self.migrated, self.unresolved)
        self.assertEqual(applied, [])
        self.assertEqual(len(rejected), 2)


class CliTest(unittest.TestCase):
    def _run(self, *argv, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-m", "cmtool", *argv],
            cwd=cwd, capture_output=True, text=True)

    def test_compare_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run("compare", "--old", "fixtures/old.json",
                          "--new", "fixtures/new.json",
                          "--comments", "fixtures/comments.json", "--out", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self._run("verify", "--old", "fixtures/old.json",
                          "--new", "fixtures/new.json",
                          "--comments", "fixtures/comments.json", "--out", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("migrated.json", "unresolved.json", "report.html",
                         "summary.json"):
                self.assertTrue((Path(tmp) / name).exists())

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run("compare", "--old", "fixtures/old.json",
                      "--new", "fixtures/new.json",
                      "--comments", "fixtures/comments.json", "--out", tmp)
            mig_path = Path(tmp) / "migrated.json"
            mig = load_json(mig_path)
            mig["migrated"][0]["start"] += 1
            mig_path.write_text(json.dumps(mig, ensure_ascii=False),
                                encoding="utf-8")
            r = self._run("verify", "--old", "fixtures/old.json",
                          "--new", "fixtures/new.json",
                          "--comments", "fixtures/comments.json", "--out", tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("复核", r.stderr)

    def test_verify_detects_missing_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run("compare", "--old", "fixtures/old.json",
                      "--new", "fixtures/new.json",
                      "--comments", "fixtures/comments.json", "--out", tmp)
            unr_path = Path(tmp) / "unresolved.json"
            unr = load_json(unr_path)
            unr["unresolved"] = []  # 人为让未解决项消失
            unr_path.write_text(json.dumps(unr, ensure_ascii=False),
                                encoding="utf-8")
            r = self._run("verify", "--old", "fixtures/old.json",
                          "--new", "fixtures/new.json",
                          "--comments", "fixtures/comments.json", "--out", tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn("没有任何去向", r.stderr)

    def test_apply_decisions_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._run("compare", "--old", "samples/duplicate/old.json",
                      "--new", "samples/duplicate/new.json",
                      "--comments", "samples/duplicate/comments.json",
                      "--out", str(tmp))
            summary = load_json(tmp / "summary.json")
            decisions = {
                "source_digests": {"old": summary["old_digest"],
                                   "new": summary["new_digest"]},
                "decisions": [{"comment_id": "C3",
                               "action": "choose_candidate",
                               "paragraph_id": "qb1"}],
            }
            (tmp / "decisions.json").write_text(
                json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
            r = self._run("apply-decisions",
                          "--old", "samples/duplicate/old.json",
                          "--new", "samples/duplicate/new.json",
                          "--comments", "samples/duplicate/comments.json",
                          "--decisions", str(tmp / "decisions.json"),
                          "--out", str(tmp))
            self.assertEqual(r.returncode, 0, r.stderr)
            mig = load_json(tmp / "migrated.json")
            self.assertEqual(len(mig["migrated"]), 3)
            unr = load_json(tmp / "unresolved.json")
            self.assertEqual(unr["unresolved"], [])
            r = self._run("verify", "--old", "samples/duplicate/old.json",
                          "--new", "samples/duplicate/new.json",
                          "--comments", "samples/duplicate/comments.json",
                          "--out", str(tmp))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_apply_decisions_stale_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            decisions = {
                "source_digests": {"old": "0" * 64, "new": "0" * 64},
                "decisions": [{"comment_id": "C3",
                               "action": "choose_candidate",
                               "paragraph_id": "qb1"}],
            }
            (tmp / "decisions.json").write_text(
                json.dumps(decisions), encoding="utf-8")
            r = self._run("apply-decisions",
                          "--old", "samples/duplicate/old.json",
                          "--new", "samples/duplicate/new.json",
                          "--comments", "samples/duplicate/comments.json",
                          "--decisions", str(tmp / "decisions.json"),
                          "--out", str(tmp))
            self.assertEqual(r.returncode, 1)
            self.assertIn("拒绝沿用旧决定", r.stderr)
            # C3 保持未解决，不计入迁移
            unr = load_json(tmp / "unresolved.json")
            self.assertEqual({u["comment_id"] for u in unr["unresolved"]},
                             {"C3"})


if __name__ == "__main__":
    unittest.main()
