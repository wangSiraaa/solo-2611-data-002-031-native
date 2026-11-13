"""独立枚举核对：对极小块用一套不依赖 logseg 切分实现的暴力
枚举器重新计算全部完备解释，与主算法输出逐一比对。

本文件故意只用 json / re / hashlib / pathlib 重新实现规范
（空行分块、时间戳前缀、1..max 行候选、fullmatch+DOTALL、
尾块隔离），不调用 logseg 的任何切分函数；logseg 仅作为
被核对的对象提供输出。不是用主算法自证。
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from logseg.report import segment_all  # 被核对的对象（主算法产物）

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

# ---- 以下是独立重实现（预言机），与 logseg 内部实现无共享代码 ----

TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


def oracle_blocks(text: str) -> list[list[str]]:
    """空行分块；末尾换行不产生幻影行。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def oracle_interpretations(
    block: list[str], patterns: list[tuple[str, str]], max_lines: int
) -> list[tuple]:
    """枚举全部组合切分：每个切口 2^(n-1) 种，片段 1..max_lines 行，
    片段首行须为时间戳行；消息 = 去掉首个时间戳前缀（fixtures 行
    均以 \n 结尾，join 等价于"去末尾终止符、内部换行保留"）。
    每个命中规则的 (切分, 规则) 组合都是一个解释。
    """
    n = len(block)
    results: list[tuple] = []

    def rec(i: int, acc: list) -> None:
        if i == n:
            results.append(tuple(acc))
            return
        for k in range(1, max_lines + 1):
            j = i + k
            if j > n:
                break
            m = TS.match(block[i])
            if not m:
                continue
            message = "\n".join(block[i:j])[m.end():]
            for rid, pattern in patterns:
                if re.fullmatch(pattern, message, re.DOTALL):
                    acc.append(((i, j), rid))
                    rec(j, acc)
                    acc.pop()

    rec(0, [])
    return results


def load_patterns(rules_path: Path) -> tuple[list[tuple[str, str]], int, str]:
    doc = json.loads(rules_path.read_text(encoding="utf-8"))
    patterns = [(r["id"], r["pattern"]) for r in doc["rules"]]
    return patterns, doc["max_candidate_lines"], doc["revision"]


def main_interpretations(block_result: dict) -> list[tuple]:
    """把主算法的块输出换算成与预言机相同的元组形式。"""
    first_line = block_result["lines"][0]

    def rel(piece: dict) -> tuple:
        a, b = piece["lines"]
        return ((a - first_line, b - first_line + 1), piece["rule"])

    if block_result["status"] == "confirmed":
        return [tuple(rel(e) for e in block_result["events"])]
    if block_result["status"] == "ambiguous":
        return [tuple(rel(w) for w in witness)
                for witness in block_result["witnesses"]]
    return []


class IndependentEnumeration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        cls.segs = {}
        for name in ("a", "b"):
            cls.segs[name] = segment_all(FIXTURES,
                                         FIXTURES / f"templates-{name}.json")

    def test_text_blocks_match_oracle(self):
        checked = 0
        for name, seg in self.segs.items():
            patterns, max_lines, _ = load_patterns(
                FIXTURES / f"templates-{name}.json")
            for entry in self.manifest["files"]:
                if entry["format"] != "text":
                    continue
                raw = (ROOT / entry["path"]).read_bytes()
                # 预言机也独立核验哈希，不依赖主算法的核验结论
                self.assertEqual(hashlib.sha256(raw).hexdigest(),
                                 entry["sha256"])
                text = raw.decode("utf-8")
                blocks = oracle_blocks(text)
                # 尾块隔离：声明非 true 且最后一块直达 EOF（无尾随空行）
                trailing = bool(blocks) and not text.endswith("\n\n")
                src = next(s for s in seg["sources"]
                           if s["path"] == entry["path"])
                self.assertEqual(len(src["blocks"]), len(blocks),
                                 f"{name}/{entry['path']} block count")
                for index, block_lines in enumerate(blocks):
                    result = src["blocks"][index]
                    if (trailing and index == len(blocks) - 1
                            and entry.get("last_event_complete") is not True):
                        self.assertIn(result["status"],
                                      ("incomplete", "unknown_completeness"))
                        continue
                    oracle = oracle_interpretations(block_lines, patterns,
                                                    max_lines)
                    main = main_interpretations(result)
                    self.assertEqual(
                        result["interpretation_count"], len(oracle),
                        f"{name}/{entry['path']} block {index} count")
                    self.assertEqual(
                        sorted(map(str, main)), sorted(map(str, oracle)),
                        f"{name}/{entry['path']} block {index} content")
                    checked += 1
        self.assertGreaterEqual(checked, 10)  # 确实覆盖了全部文本块

    def test_jsonl_objects_match_oracle(self):
        checked = 0
        for name, seg in self.segs.items():
            patterns, _, _ = load_patterns(FIXTURES / f"templates-{name}.json")
            for entry in self.manifest["files"]:
                if entry["format"] != "jsonl":
                    continue
                raw = (ROOT / entry["path"]).read_bytes()
                text = raw.decode("utf-8")
                lines = [ln for ln in text.split("\n") if ln != ""]
                src = next(s for s in seg["sources"]
                           if s["path"] == entry["path"])
                self.assertEqual(len(src["objects"]), len(lines))
                for index, line in enumerate(lines):
                    result = src["objects"][index]
                    if (index == len(lines) - 1
                            and entry.get("last_event_complete") is not True):
                        self.assertIn(result["status"],
                                      ("incomplete", "unknown_completeness"))
                        continue
                    obj = json.loads(line)
                    oracle = [rid for rid, pat in patterns
                              if re.fullmatch(pat, obj["message"], re.DOTALL)]
                    self.assertEqual(result["matched_rules"], oracle,
                                     f"{name}/{entry['path']} obj {index}")
                    checked += 1
        self.assertGreaterEqual(checked, 2)


if __name__ == "__main__":
    unittest.main()
