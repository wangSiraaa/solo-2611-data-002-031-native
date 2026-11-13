"""Tests: hand checks, edge cases, shift invariance, shared-error propagation.

Run with:  .venv/bin/python -m pytest -q  (or the stdlib runner below)
The independent hand checks are asserted numerically, not by "fit succeeded".
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import tempfile

import numpy as np
import pytest

from calibration_tool.apply import apply_model
from calibration_tool.compare import compare_model
from calibration_tool.fit import fit_model
from calibration_tool.validation import load_inputs

import synth


def run(tmp, cal, sam, plan):
    bundle = load_inputs(cal, sam, plan)
    assert bundle.ok, [i.to_dict() for i in bundle.issues]
    fit, model = fit_model(bundle)
    app = apply_model(model, bundle.sample_rows, draws=2000)
    comp = compare_model(model, app, bundle.plan.get("combinations", []))
    return fit, model, app, comp


# ---------------------------------------------------------------------------
# Ideal noiseless cases: compare against independent hand calculation.
# ---------------------------------------------------------------------------
def test_ideal_linear_hand_inverse():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)
        truths = {"A": 2.5, "B": 7.5, "C": 12.0}
        synth.linear_samples(sam, truths)
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1,), working_range=(0, 15))
        fit, model, app, comp = run(tmp, cal, sam, plan)

        assert fit.ok and fit.degree == 1
        # Hand fit of y=3+2x: coefficients exactly [3, 2].
        assert fit.coefficients == pytest.approx([3.0, 2.0], abs=1e-9)
        # Hand inverse x = (y-3)/2.
        for s in app.samples:
            x_hand = (s.response_mean - 3.0) / 2.0
            assert s.estimate == pytest.approx(x_hand, abs=1e-7)
            assert s.estimate == pytest.approx(truths[s.sample_id], abs=1e-7)
            # Noiseless + zero shared offset => interval collapses to the point.
            assert s.interval[1] - s.interval[0] < 1e-6


def test_ideal_curvature_branch_inverse():
    """y = 10 + 5x + 0.5x^2, strictly increasing on [0,20]; invert on branch."""
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.curved_calibration(cal)
        truths = {"A": 4.0, "B": 11.0}
        synth.curved_samples(sam, truths)
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1, 2), working_range=(0, 20))
        fit, model, app, comp = run(tmp, cal, sam, plan)

        assert fit.ok and fit.degree == 2
        assert fit.coefficients == pytest.approx([10.0, 5.0, 0.5], abs=1e-8)
        for s in app.samples:
            c = truths[s.sample_id]
            # Independent hand inverse by the quadratic formula on the increasing
            # branch: x = (-5 + sqrt(25 - 4*0.5*(10 - y)))/(2*0.5).
            y = 10 + 5 * c + 0.5 * c * c
            x_hand = (-5 + math.sqrt(25 - 2 * (10 - y))) / 1.0
            assert s.estimate == pytest.approx(x_hand, abs=1e-6)
            assert s.estimate == pytest.approx(c, abs=1e-6)


def test_out_of_range_not_extrapolated():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)          # support [0,15]
        synth.linear_samples(sam, {"A": 2.0, "FAR": 40.0})  # 40 is outside
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1,), working_range=(0, 15))
        fit, model, app, comp = run(tmp, cal, sam, plan)
        far = app.by_id["FAR"]
        assert far.status == "out_of_range"
        assert far.estimate is None and far.interval is None
        assert "no extrapolation" in far.reason
        assert app.by_id["A"].status == "ok"


def test_nonmonotone_refuses_inversion():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.nonmonotone_calibration(cal)
        synth.linear_samples(sam, {"A": 5.0})
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1, 2), working_range=(0, 30))
        bundle = load_inputs(cal, sam, plan)
        fit, model = fit_model(bundle)
        codes = {i.code for i in fit.issues}
        assert not fit.ok
        assert "non_monotone" in codes
        app = apply_model(model, bundle.sample_rows, draws=500)
        assert all(s.status == "not_invertible" for s in app.samples)


def test_flat_curve_near_zero_slope():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.flat_calibration(cal)
        synth.linear_samples(sam, {"A": 5.0})
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1,), working_range=(0, 15))
        bundle = load_inputs(cal, sam, plan)
        fit, model = fit_model(bundle)
        assert not fit.ok
        assert "near_zero_slope" in {i.code for i in fit.issues}


def test_insufficient_levels_rejects_degree():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.too_few_levels_calibration(cal)   # 3 levels
        synth.linear_samples(sam, {"A": 5.0})
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1, 2), working_range=(0, 10))
        bundle = load_inputs(cal, sam, plan)
        fit, model = fit_model(bundle)
        cand = {c.degree: c for c in fit.candidates}
        assert cand[2].eligible is False
        assert "levels" in cand[2].reason
        assert cand[1].eligible is True
        assert fit.degree == 1 and fit.ok


# ---------------------------------------------------------------------------
# Common offset: shared, additive; difference cancels, mean retains.
# ---------------------------------------------------------------------------
def test_shared_offset_difference_cancels_mean_retains():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)
        synth.linear_samples(sam, {"S1": 4.0, "S2": 9.0})
        combos = [
            {"id": "S2-S1", "terms": {"S2": 1, "S1": -1}},
            {"id": "mean", "terms": {"S1": 0.5, "S2": 0.5}},
        ]
        synth.linear_plan(plan, shared_sd=0.3, seed=99, degrees=(1,),
                          combinations=combos, working_range=(0, 15))
        fit, model, app, comp = run(tmp, cal, sam, plan)
        c = {x.combo_id: x for x in comp.combinations}
        assert c["S2-S1"].status == "ok" and c["mean"].status == "ok"
        # Difference: shared offset cancels -> small interval (~ reading/params).
        assert c["S2-S1"].coefficient_sum == 0
        assert c["S2-S1"].shared_contribution_sd == 0
        # Mean: coefficient sum 1 -> shared contribution equals 0.3.
        assert c["mean"].coefficient_sum == 1
        assert c["mean"].shared_contribution_sd == pytest.approx(0.3, abs=1e-12)
        # Points: noiseless readings, so difference is exact.
        assert c["S2-S1"].estimate == pytest.approx(5.0, abs=1e-6)
        assert c["mean"].estimate == pytest.approx(6.5, abs=1e-6)
        # Shared offset makes the mean wider than the naive independent SD implies.
        assert c["mean"].total_sd > c["S2-S1"].total_sd


# ---------------------------------------------------------------------------
# Independent recomputation of a sample difference by raw sampling.
# ---------------------------------------------------------------------------
def test_independent_resampling_of_difference():
    """Rebuild the S2-S1 interval from scratch with a separate RNG stream and the
    published model, then compare against compare_model's interval."""
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)
        synth.linear_samples(sam, {"S1": 4.0, "S2": 9.0})
        combos = [{"id": "S2-S1", "terms": {"S2": 1, "S1": -1}}]
        synth.linear_plan(plan, shared_sd=0.3, seed=7, degrees=(1,),
                          combinations=combos, working_range=(0, 15))
        fit, model, app, comp = run(tmp, cal, sam, plan)
        got = comp.combinations[0]

        # Independent recomputation using the published coefficients/covariance.
        p = model.to_dict()
        coef = np.array(p["model"]["coefficients_ascending"])
        cov = np.array(p["model"]["coefficient_covariance"])
        sigma = p["reference_shared_offset_sd"]
        n = 200000
        rng = np.random.default_rng(2024)   # different seed on purpose
        th = rng.multivariate_normal(coef, cov, size=n)
        delta = rng.normal(0, sigma, size=n)
        # Sample means are noiseless (two identical repeats); map response -> x on
        # the increasing line: x = (y - p0)/p1.
        y1 = 3 + 2 * 4.0
        y2 = 3 + 2 * 9.0
        x1 = (y1 - th[:, 0]) / th[:, 1] + delta
        x2 = (y2 - th[:, 0]) / th[:, 1] + delta
        diff = x2 - x1
        lo, hi = np.quantile(diff, [0.025, 0.975])
        # Delta cancels in the difference exactly, so SD is tiny and intervals
        # should be close to the tool's, within Monte-Carlo tolerance.
        assert got.estimate == pytest.approx(diff.mean(), abs=5e-3)
        assert got.interval[0] == pytest.approx(lo, abs=2e-2)
        assert got.interval[1] == pytest.approx(hi, abs=2e-2)
        # Cert offset cancels: empirical SD of difference << 0.3.
        assert diff.std() < 0.02


