"""文本工具：匹配视图（NFC）与原文坐标严格分离。

对齐、相似度、检索都在 NFC 视图上进行；任何下标结论必须通过
view_with_map 的映射回到原文码点坐标，再切原文核对。绝不用视图下标
直接切原文，也绝不用规范化文本覆盖原文。
"""
from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def view_with_map(text: str):
    """返回 (匹配视图, 视图边界 -> 原文边界 的映射表)。

    映射表长度为 len(view)+1，给出每个视图边界对应的原文边界。
    非等价块内部边界保守地映射到块起点；调用方只在核对诊断时使用。
    """
    view = nfc(text)
    if view == text:
        return view, list(range(len(text) + 1))
    n = len(view)
    to_orig = [None] * (n + 1)
    to_orig[0] = 0
    to_orig[n] = len(text)
    sm = SequenceMatcher(None, text, view, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1 + 1):
                to_orig[j1 + k] = i1 + k
        else:
            if to_orig[j1] is None:
                to_orig[j1] = i1
            to_orig[j2] = i2
            for j in range(j1 + 1, j2):
                if to_orig[j] is None:
                    to_orig[j] = i1
    for j in range(1, n + 1):
        if to_orig[j] is None:
            to_orig[j] = to_orig[j - 1]
    return view, to_orig


def find_occurrences(text: str, sub: str) -> list:
    """sub 在 text 中所有出现位置（原文码点下标）。"""
    if sub == "":
        return []
    hits = []
    i = text.find(sub)
    while i != -1:
        hits.append(i)
        i = text.find(sub, i + 1)
    return hits


def context_ok(text: str, s: int, e: int, before: str, after: str) -> bool:
    """检查 [s,e) 处的前后上下文是否与记录一致（原文精确比较）。"""
    if before and text[max(0, s - len(before)) : s] != before:
        return False
    if after and text[e : e + len(after)] != after:
        return False
    return True
