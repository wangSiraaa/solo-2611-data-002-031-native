"""批注锚点在新段落内的重新定位。

只在引用完整保留且位置唯一时给出新坐标；坐标一律为原文码点下标，
绝不按总长度差平移。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Comment, Document, Paragraph
from .textutil import context_ok, find_occurrences, nfc, view_with_map


@dataclass
class AnchorResult:
    ok: bool
    start: int = 0
    end: int = 0
    reason: str = ""
    detail: str = ""
    evidence: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _fail(reason, detail, **extra):
    return AnchorResult(ok=False, reason=reason, detail=detail, extra=extra)


def relocate(comment: Comment, new_para: Paragraph, new_doc: Document) -> AnchorResult:
    """在已确认对应的新段落内推导批注新位置。"""
    text = new_para.text
    if comment.start == comment.end:
        return _relocate_insertion(comment, text)

    quote = comment.quote
    hits = find_occurrences(text, quote)
    if len(hits) == 1:
        s = hits[0]
        e = s + len(quote)
        ev = [f"引用在新段落 {new_para.pid} 中唯一出现于 [{s},{e})"]
        if not context_ok(text, s, e, comment.before, comment.after):
            ev.append("注意：引用唯一，但前后上下文已有改动")
        return AnchorResult(ok=True, start=s, end=e, evidence=ev)
    if len(hits) > 1:
        ctx = [s for s in hits if context_ok(text, s, s + len(quote),
                                             comment.before, comment.after)]
        if len(ctx) == 1:
            s = ctx[0]
            return AnchorResult(
                ok=True, start=s, end=s + len(quote),
                evidence=[
                    f"引用在新段落中出现 {len(hits)} 次，仅 [{s},{s + len(quote)}) "
                    "处前后上下文与旧文一致，据此唯一定位"
                ],
            )
        return _fail(
            "quote_repeated",
            f"引用在新段落中出现 {len(hits)} 次（{hits}），前后上下文无法唯一确定",
            occurrences=hits,
        )
    # 原文中未命中：诊断是规范化差异、跨段移动还是删除
    view, to_orig = view_with_map(text)
    vhits = find_occurrences(view, nfc(quote))
    if vhits:
        s = to_orig[vhits[0]]
        e = to_orig[vhits[0] + len(nfc(quote))]
        return _fail(
            "normalization_changed",
            f"引用仅在 NFC 规范化后匹配：新文本切片 {text[s:e]!r} 与原引用 "
            f"{quote!r} 码点不同。保留原文，不自动迁移",
            new_slice=text[s:e],
        )
    elsewhere = []
    for p in new_doc.paragraphs:
        if p.pid == new_para.pid:
            continue
        hs = find_occurrences(p.text, quote)
        if hs:
            elsewhere.append({"paragraph_id": p.pid, "positions": hs})
    if elsewhere:
        return _fail(
            "quote_moved_across_paragraphs",
            "引用未出现在对应段落，但出现在其他段落，疑似跨段移动",
            elsewhere=elsewhere,
        )
    return _fail(
        "quote_deleted",
        "引用在新版本中已不存在（可能被修改或删除）",
    )


def _relocate_insertion(comment: Comment, text: str) -> AnchorResult:
    """插入点批注（start==end，quote 为空）：只能靠前后上下文定位。"""
    before, after = comment.before, comment.after
    if not before and not after:
        return _fail(
            "insertion_context_missing",
            "插入点批注缺少前后上下文，无法定位",
        )
    positions = [
        i for i in range(len(text) + 1)
        if context_ok(text, i, i, before, after)
    ]
    if len(positions) == 1:
        i = positions[0]
        return AnchorResult(
            ok=True, start=i, end=i,
            evidence=[f"插入点由前后上下文唯一定位于新段落偏移 {i}"],
        )
    if not positions:
        return _fail(
            "insertion_context_not_found",
            "插入点的前后上下文在新段落中不存在（周边文字已改动）",
        )
    return _fail(
        "insertion_context_ambiguous",
        f"前后上下文在新段落中匹配 {len(positions)} 个位置（{positions}），"
        "上下文不足，无法唯一确定插入点",
        positions=positions,
    )
