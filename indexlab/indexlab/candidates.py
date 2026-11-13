"""候选索引生成：仅普通 B-tree、单列或 ≤3 列复合键、可选 INCLUDE。

规则（每条候选都记录来源查询与规则，可解释）：
  R1 单列：每个过滤谓词列各一条（等值/范围列）
  R2 等值+范围复合：(等值列..., 范围列) —— 等值列按查询中出现顺序
  R2b 列序对照：(范围列, 等值列...) —— 用于展示复合列顺序的影响
  R3 等值+排序复合：(等值列..., 排序列)，消除排序
  R4 覆盖：R2/R3 键 + INCLUDE(查询输出中该表其余少量列)；或 等值键+INCLUDE
  R5 连接键：连接谓词两侧列各一条单列候选（主键侧通常被去重规则剔除）

去重：规范化 (表, 键列, INCLUDE列) 后
  - 与已有索引完全相同 -> 剔除（等价）
  - 键是已有索引键的左前缀且 INCLUDE 被其覆盖 -> 剔除（冗余）
不做部分索引、表达式索引、分区索引。
"""
from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field

from .sqlparse_util import QueryStructure

MAX_KEY = 3
MAX_INCLUDE = 4


@dataclass(frozen=True)
class Candidate:
    table: str
    key_cols: tuple[str, ...]
    include_cols: tuple[str, ...] = ()

    def ddl(self, name: str, schema: str) -> str:
        cols = ", ".join(self.key_cols)
        inc = f" INCLUDE ({', '.join(self.include_cols)})" if self.include_cols else ""
        return f"CREATE INDEX {name} ON {schema}.{self.table} ({cols}){inc}"

    def label(self) -> str:
        s = f"{self.table}({', '.join(self.key_cols)})"
        if self.include_cols:
            s += f" INCLUDE({', '.join(self.include_cols)})"
        return s


@dataclass
class SourcedCandidate:
    cand: Candidate
    sources: list[str] = field(default_factory=list)  # ["q1:R2", ...]

    def to_json(self):
        return {
            "table": self.cand.table,
            "key_cols": list(self.cand.key_cols),
            "include_cols": list(self.cand.include_cols),
            "label": self.cand.label(),
            "sources": self.sources,
        }


def _add(pool: dict[Candidate, SourcedCandidate], cand: Candidate, src: str):
    if not cand.key_cols or len(cand.key_cols) > MAX_KEY:
        return
    if len(cand.include_cols) > MAX_INCLUDE:
        return
    if set(cand.include_cols) & set(cand.key_cols):
        cand = Candidate(cand.table, cand.key_cols,
                         tuple(c for c in cand.include_cols if c not in cand.key_cols))
    if cand in pool:
        if src not in pool[cand].sources:
            pool[cand].sources.append(src)
    else:
        pool[cand] = SourcedCandidate(cand, [src])


def generate(structures: list[QueryStructure], max_candidates: int) -> list[SourcedCandidate]:
    pool: dict[Candidate, SourcedCandidate] = {}
    for qs in structures:
        qid = qs.qid
        for table in set(qs.tables.values()):
            eq = qs.eq(table)
            rng = qs.rng(table)
            order = [c for t, c, _d in qs.order_cols if t == table]
            sel = qs.select_cols.get(table, [])

            # R1 单列
            for c in eq + rng:
                _add(pool, Candidate(table, (c,)), f"{qid}:R1")

            # R2 等值+范围（等值在前）；等值列 >1 时也给出交换顺序
            if eq and rng:
                for r in rng[:1]:
                    _add(pool, Candidate(table, tuple(eq) + (r,)), f"{qid}:R2")
                    if len(eq) == 2:
                        _add(pool, Candidate(table, (eq[1], eq[0], r)), f"{qid}:R2swap")
                    # R2b 列序对照：范围列在前
                    _add(pool, Candidate(table, (r,) + tuple(eq)), f"{qid}:R2b")

            # R3 等值+排序
            if eq and order:
                _add(pool, Candidate(table, tuple(eq) + (order[0],)), f"{qid}:R3")

            # R4 覆盖：少量输出列时给 INCLUDE 变体
            if 0 < len(sel) <= 4:
                rest = tuple(c for c in sel if c not in eq and c not in rng)
                if rest:
                    if eq and rng:
                        _add(pool, Candidate(table, tuple(eq) + tuple(rng[:1]), rest),
                             f"{qid}:R4")
                    if eq and order:
                        _add(pool, Candidate(table, tuple(eq) + (order[0],), rest),
                             f"{qid}:R4")
                    if eq and not rng and not order:
                        _add(pool, Candidate(table, tuple(eq), rest), f"{qid}:R4")

        # R5 连接键
        for t1, c1, t2, c2 in qs.join_pairs:
            _add(pool, Candidate(t1, (c1,)), f"{qid}:R5")
            _add(pool, Candidate(t2, (c2,)), f"{qid}:R5")

    out = sorted(pool.values(), key=lambda sc: (sc.cand.table, sc.cand.key_cols,
                                                sc.cand.include_cols))
    return out[:max_candidates]


def dedup_against_existing(cands: list[SourcedCandidate],
                           existing: list[dict]) -> tuple[list[SourcedCandidate], list[dict]]:
    """剔除与已有索引等价/冗余的候选。返回 (保留, 剔除原因列表)。"""
    kept, dropped = [], []
    for sc in cands:
        c = sc.cand
        reason = None
        for e in existing:
            if e["table"] != c.table or e["am"] != "btree":
                continue
            ek, ei = e["key_cols"], e["include_cols"]
            if tuple(c.key_cols) == tuple(ek) and tuple(c.include_cols) == tuple(ei):
                reason = f"与已有索引 {e['name']} 定义等价"
                break
            if (tuple(c.key_cols) == tuple(ek[: len(c.key_cols)])
                    and set(c.include_cols) <= set(ei) | set(ek)):
                reason = f"键为已有索引 {e['name']} 的左前缀且 INCLUDE 被覆盖（冗余）"
                break
        if reason:
            dropped.append({"candidate": c.label(), "reason": reason,
                            "sources": sc.sources})
        else:
            kept.append(sc)
    return kept, dropped
