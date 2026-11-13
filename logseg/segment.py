"""核心切分：候选生成与全块完备解释搜索。

原则：
- 块内逐行提出 1..max_candidate_lines 行的连续候选，候选首行必须是
  时间戳行；逻辑消息只去掉首个时间戳前缀与末尾行终止符。
- 每个候选用全部规则做 fullmatch（DOTALL），一个候选可命中多条规则。
- 不做局部最长/最短/首条规则选择：只有覆盖整块所有行的解释才算数。
  0 个完备解释 => UNKNOWN（保留候选证据）；恰好 1 个 => 确认事件；
  >=2 个 => 歧义，保留至少两套完备见证（同边界不同规则也算歧义）。
- JSONL 对象自带边界，绝不跨对象拼接。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .convert import convert_field
from .manifest import SourceSpec
from .model import TIMESTAMP_PREFIX, Block
from .rules import RuleSet
from .sources import SourceDecodeError, decode_line, decode_lines, split_blocks, split_lines

# 见证保留上限与计数上限：块都很小，正常远达不到；上限只是保险。
WITNESS_CAP = 16
COUNT_CAP = 10_000


@dataclass(frozen=True)
class MatchedCandidate:
    """一个候选片段 + 一条命中规则。"""

    first_line: int  # 块内起始行下标（0 起始）
    n_lines: int
    rule_id: str
    message: str
    groups: dict[str, str]

    @property
    def last_line(self) -> int:
        return self.first_line + self.n_lines - 1


def build_message(block: Block, first: int, last: int) -> str:
    """构造候选逻辑消息：原始字节拼接，去掉末尾行终止符，
    解码后只去掉首个时间戳前缀；内部换行与时间戳行原样保留。
    """
    lines = block.lines[first : last + 1]
    raw = b"".join(line.content + line.term for line in lines)
    term = lines[-1].term
    if term:
        raw = raw[: -len(term)]
    text = raw.decode("utf-8", errors="strict")
    match = TIMESTAMP_PREFIX.match(text)
    if match is None:
        # 调用方保证首行是时间戳行；防御性兜底。
        raise SourceDecodeError(
            f"block {block.index} line {lines[0].number}: missing timestamp prefix"
        )
    return text[match.end() :]


def block_candidates(
    block: Block, ruleset: RuleSet
) -> tuple[dict[int, list[MatchedCandidate]], list[dict]]:
    """提出块内全部候选并逐条 fullmatch 所有规则。

    返回 (按起始行索引的命中候选, 候选证据列表)。证据包含未命中
    任何规则的候选，供 UNKNOWN/歧义块留存。
    """
    decoded = decode_lines(list(block.lines))
    by_start: dict[int, list[MatchedCandidate]] = {}
    evidence: list[dict] = []
    n = len(block.lines)
    for first in range(n):
        if TIMESTAMP_PREFIX.match(decoded[first]) is None:
            continue  # 非时间戳行不能开启候选
        for k in range(1, ruleset.max_candidate_lines + 1):
            last = first + k - 1
            if last >= n:
                break  # 候选不跨块
            message = build_message(block, first, last)
            matched = []
            for rule in ruleset.rules:
                m = rule.regex.fullmatch(message)
                if m is not None:
                    matched.append((rule, m.groupdict()))
            span = (block.lines[first].start, block.lines[last].end)
            evidence.append(
                {
                    "lines": [block.lines[first].number, block.lines[last].number],
                    "span": {"start": span[0], "end": span[1]},
                    "message": message,
                    "matched_rules": [rule.id for rule, _ in matched],
                }
            )
            for rule, groups in matched:
                by_start.setdefault(first, []).append(
                    MatchedCandidate(first, k, rule.id, message, groups)
                )
    return by_start, evidence


def complete_interpretations(
    n_lines: int,
    by_start: dict[int, list[MatchedCandidate]],
    witness_cap: int = WITNESS_CAP,
    count_cap: int = COUNT_CAP,
) -> tuple[list[tuple[MatchedCandidate, ...]], int, bool]:
    """回溯搜索覆盖全部 n_lines 行的完备解释。

    返回 (见证列表, 完备解释总数, 计数是否被截断)。同一行数边界
    若命中多条规则，是不同的解释（同边界不同规则也算歧义）。
    """
    witnesses: list[tuple[MatchedCandidate, ...]] = []
    total = 0
    truncated = False

    def rec(pos: int, acc: list[MatchedCandidate]) -> None:
        nonlocal total, truncated
        if truncated:
            return
        if pos == n_lines:
            total += 1
            if total > count_cap:
                truncated = True
                total = count_cap
                return
            if len(witnesses) < witness_cap:
                witnesses.append(tuple(acc))
            return
        for cand in by_start.get(pos, ()):
            acc.append(cand)
            rec(pos + cand.n_lines, acc)
            acc.pop()

    rec(0, [])
    return witnesses, total, truncated


def _make_event(
    event_id: str, cand: MatchedCandidate, block: Block, ruleset: RuleSet
) -> dict:
    rule = next(r for r in ruleset.rules if r.id == cand.rule_id)
    lines = block.lines[cand.first_line : cand.last_line + 1]
    first, last = lines[0], lines[-1]
    fields = {
        name: convert_field(cand.groups[name], ftype)
        for name, ftype in rule.fields.items()
    }
    raw = b"".join(line.content + line.term for line in lines).decode("utf-8")
    return {
        "id": event_id,
        "rule": rule.id,
        "lines": [first.number, last.number],
        "span": {"start": first.start, "end": last.end},
        "raw": raw,
        "message": cand.message,
        "fields": fields,
    }


def _witness_json(cand: MatchedCandidate, block: Block) -> dict:
    first, last = block.lines[cand.first_line], block.lines[cand.last_line]
    return {
        "rule": cand.rule_id,
        "lines": [first.number, last.number],
        "span": {"start": first.start, "end": last.end},
    }


def segment_block(
    block: Block, ruleset: RuleSet, source_name: str, event_counter: list[int]
) -> dict:
    """分析一个文本块，返回结构化块结果（含确认事件或证据）。"""
    by_start, evidence = block_candidates(block, ruleset)
    witnesses, total, truncated = complete_interpretations(
        len(block.lines), by_start
    )
    result = {
        "index": block.index,
        "lines": list(block.line_range),
        "span": {"start": block.start, "end": block.end},
        "candidates": evidence,
        "interpretation_count": total,
    }
    if truncated:
        result["count_truncated"] = True
    if total == 0:
        result["status"] = "unknown"
        if not evidence:
            result["notes"] = ["no timestamp-start line; no candidate proposed"]
        return result
    if total > 1:
        result["status"] = "ambiguous"
        result["witnesses"] = [
            [_witness_json(c, block) for c in interp] for interp in witnesses
        ]
        return result
    # 唯一完备解释 => 确认事件
    result["status"] = "confirmed"
    events = []
    for cand in witnesses[0]:
        event_counter[0] += 1
        events.append(
            _make_event(f"{source_name}#e{event_counter[0]}", cand, block, ruleset)
        )
    result["events"] = events
    return result


def _isolate_block(block: Block, status: str, declared: object) -> dict:
    """尾块按清单声明隔离：不做候选分析，绝不猜测。"""
    shown = "false" if declared is False else "absent"
    return {
        "index": block.index,
        "lines": list(block.line_range),
        "span": {"start": block.start, "end": block.end},
        "status": status,
        "isolated": True,
        "notes": [
            f"final block isolated by manifest (last_event_complete={shown}); "
            "not analyzed"
        ],
    }


def segment_text(raw: bytes, spec: SourceSpec, ruleset: RuleSet) -> dict:
    """切分 text 格式源。"""
    lines = split_lines(raw)
    blocks = split_blocks(lines)
    event_counter = [0]
    source_name = spec.path.rsplit("/", 1)[-1]
    block_results = []
    events: list[dict] = []
    # 尾块隔离由清单声明决定，与是否有末尾换行无关；声明为 true
    # 也不免除语法检查。若最后一块之后还有空行，则它结构上已完结。
    trailing = bool(blocks) and blocks[-1].lines[-1].end == len(raw)
    for i, block in enumerate(blocks):
        is_isolated_tail = (
            trailing and i == len(blocks) - 1 and spec.last_event_complete is not True
        )
        if is_isolated_tail:
            status = (
                "incomplete"
                if spec.last_event_complete is False
                else "unknown_completeness"
            )
            block_results.append(
                _isolate_block(block, status, spec.last_event_complete))
            continue
        result = segment_block(block, ruleset, source_name, event_counter)
        block_results.append(result)
        events.extend(result.get("events", []))
    return {"blocks": block_results, "events": events}


def segment_jsonl(raw: bytes, spec: SourceSpec, ruleset: RuleSet) -> dict:
    """切分 jsonl 格式源：每个对象即独立边界，只匹配其 message 字段。"""
    lines = split_lines(raw)
    objects = [line for line in lines if not line.is_blank]
    event_counter = [0]
    source_name = spec.path.rsplit("/", 1)[-1]
    obj_results = []
    events: list[dict] = []
    trailing = bool(objects) and objects[-1].end == len(raw)
    for index, line in enumerate(objects):
        base = {
            "index": index,
            "line": line.number,
            "span": {"start": line.start, "end": line.end},
        }
        if (
            trailing
            and index == len(objects) - 1
            and spec.last_event_complete is not True
        ):
            status = (
                "incomplete"
                if spec.last_event_complete is False
                else "unknown_completeness"
            )
            obj_results.append(
                base
                | {
                    "status": status,
                    "isolated": True,
                    "notes": ["final object isolated by manifest declaration"],
                }
            )
            continue
        text = decode_line(line)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            obj_results.append(
                base | {"status": "invalid_json", "error": str(exc), "raw": text}
            )
            continue
        message = obj.get("message")
        if not isinstance(message, str):
            obj_results.append(
                base | {"status": "invalid_object",
                        "error": "missing string field 'message'"}
            )
            continue
        matched = [
            (rule, rule.regex.fullmatch(message))
            for rule in ruleset.rules
        ]
        matched = [(rule, m) for rule, m in matched if m is not None]
        record = base | {
            "message": message,
            "attributes": {k: v for k, v in obj.items() if k != "message"},
            "matched_rules": [rule.id for rule, _ in matched],
        }
        if len(matched) == 0:
            record["status"] = "unknown"
        elif len(matched) > 1:
            # 同一边界命中多条规则：歧义，保留全部见证。
            record["status"] = "ambiguous"
            record["witnesses"] = [
                [{"rule": rule.id, "lines": [line.number, line.number],
                  "span": {"start": line.start, "end": line.end}}]
                for rule, _ in matched
            ]
        else:
            rule, m = matched[0]
            event_counter[0] += 1
            cand = MatchedCandidate(0, 1, rule.id, message, m.groupdict())
            pseudo_block = Block(0, (line,))
            event = _make_event(
                f"{source_name}#e{event_counter[0]}", cand, pseudo_block, ruleset
            )
            record["status"] = "confirmed"
            record["event"] = event
            events.append(event)
        obj_results.append(record)
    return {"objects": obj_results, "events": events}


def segment_source(raw: bytes, spec: SourceSpec, ruleset: RuleSet) -> dict:
    """按声明格式切分一个已核验的源，返回结构化结果。"""
    result = {
        "path": spec.path,
        "format": spec.format,
        "last_event_complete": spec.last_event_complete,
    }
    try:
        if spec.format == "text":
            result.update(segment_text(raw, spec, ruleset))
        elif spec.format == "jsonl":
            result.update(segment_jsonl(raw, spec, ruleset))
        else:
            result["status"] = "failed"
            result["error"] = f"unsupported format: {spec.format!r}"
            return result
    except SourceDecodeError as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        return result
    for event in result.get("events", []):
        event["source"] = spec.path  # 记录原文件
    result["status"] = "ok"
    return result
