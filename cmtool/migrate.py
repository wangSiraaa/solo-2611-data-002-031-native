"""迁移主流程：核对旧文 -> 段落对应 -> 锚点推导 -> 逐条复核。"""
from __future__ import annotations

from .anchors import relocate
from .matching import match_paragraphs
from .model import validate_comment


def _public(c):
    return {
        "comment_id": c.cid,
        "old_paragraph_id": c.paragraph_id,
        "old_range": [c.start, c.end],
        "quote": c.quote,
        "note": c.note,
    }


def run_migration(old_doc, new_doc, comments) -> dict:
    para_matches = match_paragraphs(old_doc, new_doc)
    migrated = []
    unresolved = []
    for c in sorted(comments, key=lambda x: x.cid):
        problems = validate_comment(c, old_doc)
        if problems:
            unresolved.append({
                **_public(c),
                "reason": "source_mismatch",
                "detail": "批注记录与旧文不一致：" + "；".join(problems),
                "candidates": [],
            })
            continue
        pm = para_matches[c.paragraph_id]
        if pm.status != "confident":
            unresolved.append({
                **_public(c),
                "reason": pm.status,
                "detail": "；".join(pm.evidence),
                "candidates": [vars(cand) for cand in pm.candidates],
            })
            continue
        new_para = new_doc.by_id[pm.chosen]
        res = relocate(c, new_para, new_doc)
        if res.ok:
            # 逐条重新截取新文本核对已确认锚点
            sliced = new_para.text[res.start:res.end]
            if sliced != c.quote:
                unresolved.append({
                    **_public(c),
                    "reason": "anchor_check_failed",
                    "detail": f"复核失败：新文本切片 {sliced!r} 与引用 {c.quote!r} 不一致",
                    "candidates": [],
                })
                continue
            migrated.append({
                "comment_id": c.cid,
                "paragraph_id": new_para.pid,
                "start": res.start,
                "end": res.end,
                "quote": sliced,
                "note": c.note,
                "migration": {
                    "kind": "auto",
                    "old_paragraph_id": c.paragraph_id,
                    "old_range": [c.start, c.end],
                    "evidence": pm.evidence + res.evidence,
                },
            })
        else:
            entry = {
                **_public(c),
                "reason": res.reason,
                "detail": res.detail,
                "paragraph_match": {
                    "paragraph_id": new_para.pid,
                    "evidence": pm.evidence,
                },
                "candidates": [],
            }
            if res.extra:
                entry["anchor"] = res.extra
            unresolved.append(entry)
    return {
        "migrated": migrated,
        "unresolved": unresolved,
        "para_matches": para_matches,
    }
