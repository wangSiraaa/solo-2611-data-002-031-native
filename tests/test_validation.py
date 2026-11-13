"""资格检查与家庭汇总的单元测试：七类必检问题逐一覆盖。"""

import tempfile
import unittest
from pathlib import Path

from coupon_experiment.analysis import run_analysis
from coupon_experiment.models import (
    SEVERITY_ERROR,
    AssignmentRow,
    CoverageRow,
    EventRow,
    MembershipRow,
)
from coupon_experiment.validation import (
    is_eligible,
    _parse_bool,
    load_plan,
    validate_and_aggregate,
)

from tests.fixture_builders import (
    ASSIGNMENTS,
    COVERAGE,
    EVENTS,
    MEMBERS,
    PLAN,
    write_fixture,
)


def _load_and_validate(paths):
    plan = load_plan(paths["plan"])
    diags, metrics = validate_and_aggregate(
        plan,
        [AssignmentRow(h, s, a, _parse_bool(e))
         for h, s, a, e in _read(paths["assignments"])],
        [MembershipRow(r[0], r[1]) for r in _read(paths["members"])],
        [EventRow(r[0], r[1], r[2], _int(r[3])) for r in _read(paths["events"])],
        [CoverageRow(r[0], _parse_bool(r[1])) for r in _read(paths["coverage"])],
    )
    return plan, diags, metrics


def _read(p: Path):
    lines = p.read_text(encoding="utf-8").strip().split("\n")[1:]
    return [line.split(",") for line in lines]


def _int(v):
    try:
        return int(v)
    except ValueError:
        return None


def _codes(diags, severity=SEVERITY_ERROR):
    return {d.code for d in diags if d.severity == severity}


class EligibleBaselineTest(unittest.TestCase):
    def test_baseline_eligible_and_window_convention(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d))
            plan, diags, metrics = _load_and_validate(paths)
            self.assertTrue(is_eligible(diags))
            # E08 恰在 window_end，左闭右开 -> 窗口外
            self.assertEqual(metrics["H01"].in_window_event_ids, ["E01", "E02"])
            self.assertEqual(metrics["H01"].out_of_window_event_ids, ["E08"])
            self.assertEqual(metrics["H01"].metric_cents, 300)
            # H04 处理组但 exposed=false：保留且金额正常入算
            self.assertEqual(metrics["H04"].arm, "B")
            self.assertFalse(metrics["H04"].exposed)
            self.assertEqual(metrics["H04"].metric_cents, 500)
            # 无事件但覆盖完整才记零；本基线无零户，制造一个零户在下面用例
            self.assertFalse(any(m.zero_recorded for m in metrics.values()))

    def test_zero_only_with_complete_coverage(self):
        events = [e for e in EVENTS if e[0] != "E03"]  # H02 变无事件
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d), events=events)
            _, diags, metrics = _load_and_validate(paths)
            self.assertTrue(is_eligible(diags))
            self.assertTrue(metrics["H02"].zero_recorded)
            self.assertEqual(metrics["H02"].metric_cents, 0)

            cov = [c for c in COVERAGE if c[0] != "H02"] + [("H02", "false")]
            paths2 = write_fixture(Path(d), events=events, coverage=cov)
            _, diags2, metrics2 = _load_and_validate(paths2)
            self.assertIn("COVERAGE_INCOMPLETE", _codes(diags2))
            self.assertFalse(is_eligible(diags2))
            self.assertIsNone(metrics2["H02"].metric_cents)
            self.assertFalse(metrics2["H02"].zero_recorded)


