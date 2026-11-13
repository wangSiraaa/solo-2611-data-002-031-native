"""数据模型、输入加载与旧文一致性核对。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def canonical_json(obj) -> str:
    """规范化 JSON 序列化：键排序、紧凑分隔符。

    摘要只依赖内容，不依赖输入对象的键顺序。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Paragraph:
    pid: str
    text: str
    chapter_id: str
    chapter_title: str
    order: int  # 文档显示顺序


@dataclass
class Document:
    raw: dict
    digest: str
    paragraphs: list  # 按显示顺序
    by_id: dict


def document_from_dict(raw: dict) -> Document:
    paragraphs = []
    by_id = {}
    for ch in raw.get("chapters", []):
        for p in ch.get("paragraphs", []):
            pid = p["id"]
            if pid in by_id:
                raise ValueError(f"段落 id 重复: {pid}")
            para = Paragraph(
                pid=pid,
                text=p["text"],
                chapter_id=ch.get("chapter_id", ""),
                chapter_title=ch.get("title", ""),
                order=len(paragraphs),
            )
            paragraphs.append(para)
            by_id[pid] = para
    return Document(raw=raw, digest=digest_json(raw), paragraphs=paragraphs, by_id=by_id)


def load_document(path) -> Document:
    return document_from_dict(load_json(path))


@dataclass
class Comment:
    cid: str
    paragraph_id: str
    start: int
    end: int
    quote: str
    before: str
    after: str
    note: str
    raw: dict


def load_comments(path) -> list:
    raw = load_json(path)
    comments = []
    for item in raw:
        comments.append(
            Comment(
                cid=item["comment_id"],
                paragraph_id=item["paragraph_id"],
                start=int(item["start"]),
                end=int(item["end"]),
                quote=item.get("quote", ""),
                before=item.get("before", ""),
                after=item.get("after", ""),
                note=item.get("note", ""),
                raw=item,
            )
        )
    return comments


def validate_comment(c: Comment, doc: Document) -> list:
    """核对批注记录是否与旧文一致，返回问题列表（空列表表示一致）。

    只读取旧文做核对，绝不改动或用规范化文本覆盖原文。
    """
    para = doc.by_id.get(c.paragraph_id)
    if para is None:
        return [f"旧版本中不存在段落 {c.paragraph_id}"]
    text = para.text
    if not (0 <= c.start <= c.end <= len(text)):
        return [f"范围 [{c.start},{c.end}) 超出段落长度 {len(text)}"]
    problems = []
    sliced = text[c.start : c.end]
    if sliced != c.quote:
        problems.append(f"quote 与旧文切片不一致：记录 {c.quote!r}，实际 {sliced!r}")
    before = text[max(0, c.start - len(c.before)) : c.start]
    if before != c.before:
        problems.append(f"before 上下文不一致：记录 {c.before!r}，实际 {before!r}")
    after = text[c.end : c.end + len(c.after)]
    if after != c.after:
        problems.append(f"after 上下文不一致：记录 {c.after!r}，实际 {after!r}")
    return problems
