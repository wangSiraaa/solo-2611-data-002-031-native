"""Combine samples from one calibration with shared-error accounting.

A combination is a linear functional of the true quantities:
    L = sum_s a_s * c_s
Each Monte Carlo replicate evaluates L on the SAME theta/delta draws used for
every sample, so the certificate offset enters every sample identically and
the parameter draw enters through the same curve. The resulting interval is
therefore not the naive sqrt(sum a_s^2 var_s) that would wrongly treat samples
as independent.

Variance decomposition per replicate:
    L_k = theta/reading part + (sum_s a_s) * delta_k
The shared contribution is (sum a_s)^2 * sigma_shared^2 in the additive model;
it vanishes when the coefficients sum to zero (a difference) and persists /
adds when they do not (e.g. a mean). We report both the total empirical SD and
the shared contribution estimated from the draws, plus the coefficient sum.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .apply import ApplyResult
from .issues import Issue

INTERVAL_LEVEL = 0.95


@dataclass
class CombinationResult:
    combo_id: str
    terms: dict[str, float]
    status: str
    reason: str
    estimate: Optional[float]
    interval: Optional[list[float]]
    interval_level: float
    coefficient_sum: float
    draws_valid: int
    total_sd: Optional[float]
    shared_offset_sd: Optional[float]
    shared_contribution_sd: Optional[float]
    independent_sd: Optional[float]
    naive_independent_sd: Optional[float]
    unit: str
    per_sample_estimates: dict[str, Optional[float]]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "combination_id": self.combo_id,
            "terms": self.terms,
            "status": self.status,
            "reason": self.reason,
            "estimate": self.estimate,
            "interval": self.interval,
            "interval_level": self.interval_level,
            "coefficient_sum": self.coefficient_sum,
            "draws_valid": self.draws_valid,
            "total_sd": self.total_sd,
            "components": {
                "shared_certificate_contribution_sd": self.shared_contribution_sd,
                "independent_or_parameter_sd": self.independent_sd,
                "naive_independent_sd_if_samples_treated_independent":
                    self.naive_independent_sd,
            },
            "unit": self.unit,
            "per_sample_estimates": self.per_sample_estimates,
            "notes": self.notes,
            "shared_error_explanation": self._explanation(),
        }

    def _explanation(self) -> str:
        s = self.coefficient_sum
        if abs(s) <= 1e-12:
            return ("coefficient sum is 0, so the common certificate offset cancels "
                    "exactly in the linear combination; the interval reflects only "
                    "parameter and reading noise (plus any parameter-draw correlation)")
        if abs(s - 1.0) <= 1e-12:
            return ("coefficient sum is 1, so the full common certificate offset is "
                    "retained in the combination; the shared component does not "
                    "cancel")
        return (f"coefficient sum is {s:g}, so the common certificate offset enters "
                f"with weight {s:g}; the shared component is {abs(s):g} times the "
                "single-sample shared SD and may add to or partly offset the "
                "independent part depending on other correlations")


@dataclass
class CompareResult:
    combinations: list[CombinationResult]
    draws: int
    seed: int
    interval_level: float
    shared_error_id: str
    covariance: Optional[list[list[float]]]
    covariance_ids: list[str]
    assumptions: list[str]
    issues: list[Issue]

    def to_dict(self) -> dict:
        return {
            "schema": "calibration-compare/1",
            "draws": self.draws,
            "seed": self.seed,
            "interval_level": self.interval_level,
            "shared_error_id": self.shared_error_id,
            "assumptions": self.assumptions,
            "sample_estimate_covariance": self.covariance,
            "covariance_sample_ids": self.covariance_ids,
            "combinations": [c.to_dict() for c in self.combinations],
            "issues": [i.to_dict() for i in self.issues],
        }


def compare_model(model, apply_result: ApplyResult, combinations) -> CompareResult:
    issues: list[Issue] = []
    draws = apply_result.draws
    seed = apply_result.seed
    shared_id = getattr(apply_result, "shared_error_id", None) or \
        model.to_dict().get("shared_error_id")
    unit = model.reference_unit
    order = getattr(apply_result, "sample_order", [s.sample_id for s in apply_result.samples])
    table = apply_result.draws_table
    sample_est = apply_result.by_id

    assumptions = [
        "all samples share one calibration fit and one certificate offset, so "
        "their estimates are correlated; combinations are evaluated replicate by "
        "replicate on the common draws rather than by combining marginal SDs",
        f"each sample's reading noise is independent; parameters and certificate "
        f"offset are common. Draws={draws}, seed={seed}",
        "a combination is a linear functional of the true quantities; its "
        "interval uses the same central 95% quantiles as single samples",
        "samples outside the calibration support or non-invertible are omitted "
        "from any combination that needs them, and the combination is flagged",
    ]

    combos = combinations if isinstance(combinations, list) else []
    combo_results: list[CombinationResult] = []
    required: list[str] = []

    # Estimate covariance across samples from the common draws (shared sources).
    # Samples that are not invertible have no x-draws and are excluded; they are
    # already flagged individually and cannot appear in an ok combination.
    cov = None
    cov_ids: list[str] = []
    if table is not None and table.shape[1] > 0:
        ok_cols = [j for j in range(len(order))
                   if sample_est[order[j]].status == "ok"]
        if len(ok_cols) >= 2:
            sub_cov = table[:, ok_cols]
            complete = np.all(np.isfinite(sub_cov), axis=1)
            if int(complete.sum()) > 2:
                cov = np.atleast_2d(np.cov(sub_cov[complete].T, ddof=1)).tolist()
                cov_ids = [order[j] for j in ok_cols]
            else:
                issues.append(Issue(
                    "warning", "too_few_common_draws", "compare",
                    "too few shared replicates contain all invertible samples to "
                    "estimate the cross-sample covariance"))
        else:
            issues.append(Issue(
                "warning", "too_few_invertible_samples", "compare",
                "fewer than two invertible samples; covariance not estimable"))

    for combo in combos:
        cid = combo.get("id", "?")
        terms = combo.get("terms", {})
        missing = [s for s in terms if s not in sample_est]
        noninv = [s for s in terms
                  if s in sample_est and sample_est[s].status != "ok"]
        per_est = {s: (sample_est[s].estimate if s in sample_est else None)
                   for s in terms}
        coef_sum = float(sum(terms.values()))
        if missing or noninv:
            bad = missing + noninv
            reasons = []
            for s in bad:
                if s not in sample_est:
                    reasons.append(f"{s}: not present in sample results")
                else:
                    reasons.append(f"{s}: {sample_est[s].status} ({sample_est[s].reason})")
            issues.append(Issue(
                "error", "combination_not_evaluable", f"combination {cid}",
                "cannot evaluate because " + "; ".join(reasons)))
            combo_results.append(CombinationResult(
                combo_id=cid, terms=terms, status="not_evaluable",
                reason="; ".join(reasons), estimate=None, interval=None,
                interval_level=INTERVAL_LEVEL, coefficient_sum=coef_sum,
                draws_valid=0, total_sd=None, shared_offset_sd=None,
                shared_contribution_sd=None, independent_sd=None,
                naive_independent_sd=None, unit=unit,
                per_sample_estimates=per_est))
            for s in terms:
                if s not in required:
                    required.append(s)
            continue

        required.extend(s for s in terms if s not in required)
        idx = [order.index(s) for s in terms]
        coefs = np.array([terms[s] for s in terms.keys()], dtype=float)
        sub = table[:, idx]
        complete = np.all(np.isfinite(sub), axis=1)
        n_valid = int(complete.sum())

        # Point estimate is the linear combination of point inversions.
        point = float(sum(terms[s] * sample_est[s].estimate for s in terms))

        if n_valid < 2:
            issues.append(Issue(
                "error", "combination_no_draws", f"combination {cid}",
                "fewer than 2 replicates have all required samples invertible"))
            combo_results.append(CombinationResult(
                combo_id=cid, terms=terms, status="not_evaluable",
                reason="fewer than 2 shared replicates contain all samples",
                estimate=point, interval=None, interval_level=INTERVAL_LEVEL,
                coefficient_sum=coef_sum, draws_valid=n_valid, total_sd=None,
                shared_offset_sd=None, shared_contribution_sd=None,
                independent_sd=None, naive_independent_sd=None, unit=unit,
                per_sample_estimates=per_est))
            continue

        L = sub[complete] @ coefs
        alpha = (1.0 - INTERVAL_LEVEL) / 2.0
        lo_q, hi_q = np.quantile(L, [alpha, 1.0 - alpha])
        total_sd = float(L.std(ddof=1))

        # Shared contribution from the additive offset.
        sigma_shared = model.shared_offset_sd
        shared_contrib = abs(coef_sum) * sigma_shared
        # Observable shared contribution in this replicate set: correlation
        # induced part. We isolate it as var(L) - var(L - coef_sum*delta).
        # delta draws are not stored directly, so use the analytic additive value
        # and report the empirical total.
        independent_sd = math.sqrt(max(total_sd ** 2 - shared_contrib ** 2, 0.0))

        # Naive "independent samples" SD: combine each sample's own uncertainty_sd.
        naive_sd = math.sqrt(sum(
            (terms[s] ** 2) * (sample_est[s].uncertainty_sd or 0.0) ** 2 for s in terms))

        notes = []
        if abs(coef_sum) <= 1e-12:
            notes.append("shared certificate offset cancels: coefficient sum is zero")
        else:
            notes.append(f"shared certificate offset retained with weight {coef_sum:g}")
        if abs(naive_sd - total_sd) > 1e-9:
            notes.append(
                f"using the common-draw SD {total_sd:.6g} differs from the naive "
                f"independent combination {naive_sd:.6g}; the difference is the "
                "shared calibration/offset correlation")

        combo_results.append(CombinationResult(
            combo_id=cid, terms=terms, status="ok",
            reason="evaluated on common calibration draws", estimate=point,
            interval=[float(lo_q), float(hi_q)], interval_level=INTERVAL_LEVEL,
            coefficient_sum=coef_sum, draws_valid=n_valid, total_sd=total_sd,
            shared_offset_sd=sigma_shared, shared_contribution_sd=shared_contrib,
            independent_sd=independent_sd, naive_independent_sd=naive_sd,
            unit=unit, per_sample_estimates=per_est, notes=notes))

    result = CompareResult(combo_results, draws, seed, INTERVAL_LEVEL,
                           shared_id, cov, cov_ids, assumptions, issues)
    return result
