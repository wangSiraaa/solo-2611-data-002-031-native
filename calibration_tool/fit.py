"""Fit and select a calibration model.

Model: response = p_0 + p_1*x + (p_2*x^2), x = reference value in declared
reference units, response in declared response units. Reference levels carry
replicate readings; the fit pools readings, and model selection uses
leave-one-*level*-out cross-validation so repeats of a level are never split
between train and test.

Selection rule (published, deterministic):
  1. Consider each candidate degree d in plan.candidate_degrees.
  2. A degree is eligible only if the number of distinct reference levels is
     at least d+2 (d+1 to be estimable, one more so grouped CV has >0 residual
     degrees of freedom).
  3. For each eligible degree, compute leave-one-level-out CV: fit on the other
     levels, predict every reading of the held-out level, and sum squared errors.
  4. Pick the degree with the lowest pooled CV mean squared error.
     If two degrees are within CV_TIE_REL of each other, prefer the lower
     (simpler) degree.
  5. Inversion is refused if the selected curve is not strictly monotone over
     the supported reference interval, or if its slope is numerically
     indistinguishable from zero (see issues emitted with the fit).

No random resampling is used to *fit*; the reported coefficient covariance is
the usual OLS covariance. All randomness (uncertainty intervals) happens later
in apply/compare with plan.seed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .issues import Issue
from .validation import CalRow

CV_TIE_REL = 1e-9          # relative CV-MSE tie -> prefer simpler model
SLOPE_REL_TOL = 1e-9       # |slope| <= tol * max(1, max|slope|) => flat
SIGNAL_REL_TOL = 1e-9      # response span <= tol * response scale => flat


@dataclass
class LevelSummary:
    level_id: str
    reference_value: float
    n: int
    batches: list[str]
    response_mean: float
    response_sd: float
    batch_means: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "level_id": self.level_id,
            "reference_value": self.reference_value,
            "n_readings": self.n,
            "batches": self.batches,
            "response_mean": self.response_mean,
            "response_sd": self.response_sd,
            "batch_means": self.batch_means,
        }


@dataclass
class Candidate:
    degree: int
    eligible: bool
    reason: str
    n_levels: int
    cv_mse: Optional[float]
    cv_rmse: Optional[float]
    cv_sse: Optional[float]
    fold_rmse: list[float] = field(default_factory=list)
    coefficients: Optional[list[float]] = None
    residual_sd: Optional[float] = None
    residual_dof: Optional[int] = None
    rss: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "degree": self.degree,
            "eligible": self.eligible,
            "reason": self.reason,
            "n_levels": self.n_levels,
            "cv_mse": self.cv_mse,
            "cv_rmse": self.cv_rmse,
            "cv_sse": self.cv_sse,
            "fold_rmse": self.fold_rmse,
            "coefficients_ascending": self.coefficients,
            "residual_sd": self.residual_sd,
            "residual_dof": self.residual_dof,
            "rss": self.rss,
        }


@dataclass
class FitResult:
    ok: bool
    degree: Optional[int]
    coefficients: list[float]           # ascending powers [p0, p1, ...]
    coef_cov: Optional[np.ndarray]
    residual_sd: Optional[float]
    residual_dof: Optional[int]
    support_low: Optional[float]
    support_high: Optional[float]
    working_range: list[float]
    monotone: bool
    monotone_direction: Optional[str]   # "increasing"/"decreasing"
    min_abs_derivative: Optional[float]
    levels: list[LevelSummary]
    candidates: list[Candidate]
    selected: Optional[Candidate]
    cal_rows: list[CalRow]
    reference_unit: str
    response_unit: str
    shared_error_id: str
    reference_shared_offset_sd: float
    seed: int
    n_batches: int
    batch_diagnostic: dict
    issues: list[Issue]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema": "calibration-model/1",
            "ok": self.ok,
            "reference_unit": self.reference_unit,
            "response_unit": self.response_unit,
            "shared_error_id": self.shared_error_id,
            "reference_shared_offset_sd": self.reference_shared_offset_sd,
            "seed": self.seed,
            "model": None if self.degree is None else {
                "degree": self.degree,
                "basis": "polynomial ascending powers of reference_value",
                "coefficients_ascending": self.coefficients,
                "coefficient_covariance": (
                    None if self.coef_cov is None else self.coef_cov.tolist()),
                "residual_sd": self.residual_sd,
                "residual_dof": self.residual_dof,
            },
            "support": {
                "working_range": self.working_range,
                "calibration_support_low": self.support_low,
                "calibration_support_high": self.support_high,
            },
            "invertibility": {
                "monotone": self.monotone,
                "direction": self.monotone_direction,
                "min_abs_derivative": self.min_abs_derivative,
            },
            "selection": {
                "rule": ("grouped leave-one-reference-level-out CV; lowest pooled "
                         "CV MSE; CV_TIE_REL tie-break to lower degree"),
                "cv_tie_rel": CV_TIE_REL,
                "selected_degree": self.degree,
                "candidates": [c.to_dict() for c in self.candidates],
                "n_batches": self.n_batches,
                "batch_diagnostic": self.batch_diagnostic,
            },
            "levels": [l.to_dict() for l in self.levels],
            "issues": [i.to_dict() for i in self.issues],
            "notes": self.notes,
            "calibration_readings": [
                {"line": r.line, "level_id": r.level_id,
                 "reference_value": r.reference_value,
                 "measurement_batch": r.measurement_batch,
                 "response": r.response}
                for r in self.cal_rows
            ],
        }


def _design(rows: list[CalRow], degree: int) -> tuple[np.ndarray, np.ndarray]:
    x = np.array([r.reference_value for r in rows], dtype=float)
    y = np.array([r.response for r in rows], dtype=float)
    cols = [np.ones_like(x)]
    for d in range(1, degree + 1):
        cols.append(x ** d)
    return np.column_stack(cols), y


def _ols(rows: list[CalRow], degree: int):
    X, y = _design(rows, degree)
    coef, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    rss = float(resid @ resid)
    dof = X.shape[0] - X.shape[1]
    if rank < X.shape[1]:
        return None, None, None, None, f"design matrix rank {rank} < {X.shape[1]}"
    sigma2 = rss / dof if dof > 0 else 0.0
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return None, None, None, None, "singular normal matrix"
    return coef, cov, sigma2, dof, None


def _summarise_levels(rows: list[CalRow]) -> list[LevelSummary]:
    by_level: dict[str, list[CalRow]] = {}
    for r in rows:
        by_level.setdefault(r.level_id, []).append(r)
    out = []
    for level_id in sorted(by_level, key=lambda k: min(r.reference_value for r in by_level[k])):
        rs = by_level[level_id]
        vals = np.array([r.response for r in rs], dtype=float)
        batch_means: dict[str, float] = {}
        for b in sorted({r.measurement_batch for r in rs}):
            bv = [r.response for r in rs if r.measurement_batch == b]
            batch_means[b] = float(np.mean(bv))
        out.append(LevelSummary(
            level_id=level_id,
            reference_value=float(rs[0].reference_value),
            n=len(rs),
            batches=sorted({r.measurement_batch for r in rs}),
            response_mean=float(vals.mean()),
            response_sd=float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
            batch_means=batch_means,
        ))
    return out


def _batch_diagnostic(rows: list[CalRow], levels: list[LevelSummary]) -> dict:
    batches = sorted({r.measurement_batch for r in rows})
    diag = {
        "batches": batches,
        "n_batches": len(batches),
        "note": ("batch labels are used only as a diagnostic; samples.csv carries no "
                 "batch label, so a batch offset cannot be applied when inverting "
                 "samples. Fitting is pooled over readings; with a balanced design "
                 "(each level present in each batch) a constant batch offset cancels "
                 "from the level means used by the polynomial fit."),
    }
    if len(batches) >= 2:
        # For each pair, mean of per-level batch differences (paired by level).
        pair_stats = {}
        for i in range(len(batches)):
            for j in range(i + 1, len(batches)):
                b1, b2 = batches[i], batches[j]
                diffs = []
                for lv in levels:
                    if b1 in lv.batch_means and b2 in lv.batch_means:
                        diffs.append(lv.batch_means[b1] - lv.batch_means[b2])
                if diffs:
                    d = np.array(diffs)
                    pair_stats[f"{b1}-{b2}"] = {
                        "paired_level_mean_diff": float(d.mean()),
                        "paired_level_diff_sd": float(d.std(ddof=1)) if len(d) > 1 else 0.0,
                        "n_paired_levels": len(d),
                    }
        diag["pairwise"] = pair_stats
    return diag


def _cv(rows: list[CalRow], degree: int, levels: list[LevelSummary]) -> dict:
    errors: list[float] = []
    fold_rmse: list[float] = []
    by_level: dict[str, list[CalRow]] = {}
    for r in rows:
        by_level.setdefault(r.level_id, []).append(r)
    for lv in levels:
        train = [r for r in rows if r.level_id != lv.level_id]
        test = by_level[lv.level_id]
        coef, _, _, _, err = _ols(train, degree)
        if err is not None:
            return {"ok": False, "reason": f"fold {lv.level_id}: {err}"}
        Xt, yt = _design(test, degree)
        se = (yt - Xt @ coef) ** 2
        errors.extend(se.tolist())
        fold_rmse.append(float(math.sqrt(se.mean())))
    errs = np.array(errors, dtype=float)
    return {
        "ok": True,
        "cv_sse": float(errs.sum()),
        "cv_mse": float(errs.mean()),
        "cv_rmse": float(math.sqrt(errs.mean())),
        "fold_rmse": fold_rmse,
    }


def fit_model(bundle, draws: int = 0) -> tuple[FitResult, "CalibrationModel"]:
    """Fit and select. `draws` is accepted for API symmetry but unused here."""
    rows = bundle.cal_rows
    plan = bundle.plan
    issues = [i for i in bundle.issues if i.severity in ("error", "warning")]
    notes: list[str] = []

    ref_unit = plan.get("reference_unit", "")
    resp_unit = plan.get("response_unit", "")
    shared_id = plan.get("shared_error_id", "")
    offset_sd = float(plan.get("reference_shared_offset_sd", float("nan")))
    seed = int(plan.get("seed", 0))
    working = [float(v) for v in plan.get("working_range", [0.0, 0.0])]
    degrees = [d for d in plan.get("candidate_degrees", []) if isinstance(d, int)]

    levels = _summarise_levels(rows)
    n_levels = len(levels)
    batch_diag = _batch_diagnostic(rows, levels)
    n_batches = len(batch_diag["batches"])

    if n_levels == 0:
        issues.append(Issue("error", "no_calibration", "calibration.csv",
                            "no usable calibration readings; cannot fit"))

    candidates: list[Candidate] = []
    for d in degrees:
        need = d + 2
        if n_levels < need:
            candidates.append(Candidate(
                degree=d, eligible=False,
                reason=(f"only {n_levels} distinct reference levels; degree {d} needs "
                        f">= {need} levels for grouped CV (d+1 estimable, one extra "
                        f"for held-out residual)"),
                n_levels=n_levels, cv_mse=None, cv_rmse=None, cv_sse=None))
            continue
        coef, cov, sigma2, dof, err = _ols(rows, d)
        if err is not None:
            candidates.append(Candidate(
                degree=d, eligible=False, reason=err, n_levels=n_levels,
                cv_mse=None, cv_rmse=None, cv_sse=None))
            continue
        cv = _cv(rows, d, levels)
        if not cv.get("ok", False):
            candidates.append(Candidate(
                degree=d, eligible=False, reason=cv.get("reason", "CV failed"),
                n_levels=n_levels, cv_mse=None, cv_rmse=None, cv_sse=None))
            continue
        # All levels have readings; RSS from the full fit for the coefficient table.
        X, y = _design(rows, d)
        rss = float(np.sum((y - X @ coef) ** 2))
        candidates.append(Candidate(
            degree=d, eligible=True,
            reason=("eligible: grouped leave-one-level-out CV over "
                    f"{n_levels} levels"),
            n_levels=n_levels,
            cv_mse=cv["cv_mse"], cv_rmse=cv["cv_rmse"], cv_sse=cv["cv_sse"],
            fold_rmse=cv["fold_rmse"],
            coefficients=[float(c) for c in coef],
            residual_sd=float(math.sqrt(sigma2)), residual_dof=int(dof), rss=rss))

    eligible = [c for c in candidates if c.eligible]
    selected: Optional[Candidate] = None
    if eligible:
        best = min(c.cv_mse for c in eligible)
        # Tie-break to the simpler (lower) degree when within CV_TIE_REL.
        near = [c for c in eligible
                if abs(c.cv_mse - best) <= CV_TIE_REL * max(1.0, abs(best))]
        selected = sorted(near, key=lambda c: (c.degree, c.cv_mse))[0]
        notes.append(
            f"selected degree {selected.degree} by lowest grouped-CV MSE "
            f"({selected.cv_mse:.6g}); eligible degrees: "
            f"{[(c.degree, round(c.cv_mse, 6)) for c in eligible]}")
    else:
        reasons = "; ".join(f"d={c.degree}: {c.reason}" for c in candidates) or \
            "no candidate degrees declared"
        issues.append(Issue("error", "no_eligible_model", "calibration",
                            f"no calibration degree is eligible ({reasons})"))

    # Defaults so the result object is always well-formed.
    ok = False
    degree = None
    coefficients: list[float] = []
    coef_cov: Optional[np.ndarray] = None
    residual_sd = None
    residual_dof = None
    support_low = support_high = None
    monotone = False
    direction = None
    min_abs_deriv = None

    if selected is not None and n_levels > 0:
        degree = selected.degree
        coefficients = selected.coefficients
        residual_sd = selected.residual_sd
        residual_dof = selected.residual_dof
        coef, cov, sigma2, dof, _ = _ols(rows, degree)
        coef_cov = cov

        cal_lo = min(l.reference_value for l in levels)
        cal_hi = max(l.reference_value for l in levels)
        support_low = max(working[0], cal_lo)
        support_high = min(working[1], cal_hi)
        outside_lo = cal_lo > working[1] or cal_hi < working[0]
        if support_low >= support_high or outside_lo:
            issues.append(Issue(
                "error", "empty_support", "calibration",
                f"calibration reference span [{cal_lo}, {cal_hi}] and working_range "
                f"{working} do not overlap with positive width; cannot define an "
                "invertible support"))
        else:
            if cal_lo > working[0]:
                notes.append(
                    f"calibration starts at {cal_lo}, above working low {working[0]}; "
                    "support is clamped to the observed calibration span")
            if cal_hi < working[1]:
                notes.append(
                    f"calibration ends at {cal_hi}, below working high {working[1]}; "
                    "support is clamped to the observed calibration span")

            def deriv(x):
                return coefficients[1] + 2.0 * coefficients[2] * x if degree == 2 \
                    else coefficients[1]

            d_lo = float(deriv(support_low))
            d_hi = float(deriv(support_high))
            # Quadratic derivative is linear, so extrema are at endpoints.
            endpoint_derivs = [d_lo, d_hi]
            min_abs_deriv = min(abs(v) for v in endpoint_derivs)
            max_abs_deriv = max(abs(v) for v in endpoint_derivs) or 1.0
            f_lo = float(np.polyval(coefficients[::-1], support_low))
            f_hi = float(np.polyval(coefficients[::-1], support_high))
            signal = abs(f_hi - f_lo)
            resp_scale = max(1.0, abs(f_lo), abs(f_hi))

            same_sign = (d_lo > 0 and d_hi > 0) or (d_lo < 0 and d_hi < 0)
            flat = (min_abs_deriv <= SLOPE_REL_TOL * max(1.0, max_abs_deriv)
                    or signal <= SIGNAL_REL_TOL * resp_scale)

            if not same_sign:
                issues.append(Issue(
                    "error", "non_monotone",
                    "calibration",
                    f"fitted degree-{degree} curve is not strictly monotone on the "
                    f"supported interval [{support_low:.6g}, {support_high:.6g}] "
                    f"(endpoint slopes {d_lo:.6g}, {d_hi:.6g}); inversion would be "
                    "ambiguous, so it is refused"))
            elif flat:
                issues.append(Issue(
                    "error", "near_zero_slope",
                    "calibration",
                    f"fitted curve has no usable slope on the supported interval "
                    f"(minimum |df/dx|={min_abs_deriv:.6g}, response span "
                    f"{signal:.6g}); inversion is ill-conditioned and refused"))
            else:
                monotone = True
                direction = "increasing" if d_lo > 0 else "decreasing"
                ok = True
                notes.append(
                    f"curve is strictly {direction} on [{support_low:.6g}, "
                    f"{support_high:.6g}] with minimum |df/dx|={min_abs_deriv:.6g}")

            # Sanity: a quadratic tip inside the support that we somehow missed.
            if degree == 2 and coefficients[2] != 0:
                vertex = -coefficients[1] / (2.0 * coefficients[2])
                if support_low < vertex < support_high:
                    monotone = False
                    ok = False
                    issues.append(Issue(
                        "error", "non_monotone", "calibration",
                        f"quadratic vertex at x={vertex:.6g} lies inside support "
                        f"[{support_low:.6g}, {support_high:.6g}]; inversion refused"))

    result = FitResult(
        ok=ok, degree=degree, coefficients=coefficients, coef_cov=coef_cov,
        residual_sd=residual_sd, residual_dof=residual_dof,
        support_low=support_low, support_high=support_high,
        working_range=working, monotone=monotone, monotone_direction=direction,
        min_abs_derivative=min_abs_deriv, levels=levels, candidates=candidates,
        selected=selected, cal_rows=rows, reference_unit=ref_unit,
        response_unit=resp_unit, shared_error_id=shared_id,
        reference_shared_offset_sd=offset_sd, seed=seed, n_batches=n_batches,
        batch_diagnostic=batch_diag, issues=issues, notes=notes)

    model = CalibrationModel.from_fit(result)
    return result, model


class CalibrationModel:
    """Inspectable, JSON-serialisable calibration model."""

    def __init__(self, payload: dict):
        self.payload = payload

    @classmethod
    def from_fit(cls, fit: FitResult) -> "CalibrationModel":
        return cls(fit.to_dict())

    @classmethod
    def from_dict(cls, payload: dict) -> "CalibrationModel":
        if payload.get("schema") != "calibration-model/1":
            raise ValueError(f"unexpected model schema: {payload.get('schema')!r}")
        return cls(payload)

    def to_dict(self) -> dict:
        return self.payload

    # Convenience accessors used by apply/compare.
    @property
    def ok(self) -> bool:
        return bool(self.payload.get("ok"))

    @property
    def degree(self) -> Optional[int]:
        m = self.payload.get("model")
        return None if m is None else int(m["degree"])

    @property
    def coefficients(self) -> list[float]:
        m = self.payload.get("model")
        return [] if m is None else [float(c) for c in m["coefficients_ascending"]]

    @property
    def support(self) -> tuple[float, float]:
        s = self.payload.get("support", {})
        return float(s.get("calibration_support_low")), \
            float(s.get("calibration_support_high"))

    @property
    def reference_unit(self) -> str:
        return self.payload.get("reference_unit", "")

    @property
    def response_unit(self) -> str:
        return self.payload.get("response_unit", "")

    @property
    def shared_offset_sd(self) -> float:
        return float(self.payload.get("reference_shared_offset_sd", 0.0))

    @property
    def seed(self) -> int:
        return int(self.payload.get("seed", 0))

    @property
    def monotone_direction(self) -> str:
        return self.payload.get("invertibility", {}).get("direction") or "increasing"
