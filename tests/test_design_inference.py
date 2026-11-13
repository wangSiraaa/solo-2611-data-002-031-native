"""分配空间合法性、估计量、随机化检验与反演区间的单元测试。"""

import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from coupon_experiment import inference as inf_mod
from coupon_experiment.design import Allocation, DesignSpace
from coupon_experiment.inference import (
    EXACT_ENUM_LIMIT,
    additive_effect_interval,
    overall_effect,
    randomization_tests,
    stratum_estimates,
)
from coupon_experiment.analysis import _descriptive
from coupon_experiment.validation import _parse_bool, load_plan, validate_and_aggregate
from coupon_experiment.models import (
    AssignmentRow,
    CoverageRow,
    EventRow,
    MembershipRow,
)

from tests.fixture_builders import (
    BIG_ASSIGNMENTS,
    BIG_COVERAGE,
    BIG_EVENTS,
    BIG_MEMBERS,
    BIG_PLAN,
    write_fixture,
)


def _build(paths):
    plan = load_plan(paths["plan"])
    rows = lambda p: p.read_text(encoding="utf-8").strip().split("\n")[1:]
    assignments = [
        AssignmentRow(h, s, a, _parse_bool(e))
        for h, s, a, e in (r.split(",") for r in rows(paths["assignments"]))
    ]
    members = [MembershipRow(*r.split(",")[:2]) for r in rows(paths["members"])]
    events = []
    for r in rows(paths["events"]):
        eid, pid, ts, amt = r.split(",")
        events.append(EventRow(eid, pid, ts, int(amt)))
    coverage = [
        CoverageRow(r.split(",")[0], _parse_bool(r.split(",")[1]))
        for r in rows(paths["coverage"])
    ]
    diags, metrics = validate_and_aggregate(
        plan, assignments, members, events, coverage
    )
    return plan, diags, metrics


class DesignSpaceTest(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d))
            self.plan, self.diags, self.metrics = _build(paths)
        self.space = DesignSpace(self.plan)

    def test_exact_enumeration_count_and_legality(self):
        # C(4,2) * C(2,1) = 6 * 2 = 12
        self.assertEqual(self.space.n_allocations_exact, 12)
        allocs = self.space.enumerate_allocations()
        self.assertEqual(len(allocs), 12)
        # 每个枚举分配都必须合法：层内名额精确、无跨层家庭
        for a in allocs:
            self.assertTrue(self.space.is_legal(a))
        # 独立用 itertools 重算一遍可行分配集合，不调用被测函数自证
        s1, s2 = self.plan.strata
        expected = set()
        for t1 in combinations(s1.households, s1.treatment_slots):
            for t2 in combinations(s2.households, s2.treatment_slots):
                expected.add(frozenset(t1) | frozenset(t2))
        self.assertEqual({a.treated for a in allocs}, expected)

    def test_sampled_allocations_respect_slots_and_reproduce(self):
        draws_a = self.space.sample_allocations(500, seed=1701, label="t")
        draws_b = self.space.sample_allocations(500, seed=1701, label="t")
        self.assertEqual([a.treated for a in draws_a], [a.treated for a in draws_b])
        self.assertEqual(len(draws_a), 500)
        for a in draws_a:
            self.assertTrue(self.space.is_legal(a))
            for sp in self.space.strata:
                k = sum(1 for h in sp.households if h in a.treated)
                self.assertEqual(k, sp.treatment_slots)

    def test_illegal_allocation_rejected(self):
        # 同层三个处理户，超过 S1 名额 2
        bad = Allocation(frozenset({"H01", "H02", "H03", "H06"}))
        self.assertFalse(self.space.is_legal(bad))
        # 跨层重排进来的外部家庭
        bad2 = Allocation(frozenset({"H01", "H03", "H99"}))
        self.assertFalse(self.space.is_legal(bad2))


class EstimatorTest(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d))
            self.plan, self.diags, self.metrics = _build(paths)

    def test_stratified_vs_naive_pooled(self):
        ests = stratum_estimates(self.plan, self.metrics)
        # 手算：
        # S1 处理 H03=400,H04=500 -> 450；对照 H01=300,H02=300 -> 300；差 150
        # S2 处理 H06=700；对照 H05=600；差 100
        self.assertEqual(ests[0].effect_cents, 150.0)
        self.assertEqual(ests[1].effect_cents, 100.0)
        # 权重 4/6, 2/6
        self.assertEqual(overall_effect(self.plan, ests),
                         150 * 4 / 6 + 100 * 2 / 6)
        # 朴素合并：处理均值 (400+500+700)/3，对照 (300+300+600)/3
        pooled = _descriptive(self.plan, self.metrics)[
            "naive_pooled_mean_difference_cents"
        ]
        self.assertAlmostEqual(
            pooled,
            (400 + 500 + 700) / 3 - (300 + 300 + 600) / 3,
        )
        self.assertNotEqual(pooled, overall_effect(self.plan, ests))


