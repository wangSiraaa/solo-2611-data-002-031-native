"""两版事件语法（模板配置）的加载与校验。

模板同时承担两件事：识别候选事件（fullmatch + DOTALL）并声明
提取字段的类型。加载时校验配置自洽，不学习、不推断模板。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_MATCH_MODE = "fullmatch_dotall"
SUPPORTED_BLOCK_BOUNDARY = "blank_line"
SUPPORTED_TYPES = frozenset({"string", "int", "decimal"})


class RulesetError(ValueError):
    """模板配置不被支持或自相矛盾。"""


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: str
    regex: re.Pattern
    fields: dict[str, str]  # 字段名 -> 声明类型


@dataclass(frozen=True)
class RuleSet:
    revision: str
    max_candidate_lines: int
    rules: tuple[Rule, ...]


def load_ruleset(path: Path) -> RuleSet:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("match") != SUPPORTED_MATCH_MODE:
        raise RulesetError(
            f"unsupported match mode: {doc.get('match')!r} "
            f"(only {SUPPORTED_MATCH_MODE!r})"
        )
    if doc.get("block_boundary") != SUPPORTED_BLOCK_BOUNDARY:
        raise RulesetError(
            f"unsupported block boundary: {doc.get('block_boundary')!r}"
        )
    max_lines = doc["max_candidate_lines"]
    if not isinstance(max_lines, int) or max_lines < 1:
        raise RulesetError(f"invalid max_candidate_lines: {max_lines!r}")
    rules = []
    seen_ids = set()
    for raw_rule in doc["rules"]:
        rid = raw_rule["id"]
        if rid in seen_ids:
            raise RulesetError(f"duplicate rule id: {rid}")
        seen_ids.add(rid)
        fields = raw_rule.get("fields", {})
        bad_types = set(fields.values()) - SUPPORTED_TYPES
        if bad_types:
            raise RulesetError(f"rule {rid}: unsupported field types {bad_types}")
        regex = re.compile(raw_rule["pattern"], re.DOTALL)
        groups = set(regex.groupindex)
        if set(fields) != groups:
            raise RulesetError(
                f"rule {rid}: declared fields {sorted(fields)} != "
                f"pattern groups {sorted(groups)}"
            )
        rules.append(
            Rule(id=rid, pattern=raw_rule["pattern"], regex=regex, fields=fields)
        )
    return RuleSet(
        revision=doc["revision"],
        max_candidate_lines=max_lines,
        rules=tuple(rules),
    )
