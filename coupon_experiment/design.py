"""分层完全随机化的分配空间。

设计（plan.json）：每层在固定的 n_s 个入组家庭中，等概率抽取恰好 k_s
个处理名额；层间独立。合法分配的概率为 1 / Π_s C(n_s, k_s)。

随机化推断只允许在该空间内重抽：
- 小设计用 itertools.combinations 精确枚举，全部分配都满足固定名额；
- 大设计按层独立 sample(population, k=slots) 固定种子采样，同样保证名额；
绝不允许把家庭跨层重排，也不允许对订单做普通 bootstrap。
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from .models import Plan, StratumPlan


@dataclass(frozen=True)
class StratumSpace:
    id: str
    households: tuple[str, ...]
    treatment_slots: int

    @property
    def n_allocations(self) -> int:
        from math import comb

        return comb(len(self.households), self.treatment_slots)

    def enumerate_treated_sets(self) -> list[frozenset[str]]:
        return [
            frozenset(combo)
            for combo in itertools.combinations(self.households, self.treatment_slots)
        ]

    def sample_treated_set(self, rng: random.Random) -> frozenset[str]:
        return frozenset(rng.sample(self.households, self.treatment_slots))


@dataclass(frozen=True)
class Allocation:
    """一次合法分配：家庭 -> 是否处理。每层处理数恒等于固定名额。"""

    treated: frozenset[str]

    def is_treated(self, household_id: str) -> bool:
        return household_id in self.treated


class DesignSpace:
    def __init__(self, plan: Plan):
        self.spaces: tuple[StratumSpace, ...] = tuple(
            StratumSpace(
                id=s.id,
                households=tuple(s.households),
                treatment_slots=s.treatment_slots,
            )
            for s in plan.strata
        )
        self.household_stratum: dict[str, str] = {}
        for sp in self.spaces:
            for h in sp.households:
                self.household_stratum[h] = sp.id
        self.observed: Allocation | None = None

    @property
    def strata(self) -> tuple[StratumSpace, ...]:
        return self.spaces

    @property
    def n_allocations_exact(self) -> int:
        total = 1
        for sp in self.spaces:
            total *= sp.n_allocations
        return total

    # ---- 精确枚举 --------------------------------------------------------
    def enumerate_allocations(self) -> list[Allocation]:
        """层间笛卡尔积；每个元素都是满足各层固定名额的合法分配。"""
        per_stratum = [sp.enumerate_treated_sets() for sp in self.spaces]
        out: list[Allocation] = []
        for combo in itertools.product(*per_stratum):
            treated: set[str] = set()
            for ts in combo:
                treated.update(ts)
            out.append(Allocation(frozenset(treated)))
        return out

    # ---- 固定种子采样 ----------------------------------------------------
    def sample_allocations(self, n_draws: int, seed: int, label: str = "") -> list[Allocation]:
        """每层独立采样，保证每层处理名额恒定。

        各层使用由 (seed, 层 id, label) 派生的独立 Random，保证可复现，
        且不同检验/场景不会复用同一随机流。
        """
        rngs = {
            sp.id: random.Random(f"{seed}|{label}|{sp.id}")
            for sp in self.spaces
        }
        draws: list[Allocation] = []
        for _ in range(n_draws):
            treated: set[str] = set()
            for sp in self.spaces:
                treated.update(sp.sample_treated_set(rngs[sp.id]))
            draws.append(Allocation(frozenset(treated)))
        return draws

    def observed_allocation(self, arm_of: dict[str, str], treatment_arm: str) -> Allocation:
        treated = frozenset(h for h, a in arm_of.items() if a == treatment_arm)
        self.observed = Allocation(treated)
        return self.observed

    # ---- 合法性自检 ------------------------------------------------------
    def is_legal(self, alloc: Allocation) -> bool:
        """核对一次分配确实只在各层内部抽取且名额精确。"""
        for sp in self.spaces:
            n_treated = sum(1 for h in sp.households if h in alloc.treated)
            if n_treated != sp.treatment_slots:
                return False
        if not alloc.treated.issubset(set(self.household_stratum)):
            return False
        return True
