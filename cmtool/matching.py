"""段落对应：利用正文相似度与章节上下文建立候选，识别移动与局部改写。

原则：
- 段落编号只在各自版本内有意义，匹配完全基于正文与章节标题。
- 重复段落存在多个合理候选时，用章节标题消歧；仍不唯一则保持
  ambiguous，绝不按文件顺序取第一个。
- 拆分 / 合并只识别为无法可靠一对一迁移（split_suspected /
  merge_suspected），不自动解决。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .model import Document
from .textutil import nfc

CHAPTER_BONUS = 0.1   # 章节标题一致的加分
FLOOR = 0.45          # 进入候选列表的最低分
CONFIDENT = 0.6       # 可信对应的最低分
MARGIN = 0.15         # 与次优候选的最小差距
PAIR_SIM = 0.9        # 拆分/合并判定：拼接文本相似度阈值
PAIR_MARGIN = 0.1     # 拼接匹配需比最佳单段高出多少才覆盖原判定
TOP_K = 5             # 未解决项中保留的候选数


@dataclass
class Candidate:
    pid: str
    chapter_title: str
    score: float
    text_sim: float
    chapter_match: bool
    exact: bool
    preview: str


@dataclass
class ParaMatch:
    old_pid: str
    status: str  # confident / ambiguous / deleted / split_suspected / merge_suspected
    chosen: str | None
    candidates: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    exact: bool = False


def _sim(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _preview(text: str, n: int = 24) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _diff_summary(old_text: str, new_text: str, limit: int = 3) -> list:
    """局部改写摘要（NFC 视图上对齐，仅用于解释，不产生坐标）。"""
    ov, nv = nfc(old_text), nfc(new_text)
    sm = SequenceMatcher(None, ov, nv, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        parts.append(f"「{ov[i1:i2]}」→「{nv[j1:j2]}」")
        if len(parts) >= limit:
            break
    return parts


def _score_candidates(op, new_doc: Document, new_nfc: dict) -> list:
    on = nfc(op.text)
    cands = []
    for np_ in new_doc.paragraphs:
        nn = new_nfc[np_.pid]
        exact = on == nn
        sim = 1.0 if exact else _sim(on, nn)
        chap = op.chapter_title == np_.chapter_title
        score = sim + (CHAPTER_BONUS if chap else 0.0)
        cands.append(
            Candidate(
                pid=np_.pid,
                chapter_title=np_.chapter_title,
                score=round(score, 6),
                text_sim=round(sim, 6),
                chapter_match=chap,
                exact=exact,
                preview=_preview(np_.text),
            )
        )
    # 排序确定化：分数相同按段落 id，与输入数组顺序无关
    cands.sort(key=lambda c: (-c.score, c.pid))
    return cands


def match_paragraphs(old_doc: Document, new_doc: Document) -> dict:
    new_nfc = {p.pid: nfc(p.text) for p in new_doc.paragraphs}
    results = {}
    for op in old_doc.paragraphs:
        cands = _score_candidates(op, new_doc, new_nfc)
        ev = []
        exacts = [c for c in cands if c.exact]
        status, chosen, exact_flag = None, None, False
        if exacts:
            exact_flag = True
            chap_exact = [c for c in exacts if c.chapter_match]
            if len(chap_exact) == 1:
                status, chosen = "confident", chap_exact[0]
                ev.append(
                    f"正文完全一致，且章节标题「{op.chapter_title}」一致，唯一对应 {chosen.pid}"
                )
                if len(exacts) > 1:
                    others = "、".join(c.pid for c in exacts if c.pid != chosen.pid)
                    ev.append(
                        f"存在 {len(exacts)} 个文本完全相同的重复段落（{others}），"
                        "已按章节上下文而非文件顺序取舍"
                    )
            elif len(exacts) == 1:
                status, chosen = "confident", exacts[0]
                np_ = new_doc.by_id[chosen.pid]
                ev.append(
                    f"正文完全一致，唯一对应 {chosen.pid}"
                    f"（章节标题由「{op.chapter_title}」变为「{np_.chapter_title}」）"
                )
            else:
                status = "ambiguous"
                ids = "、".join(c.pid for c in exacts)
                ev.append(
                    f"{len(exacts)} 个候选段落文本完全相同（{ids}），"
                    "章节上下文无法唯一区分，不按文件顺序取第一个"
                )
        else:
            above = [c for c in cands if c.score >= FLOOR]
            if not above:
                status = "deleted"
                ev.append("新版本中不存在相似段落，原段落可能已删除")
            else:
                best = above[0]
                second = above[1] if len(above) > 1 else None
                if best.score >= CONFIDENT and (
                    second is None or best.score - second.score >= MARGIN
                ):
                    status, chosen = "confident", best
                    ev.append(
                        f"最佳候选 {best.pid}：文本相似度 {best.text_sim}"
                        + ("，章节标题一致" if best.chapter_match else "，章节标题不同")
                    )
                    for seg in _diff_summary(op.text, new_doc.by_id[best.pid].text):
                        ev.append(f"局部改写：{seg}")
                else:
                    status = "ambiguous"
                    if second is not None:
                        ev.append(
                            f"候选 {best.pid}（{best.score}）与 {second.pid}（{second.score}）"
                            "得分接近，无法可靠二选一"
                        )
                    else:
                        ev.append(f"最高相似度 {best.score} 不足，无可靠对应")
        if status == "confident":
            np_ = new_doc.by_id[chosen.pid]
            if np_.chapter_id != op.chapter_id:
                ev.append(
                    f"段落发生移动：章节「{op.chapter_title}」→「{np_.chapter_title}」"
                )
        results[op.pid] = ParaMatch(
            old_pid=op.pid,
            status=status,
            chosen=chosen.pid if chosen else None,
            candidates=cands[:TOP_K],
            evidence=ev,
            exact=exact_flag,
        )
    _detect_splits(old_doc, new_doc, results, new_nfc)
    _detect_merges(old_doc, new_doc, results, new_nfc)
    _detect_double_targets(results)
    return results


def _best_pair_score(text_nfc: str, others: list) -> tuple:
    """text_nfc 与相邻段落拼接的最佳相似度，返回 (分数, 两个段落id)。"""
    best = (0.0, None)
    for i in range(len(others) - 1):
        joined = others[i][0] + others[i + 1][0]
        s = _sim(text_nfc, joined)
        if s > best[0]:
            best = (s, (others[i][1], others[i + 1][1]))
    return best


def _detect_splits(old_doc, new_doc, results, new_nfc):
    """旧段落似乎被拆成相邻两段：拼接匹配显著优于任何单段。"""
    new_seq = [(new_nfc[p.pid], p.pid) for p in new_doc.paragraphs]
    for op in old_doc.paragraphs:
        pm = results[op.pid]
        if pm.exact:
            continue
        on = nfc(op.text)
        pair_score, pair_ids = _best_pair_score(on, new_seq)
        best_single = pm.candidates[0].text_sim if pm.candidates else 0.0
        if pair_score >= PAIR_SIM and pair_score > best_single + PAIR_MARGIN:
            pm.status = "split_suspected"
            pm.chosen = None
            pm.evidence.append(
                f"疑似拆分：{pair_ids[0]} + {pair_ids[1]} 拼接后与原文相似度 "
                f"{pair_score:.2f}，优于任何单段（{best_single:.2f}），"
                "无法可靠一对一迁移"
            )


def _detect_merges(old_doc, new_doc, results, new_nfc):
    """相邻两个旧段落似乎合并成一个新段落。"""
    paras = old_doc.paragraphs
    for i in range(len(paras) - 1):
        o1, o2 = paras[i], paras[i + 1]
        pm1, pm2 = results[o1.pid], results[o2.pid]
        if pm1.exact or pm2.exact:
            continue
        joined = nfc(o1.text) + nfc(o2.text)
        for np_ in new_doc.paragraphs:
            s = _sim(joined, new_nfc[np_.pid])
            if s < PAIR_SIM:
                continue
            b1 = pm1.candidates[0].text_sim if pm1.candidates else 0.0
            b2 = pm2.candidates[0].text_sim if pm2.candidates else 0.0
            if s > b1 + PAIR_MARGIN and s > b2 + PAIR_MARGIN:
                for pm, other in ((pm1, o2.pid), (pm2, o1.pid)):
                    pm.status = "merge_suspected"
                    pm.chosen = None
                    pm.evidence.append(
                        f"疑似与 {other} 合并为 {np_.pid}（拼接相似度 {s:.2f}），"
                        "无法可靠一对一迁移"
                    )
                break


def _detect_double_targets(results):
    """多个旧段落可信地对应到同一个新段落：按合并处理，全部降级。"""
    owners = {}
    for pid, pm in results.items():
        if pm.status == "confident" and pm.chosen:
            owners.setdefault(pm.chosen, []).append(pid)
    for target, olds in owners.items():
        if len(olds) > 1:
            for pid in olds:
                pm = results[pid]
                pm.status = "merge_suspected"
                pm.chosen = None
                pm.evidence.append(
                    f"与 {'、'.join(p for p in olds if p != pid)} 同时对应到 {target}，"
                    "疑似合并，无法可靠一对一迁移"
                )