# ---------------------------------------------------------------------------
# Validation: illegal units, missing values, duplicate repeats.
# ---------------------------------------------------------------------------
def test_illegal_units_reported():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)
        synth.linear_samples(sam, {"A": 2.0})
        synth.linear_plan(plan, shared_sd=0.0, reference_unit="furlongs",
                          response_unit="mV", degrees=(1,))
        bundle = load_inputs(cal, sam, plan)
        assert not bundle.ok
        assert any(i.code == "unit_illegal" and "reference_unit" in i.item
                   for i in bundle.issues)


def test_missing_value_itemised():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        with open(cal, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["level_id", "reference_value", "measurement_batch", "response"])
            w.writerow(["L0", 0, "B1", ""])            # missing response
            w.writerow(["L1", 5, "B1", "13"])
            w.writerow(["L2", "", "B1", "23"])         # missing reference
        synth.linear_samples(sam, {"A": 2.0})
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1,))
        bundle = load_inputs(cal, sam, plan)
        assert not bundle.ok
        miss = [i for i in bundle.issues if i.code == "missing_value"]
        assert len(miss) == 2
        assert {i.line for i in miss} == {2, 4}


def test_duplicate_repeat_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.linear_calibration(cal)
        with open(sam, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["sample_id", "repeat_id", "response"])
            w.writerow(["A", 1, 7.0])
            w.writerow(["A", 1, 7.0])
        synth.linear_plan(plan, shared_sd=0.0, degrees=(1,))
        bundle = load_inputs(cal, sam, plan)
        assert not bundle.ok
        assert any(i.code == "duplicate_repeat" for i in bundle.issues)