class SmallDesignTest(unittest.TestCase):
    """12 个合法分配的小设计：检验精确枚举，区间分辨率受限须如实开放。"""

    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d))
            self.plan, self.diags, self.metrics = _build(paths)
        self.space = DesignSpace(self.plan)
        self.observed = self.space.observed_allocation(
            {h: m.arm for h, m in self.metrics.items()}, self.plan.treatment_arm
        )

    def test_exact_test_pvalue_resolution(self):
        tests = randomization_tests(self.plan, self.space, self.metrics,
                                    self.observed)
        self.assertEqual({t.scope for t in tests}, {"overall", "S1", "S2"})
        for t in tests:
            self.assertEqual(t.mode, "exact")
            self.assertEqual(t.n_legal_allocations, 12)
            self.assertEqual(t.n_draws, 12)
            # 离散零分布下最小双侧 p = 1/12 > 0.05
            self.assertGreaterEqual(t.p_value_two_sided, 1 / 12)
            self.assertLessEqual(t.p_value_two_sided, 1.0)

    def test_unbounded_acceptance_region_reported_open(self):
        iv = additive_effect_interval(self.plan, self.space, self.metrics,
                                      self.observed)
        # 最小可达 p 高于 5%，任何 tau 都不被拒绝 -> 两端开放，不能冒充有限界
        self.assertTrue(iv.lower_open and iv.upper_open)
        self.assertEqual(iv.integer_interval_cents, (None, None))
        self.assertIn("开放端点", iv.note)
        # 但点估计仍在网格接受集内
        point = overall_effect(self.plan,
                               stratum_estimates(self.plan, self.metrics))
        self.assertIn(round(round(point / 50) * 50), iv.grid_accepted)

    def test_narrow_bounds_mark_open(self):
        import json
        from tests.fixture_builders import PLAN
        plan2 = json.loads(json.dumps(PLAN))
        plan2["inference"]["search_bounds_cents"] = [0, 100]
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d), plan=plan2)
            p2, _, m2 = _build(paths)
        space2 = DesignSpace(p2)
        obs2 = space2.observed_allocation(
            {h: mm.arm for h, mm in m2.items()}, p2.treatment_arm
        )
        iv = additive_effect_interval(p2, space2, m2, obs2)
        self.assertTrue(iv.lower_open)
        self.assertIsNone(iv.integer_interval_cents[0])
        self.assertIn("开放端点", iv.note)


class BigDesignTest(unittest.TestCase):
    """4900 个合法分配：精确检验可达到 5% 水平，反演区间可封闭。"""

    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(
                Path(d), plan=BIG_PLAN, assignments=BIG_ASSIGNMENTS,
                members=BIG_MEMBERS, events=BIG_EVENTS, coverage=BIG_COVERAGE,
            )
            self.plan, self.diags, self.metrics = _build(paths)
        self.space = DesignSpace(self.plan)
        self.observed = self.space.observed_allocation(
            {h: m.arm for h, m in self.metrics.items()}, self.plan.treatment_arm
        )

    def test_exact_enumeration_size(self):
        self.assertEqual(self.space.n_allocations_exact, 70 * 70)
        for a in self.space.enumerate_allocations()[:50]:
            self.assertTrue(self.space.is_legal(a))

    def test_point_estimate_recovers_350_and_interval_closed(self):
        ests = stratum_estimates(self.plan, self.metrics)
        point = overall_effect(self.plan, ests)
        self.assertEqual(point, 350.0)
        iv = additive_effect_interval(self.plan, self.space, self.metrics,
                                      self.observed)
        self.assertFalse(iv.lower_open or iv.upper_open)
        lo, hi = iv.integer_interval_cents
        self.assertIsNotNone(lo)
        self.assertIsNotNone(hi)
        self.assertLessEqual(lo, 350)
        self.assertLessEqual(350, hi)
        # 网格区间端点与整数细化的关系：细化只可能更宽（包含网格端点）
        glo, ghi = iv.grid_interval_cents
        self.assertLessEqual(lo, glo)
        self.assertGreaterEqual(hi, ghi)

    def test_monte_carlo_path_when_enumeration_limited(self):
        # 降低精确枚举门槛，强制走固定种子采样路径
        original = inf_mod.EXACT_ENUM_LIMIT
        inf_mod.EXACT_ENUM_LIMIT = 100
        try:
            tests = randomization_tests(self.plan, self.space, self.metrics,
                                        self.observed)
        finally:
            inf_mod.EXACT_ENUM_LIMIT = original
        for t in tests:
            self.assertEqual(t.mode, "monte_carlo")
            self.assertEqual(t.n_draws, 10_000)
            self.assertEqual(t.n_legal_allocations, 4900)
            self.assertTrue(0.0 <= t.p_value_two_sided <= 1.0)
        # 采样可复现
        inf_mod.EXACT_ENUM_LIMIT = 100
        try:
            tests2 = randomization_tests(self.plan, self.space, self.metrics,
                                         self.observed)
        finally:
            inf_mod.EXACT_ENUM_LIMIT = original
        self.assertEqual(
            [t.p_value_two_sided for t in tests],
            [t.p_value_two_sided for t in tests2],
        )

    def test_mc_interval_and_empty_acceptance_set(self):
        original = inf_mod.EXACT_ENUM_LIMIT
        inf_mod.EXACT_ENUM_LIMIT = 100
        try:
            iv = additive_effect_interval(self.plan, self.space, self.metrics,
                                          self.observed)
        finally:
            inf_mod.EXACT_ENUM_LIMIT = original
        self.assertIn("蒙特卡洛", iv.note)
        self.assertFalse(iv.lower_open or iv.upper_open)

        # 搜索范围远离真实效应且极窄 -> 空接受集，如实报告而非伪造区间
        self.plan.search_bounds = (9000, 9100)
        iv_empty = additive_effect_interval(
            self.plan, self.space, self.metrics, self.observed
        )
        self.assertEqual(iv_empty.grid_accepted, [])
        self.assertEqual(iv_empty.integer_interval_cents, (None, None))
        self.assertIn("没有任何 tau", iv_empty.note)
        self.plan.search_bounds = (-3000, 3000)


if __name__ == "__main__":
    unittest.main()
