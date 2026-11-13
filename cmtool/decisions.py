"""人工决定（decisions.json）的校验与执行。

决定通过 source_digests 绑定两份源文件摘要；源文变化后摘要即不匹配，
拒绝沿用旧决定。set_range 决定若带 quote，必须与新文实际切片一致。
"""
from __future__ import annotations

from .anchors import relocate


def check_binding(decisions_obj, old_doc, new_doc) -> list:
    dig = decisions_obj.get("source_digests", {})
    problems = []
    if dig.get("old") != old_doc.digest:
        problems.append("old 摘要不匹配（旧版源文已变化）")
    if dig.get("new") != new_doc.digest:
        problems.append("new 摘要不匹配（新版源文已变化）")
    return problems


def apply_decisions(decisions_obj, old_doc, new_doc, comments_by_id,
                    migrated, unresolved):
    """返回 (applied, rejected, remaining_unresolved)。"""
    applied = []
    rejected = []
    remaining = list(unresolved)
    migrated_ids = {m["comment_id"] for m in migrated}
    unresolved_ids = {u["comment_id"] for u in remaining}

    def _remove(cid):
        nonlocal remaining
        remaining = [u for u in remaining if u["comment_id"] != cid]

    for d in decisions_obj.get("decisions", []):
        cid = d.get("comment_id")
        action = d.get("action")
        if cid not in comments_by_id:
            rejected.append({**d, "reason": f"未知批注 {cid}"})
            continue
        if cid in migrated_ids:
            rejected.append({**d, "reason": "该批注已自动迁移，无需人工决定"})
            continue
        if cid not in unresolved_ids:
            rejected.append({**d, "reason": "该批注不在待处理清单中"})
            continue
        c = comments_by_id[cid]
        pid = d.get("paragraph_id")
        para = new_doc.by_id.get(pid)
        if para is None:
            rejected.append({**d, "reason": f"新版本中不存在段落 {pid}"})
            continue
        if action == "choose_candidate":
            res = relocate(c, para, new_doc)
            if not res.ok:
                rejected.append({**d, "reason": f"选定段落内无法定位引用：{res.detail}"})
                continue
            start, end = res.start, res.end
            quote = para.text[start:end]
            evidence = [f"人工选择候选段落 {pid}"] + res.evidence
        elif action == "set_range":
            try:
                start, end = int(d["start"]), int(d["end"])
            except (KeyError, TypeError, ValueError):
                rejected.append({**d, "reason": "缺少有效的 start/end"})
                continue
            if not (0 <= start <= end <= len(para.text)):
                rejected.append({
                    **d,
                    "reason": f"范围 [{start},{end}) 超出段落长度 {len(para.text)}",
                })
                continue
            quote = para.text[start:end]
            if "quote" in d and d["quote"] != quote:
                rejected.append({
                    **d,
                    "reason": f"决定中的 quote 与新文切片不符：实际为 {quote!r}",
                })
                continue
            evidence = [f"人工指定新范围 [{start},{end})，已核对切片 {quote!r}"]
        else:
            rejected.append({**d, "reason": f"未知 action {action!r}"})
            continue
        applied.append({
            "comment_id": cid,
            "paragraph_id": pid,
            "start": start,
            "end": end,
            "quote": quote,
            "note": c.note,
            "migration": {
                "kind": "human",
                "old_paragraph_id": c.paragraph_id,
                "old_range": [c.start, c.end],
                "evidence": evidence,
                "decision": d,
            },
        })
        _remove(cid)
        migrated_ids.add(cid)
    return applied, rejected, remaining