# ---------------------------------------------------------------------------
# Row-order invariance.
# ---------------------------------------------------------------------------
def test_row_order_invariance():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.curved_calibration(cal)
        synth.curved_samples(sam, {"A": 4.0, "B": 11.0, "C": 2.0})
        combos = [
            {"id": "B-A", "terms": {"B": 1, "A": -1}},
            {"id": "mean", "terms": {"A": 0.5, "B": 0.5}},
        ]
        synth.linear_plan(plan, shared_sd=0.2, seed=5, degrees=(1, 2),
                          combinations=combos, working_range=(0, 20))
        fit1, model1, app1, comp1 = run(tmp, cal, sam, plan)
        est1 = {s.sample_id: s.estimate for s in app1.samples}
        comb1 = {c.combo_id: c for c in comp1.combinations}

        # Shuffle both files.
        for path in (cal, sam):
            with open(path, newline="") as fh:
                rows = list(csv.reader(fh))
            header, body = rows[0], rows[1:]
            random.Random(42).shuffle(body)
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(body)
        fit2, model2, app2, comp2 = run(tmp, cal, sam, plan)
        est2 = {s.sample_id: s.estimate for s in app2.samples}
        comb2 = {c.combo_id: c for c in comp2.combinations}

        assert fit2.degree == fit1.degree
        assert fit2.coefficients == pytest.approx(fit1.coefficients, abs=1e-12)
        for sid in est1:
            assert est2[sid] == pytest.approx(est1[sid], abs=1e-9)
            s1 = next(s for s in app1.samples if s.sample_id == sid)
            s2 = next(s for s in app2.samples if s.sample_id == sid)
            assert s2.interval == pytest.approx(s1.interval, abs=1e-9)
        for cid in comb1:
            assert comb2[cid].estimate == pytest.approx(comb1[cid].estimate, abs=1e-9)
            assert comb2[cid].interval == pytest.approx(comb1[cid].interval, abs=1e-9)


def test_reproducible_seed():
    with tempfile.TemporaryDirectory() as tmp:
        cal = os.path.join(tmp, "cal.csv")
        sam = os.path.join(tmp, "sam.csv")
        plan = os.path.join(tmp, "plan.json")
        synth.curved_calibration(cal)
        synth.curved_samples(sam, {"A": 4.0, "B": 11.0})
        synth.linear_plan(plan, shared_sd=0.25, seed=11, degrees=(1, 2),
                          working_range=(0, 20))
        _, _, app1, _ = run(tmp, cal, sam, plan)
        _, _, app2, _ = run(tmp, cal, sam, plan)
        for s1, s2 in zip(app1.samples, app2.samples):
            assert s1.interval == s2.interval


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