class SevenRequiredChecksTest(unittest.TestCase):
    def _run(self, tmp, **kwargs):
        paths = write_fixture(Path(tmp), **kwargs)
        return _load_and_validate(paths)

    def test_missing_roster_assignment_and_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            _, diags, _ = self._run(
                d, assignments=[r for r in ASSIGNMENTS if r[0] != "H05"]
            )
            self.assertIn("ASSIGNMENT_MISSING", _codes(diags))
            self.assertFalse(is_eligible(diags))

            _, diags2, _ = self._run(
                d, coverage=[r for r in COVERAGE if r[0] != "H05"]
            )
            self.assertIn("COVERAGE_MISSING", _codes(diags2))

    def test_cross_arm_and_cross_stratum(self):
        with tempfile.TemporaryDirectory() as d:
            rows = ASSIGNMENTS + [("H01", "S1", "B", "true")]
            _, diags, _ = self._run(d, assignments=rows)
            self.assertIn("ASSIGNMENT_CROSS_ARM", _codes(diags))
            self.assertIn("SLOT_VIOLATION", _codes(diags))
            self.assertFalse(is_eligible(diags))

            rows2 = [r for r in ASSIGNMENTS if r[0] != "H01"] + [
                ("H01", "S2", "A", "false")
            ]
            _, diags2, _ = self._run(d, assignments=rows2)
            self.assertIn("ASSIGNMENT_STRATUM_MISMATCH", _codes(diags2))

    def test_slot_violation(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [
                r if r[0] != "H01" else ("H01", "S1", "B", "true")
                for r in ASSIGNMENTS
            ]
            _, diags, _ = self._run(d, assignments=rows)
            self.assertIn("SLOT_VIOLATION", _codes(diags))
            self.assertFalse(is_eligible(diags))

    def test_member_multi_household(self):
        with tempfile.TemporaryDirectory() as d:
            rows = MEMBERS + [("P01", "H02")]
            _, diags, metrics = self._run(d, members=rows)
            self.assertIn("MEMBER_MULTI_HOUSEHOLD", _codes(diags))
            # P01 的事件 E01 无法唯一归集，H01/H02 都标记
            self.assertIn("EVENT_AMBIGUOUS_PERSON", _codes(diags))
            self.assertEqual(metrics["H01"].metric_status, "ambiguous_attribution")
            self.assertEqual(metrics["H02"].metric_status, "ambiguous_attribution")
            self.assertIsNone(metrics["H01"].metric_cents)

    def test_unknown_person_event(self):
        with tempfile.TemporaryDirectory() as d:
            rows = EVENTS + [("E99", "PXX", "2026-11-02T00:00:00Z", 100)]
            _, diags, _ = self._run(d, events=rows)
            self.assertIn("EVENT_UNKNOWN_PERSON", _codes(diags))
            self.assertFalse(is_eligible(diags))

    def test_duplicate_event(self):
        with tempfile.TemporaryDirectory() as d:
            rows = EVENTS + [("E01", "P01", "2026-11-02T00:00:00Z", 100)]
            _, diags, metrics = self._run(d, events=rows)
            self.assertIn("EVENT_DUPLICATE_ID", _codes(diags))
            self.assertIn("E01", metrics["H01"].duplicate_event_ids)
            # 重复 event_id 的两条记录整体剔除：H01 只剩 E02=200
            self.assertEqual(metrics["H01"].metric_cents, 200)
            self.assertEqual(metrics["H01"].in_window_event_ids, ["E02"])

    def test_incomplete_coverage_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            cov = [c for c in COVERAGE if c[0] != "H03"] + [("H03", "false")]
            _, diags, metrics = self._run(d, coverage=cov)
            self.assertIn("COVERAGE_INCOMPLETE", _codes(diags))
            self.assertIsNone(metrics["H03"].metric_cents)


class QualityWarningTest(unittest.TestCase):
    def test_exposed_warning_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(Path(d))
            plan, diags, _ = _load_and_validate(paths)
            self.assertTrue(is_eligible(diags))
            # H04 处理组未领券 -> warning
            self.assertTrue(
                any(x.code == "EXPOSED_INCONSISTENT" for x in diags)
            )

    def test_non_integer_amount_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [r for r in EVENTS if r[0] != "E01"] + [
                ("E01", "P01", "2026-11-02T00:00:00Z", "12.5")
            ]
            paths = write_fixture(Path(d), events=rows)
            plan, diags, _ = _load_and_validate(paths)
            self.assertIn("EVENT_NON_INTEGER_AMOUNT", _codes(diags))

    def test_unknown_household_rows_are_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(
                Path(d), coverage=COVERAGE + [("H99", "true")]
            )
            _, diags, _ = _load_and_validate(paths)
            warn_codes = {x.code for x in diags}
            self.assertIn("COVERAGE_UNKNOWN_HOUSEHOLD", warn_codes)
            self.assertTrue(is_eligible(diags))


class PipelineBlockingTest(unittest.TestCase):
    def test_blocked_run_keeps_descriptive_only(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_fixture(
                Path(d),
                coverage=[c for c in COVERAGE if c[0] != "H03"]
                + [("H03", "false")],
            )
            result = run_analysis(
                paths["plan"], paths["assignments"], paths["members"],
                paths["events"], paths["coverage"],
            )
            self.assertFalse(result["eligible_for_causal_inference"])
            self.assertIsNone(result["inference"])
            self.assertIn("inference_blocked_reason", result)
            self.assertTrue(
                result["descriptive_statistics"]["label"].startswith("DESCRIPTIVE")
            )


if __name__ == "__main__":
    unittest.main()
