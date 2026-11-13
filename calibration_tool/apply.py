"""Invert calibrations to report sample quantities with intervals.

Statistical model (published assumptions)
-----------------------------------------
Calibration readings:   y_ij = f(x_i + delta; theta) + eps_ij
  * x_i      nominal reference value of level i (declared reference unit)
  * delta    common certificate offset, delta ~ N(0, sigma_shared^2), the SAME
             delta for every reference level and every sample (one certificate)
  * theta    model coefficients (degree selected in fit)
  * eps_ij   independent reading noise, variance from repeat residuals

Sample readings:         r_sj = f(c_s; theta) + eta_sj
  * c_s      true sample quantity we want
  * eta_sj   independent reading noise

Because calibration is fitted against *nominal* x, the fitted curve satisfies
f_nom(x) ~= f_true(x + delta). Inverting a sample response with the nominal
curve therefore gives x_hat, and the true quantity is c_s = x_hat + delta.
The offset is thus an additive, perfectly shared term.

Four uncertainty sources are propagated by common Monte Carlo:
  1. theta draws from N(theta_hat, Cov_hat)  -- shared by all samples
  2. delta draw N(0, sigma_shared^2)         -- shared by all samples
  3. reading noise of each sample mean       -- independent per sample
  4. (the point curve itself is the central estimate)
The SAME draws of theta and delta are used for every sample, so a difference or
mean is formed on each replicate; shared sources cancel or persist exactly as
they would in reality.

Inversion is by bisection on the strictly monotone supported interval. We never
solve a quadratic and pick a root; a response outside the fitted range is not
extrapolated and is flagged for review.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .fit import CalibrationModel
from .issues import Issue

DEFAULT_DRAWS = 4000
INVERSE_TOL = 1e-12
INVERSE_MAX_ITER = 400
INTERVAL_LEVEL = 0.95


@dataclass
class SampleResult:
    sample_id: str
    n_repeats: int
    repeats: list[dict]
    response_mean: float
    response_sd: float
    response_se: float
    status: str                     # ok / out_of_range / not_invertible / ...
    reason: str
    estimate: Optional[float]
    interval: Optional[list[float]]
    interval_level: float
    draws_valid: Optional[int]
    uncertainty_sd: Optional[float]
    shared_offset_sd: Optional[float]
    parameter_sd: Optional[float]
    reading_sd: Optional[float]
    unit: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "n_repeats": self.n_repeats,
            "repeats": self.repeats,
            "response_mean": self.response_mean,
            "response_sd": self.response_sd,
            "response_se": self.response_se,
            "status": self.status,
            "reason": self.reason,
            "estimate": self.estimate,
            "interval": self.interval,
            "interval_level": self.interval_level,
            "draws_valid": self.draws_valid,
            "uncertainty_sd": self.uncertainty_sd,
            "components": {
                "shared_certificate_offset_sd": self.shared_offset_sd,
                "calibration_parameter_sd": self.parameter_sd,
                "reading_noise_sd": self.reading_sd,
            },
            "unit": self.unit,
            "notes": self.notes,
            "independence": (
                "estimate shares the certificate offset and the fitted parameters "
                "with every other sample from this calibration; do not treat "
                "samples as independent"),
        }


@dataclass
class ApplyResult:
    samples: list[SampleResult]
    draws: int
    seed: int
    interval_level: float
    method: str
    assumptions: list[str]
    issues: list[Issue]
    draws_table: Optional[np.ndarray] = None   # shape (draws, n_samples), internal

    @property
    def by_id(self) -> dict[str, SampleResult]:
        return {s.sample_id: s for s in self.samples}

    def to_dict(self) -> dict:
        return {
            "schema": "calibration-apply/1",
            "method": self.method,
            "draws": self.draws,
            "seed": self.seed,
            "interval_level": self.interval_level,
            "shared_error_id": getattr(self, "shared_error_id", None),
            "assumptions": self.assumptions,
            "samples": [s.to_dict() for s in self.samples],
            "issues": [i.to_dict() for i in self.issues],
        }


def _derivative(coeffs: list[float], x: float) -> float:
    return sum(d * c * x ** (d - 1) for d, c in enumerate(coeffs) if d >= 1)


def _eval(coeffs: list[float], x: float) -> float:
    return sum(c * x ** d for d, c in enumerate(coeffs))


def _invert_bisection(coeffs: list[float], target: float, lo: float, hi: float,
                      direction: str) -> Optional[float]:
    """Invert a strictly monotone curve on [lo, hi] by bisection.

    Returns None when the target lies outside the curve's range on the support
    (no extrapolation).
    """
    f_lo = _eval(coeffs, lo)
    f_hi = _eval(coeffs, hi)
    if direction == "increasing":
        if target < f_lo or target > f_hi:
            return None
    else:
        if target > f_lo or target < f_hi:
            return None
    a, b = lo, hi
    tol = INVERSE_TOL * max(1.0, abs(lo), abs(hi))
    for _ in range(INVERSE_MAX_ITER):
        if b - a <= tol:
            break
        mid = 0.5 * (a + b)
        fm = _eval(coeffs, mid)
        if direction == "increasing":
            if fm < target:
                a = mid
            else:
                b = mid
        else:
            if fm > target:
                a = mid
            else:
                b = mid
    return 0.5 * (a + b)


def apply_model(model: CalibrationModel, sample_rows, draws: Optional[int] = None
                ) -> ApplyResult:
    payload = model.to_dict()
    issues: list[Issue] = []
    unit = model.reference_unit
    seed = model.seed
    n_draws = int(draws) if draws is not None else DEFAULT_DRAWS

    assumptions = [
        "reading noise is independent across repeats and samples; calibration "
        "repeat residuals estimate calibration noise, sample repeat SD estimates "
        "sample reading noise",
        "one certificate offset delta ~ N(0, sigma_shared^2) is common to all "
        "reference levels and all samples; it is additive on the inferred quantity",
        "coefficients follow N(theta_hat, OLS covariance); this is a local "
        "normal approximation, adequate because holdout residuals are small",
        f"parameter and shared-offset draws are common across samples; the "
        f"Monte Carlo uses {n_draws} draws with seed {seed}",
        "intervals are equal-tailed central quantiles at the stated level; no "
        "extrapolation beyond the calibration support",
    ]

    # Group sample rows, preserving file order for traceability.
    order: list[str] = []
    groups: dict[str, list] = {}
    for row in sample_rows:
        if row.sample_id not in groups:
            groups[row.sample_id] = []
            order.append(row.sample_id)
        groups[row.sample_id].append(row)

    if not model.ok:
        issues.append(Issue("error", "model_not_invertible", "calibration",
                            "selected calibration is not invertible; no samples applied"))
        results = []
        for sid in order:
            rs = groups[sid]
            vals = np.array([r.response for r in rs], dtype=float)
            results.append(SampleResult(
                sample_id=sid, n_repeats=len(rs),
                repeats=[{"line": r.line, "repeat_id": r.repeat_id,
                          "response": r.response} for r in rs],
                response_mean=float(vals.mean()),
                response_sd=float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
                response_se=float(_se(vals)),
                status="not_invertible",
                reason="calibration model is not invertible (see calibration issues)",
                estimate=None, interval=None, interval_level=INTERVAL_LEVEL,
                draws_valid=None, uncertainty_sd=None, shared_offset_sd=None,
                parameter_sd=None, reading_sd=None, unit=unit))
        return ApplyResult(results, n_draws, seed, INTERVAL_LEVEL,
                           "none", assumptions, issues)

    coeffs = model.coefficients
    lo, hi = model.support
    direction = model.monotone_direction
    sigma_shared = model.shared_offset_sd
    cov = payload["model"]["coefficient_covariance"]
    cov = np.array(cov, dtype=float) if cov is not None else np.zeros((len(coeffs), len(coeffs)))
    res_sd = payload["model"].get("residual_sd")

    # Common draws: theta (draws x p) and delta (draws). Shared by all samples.
    # method="svd" is used because it tolerates a (semi)singular covariance,
    # which occurs for noiseless calibration where Cov(theta) is ~0.
    rng = np.random.default_rng(np.random.SeedSequence(seed))
    theta_draws = rng.multivariate_normal(np.array(coeffs, dtype=float), cov,
                                          size=n_draws, method="svd")
    delta_draws = rng.normal(0.0, sigma_shared, size=n_draws)

    results: list[SampleResult] = []
    point_estimates: list[Optional[float]] = []
    draw_matrix = np.full((n_draws, len(order)), np.nan, dtype=float)
    valid_counts: list[int] = []

    for col, sid in enumerate(order):
        rs = groups[sid]
        vals = np.array([r.response for r in rs], dtype=float)
        mean = float(vals.mean())
        sd = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
        se = _se(vals, fallback=res_sd)
        repeats = [{"line": r.line, "repeat_id": r.repeat_id, "response": r.response}
                   for r in rs]

        point = _invert_bisection(coeffs, mean, lo, hi, direction)
        point_estimates.append(point)
        notes: list[str] = []

        if point is None:
            if direction == "increasing":
                below = mean < _eval(coeffs, lo)
            else:
                below = mean > _eval(coeffs, lo)
            where = "below" if below else "above"
            status = "out_of_range"
            reason = (f"sample response mean {mean:.6g} {model.response_unit} is {where} "
                      f"the calibration response range "
                      f"[{min(_eval(coeffs, lo), _eval(coeffs, hi)):.6g}, "
                      f"{max(_eval(coeffs, lo), _eval(coeffs, hi)):.6g}] "
                      f"on support [{lo:.6g}, {hi:.6g}] {unit}; marked for review, "
                      "no extrapolation")
            results.append(SampleResult(
                sample_id=sid, n_repeats=len(rs), repeats=repeats,
                response_mean=mean, response_sd=sd, response_se=se,
                status=status, reason=reason, estimate=None, interval=None,
                interval_level=INTERVAL_LEVEL, draws_valid=0,
                uncertainty_sd=None, shared_offset_sd=sigma_shared,
                parameter_sd=None, reading_sd=None, unit=unit,
                notes=["point estimate would require extrapolation"]))
            valid_counts.append(0)
            continue

        # Replicate sampling: common theta and delta, independent reading noise.
        # Reading noise is applied to the response, then inverted per draw. The
        # reading stream is derived deterministically from (seed, sample_id) so
        # that reordering sample rows cannot change any sample's interval.
        reading_rng = np.random.default_rng(_sample_seed(seed, sid))
        reading_draws = reading_rng.normal(0.0, se, size=n_draws)
        sample_draws = np.full(n_draws, np.nan)
        for k in range(n_draws):
            x = _invert_bisection(theta_draws[k], mean + reading_draws[k], lo, hi, direction)
            if x is not None:
                sample_draws[k] = x + delta_draws[k]
        n_valid = int(np.count_nonzero(np.isfinite(sample_draws)))
        valid_counts.append(n_valid)
        draw_matrix[:, col] = sample_draws
        if n_valid < n_draws:
            frac = 1.0 - n_valid / n_draws
            notes.append(f"{frac:.2%} of parameter/reading draws fell outside the "
                         "drawn curve's range and were excluded (no extrapolation)")
            if frac > 0.05:
                issues.append(Issue(
                    "warning", "many_invalid_draws", f"sample {sid}",
                    f"{frac:.1%} of draws were non-invertible; interval may be biased"))

        if n_valid < 2:
            results.append(SampleResult(
                sample_id=sid, n_repeats=len(rs), repeats=repeats,
                response_mean=mean, response_sd=sd, response_se=se,
                status="not_invertible",
                reason="too few invertible parameter draws to form an interval",
                estimate=point, interval=None, interval_level=INTERVAL_LEVEL,
                draws_valid=n_valid, uncertainty_sd=None,
                shared_offset_sd=sigma_shared, parameter_sd=None,
                reading_sd=None, unit=unit, notes=notes))
            continue

        vd = sample_draws[np.isfinite(sample_draws)]
        alpha = (1.0 - INTERVAL_LEVEL) / 2.0
        lo_q, hi_q = np.quantile(vd, [alpha, 1.0 - alpha])
        est_sd = float(vd.std(ddof=1))

        # Variance decomposition (on valid draws).
        theta_only = sample_draws - delta_draws  # theta + reading, no shared delta
        theta_only_valid = theta_only[np.isfinite(theta_only)]
        param_plus_reading_var = (theta_only_valid.var(ddof=1)
                                  if theta_only_valid.size > 1 else 0.0)
        # Isolate the reading contribution by re-inverting with theta fixed would
        # need extra work; report reading SE mapped to x via local slope.
        slope = _derivative(coeffs, point)
        reading_sd_x = abs(se / slope) if slope not in (0.0,) else None
        parameter_sd_x = math.sqrt(max(param_plus_reading_var - (reading_sd_x or 0.0) ** 2,
                                       0.0)) if reading_sd_x is not None else None

        status = "ok"
        reason = "inverted on the monotone calibrated support"
        results.append(SampleResult(
            sample_id=sid, n_repeats=len(rs), repeats=repeats,
            response_mean=mean, response_sd=sd, response_se=se,
            status=status, reason=reason, estimate=point,
            interval=[float(lo_q), float(hi_q)], interval_level=INTERVAL_LEVEL,
            draws_valid=n_valid, uncertainty_sd=est_sd,
            shared_offset_sd=sigma_shared, parameter_sd=parameter_sd_x,
            reading_sd=reading_sd_x, unit=unit, notes=notes))

    result = ApplyResult(results, n_draws, seed, INTERVAL_LEVEL,
                         "common-draw Monte Carlo inversion (bisection; additive "
                         "shared certificate offset)", assumptions, issues,
                         draws_table=draw_matrix)
    result.shared_error_id = payload.get("shared_error_id")
    result.sample_order = order
    return result


def _se(vals: np.ndarray, fallback: Optional[float] = None) -> float:
    n = vals.size
    if n > 1:
        return float(vals.std(ddof=1) / math.sqrt(n))
    if fallback:
        return float(fallback)
    return 0.0


def _sample_seed(seed: int, sample_id: str) -> np.random.SeedSequence:
    """Stable per-sample stream: independent of file order and of other samples."""
    # Derive a numeric tag from the sample id in a platform-stable way.
    tag = int.from_bytes(sample_id.encode("utf-8"), "little") % (2 ** 64)
    return np.random.SeedSequence([int(seed), tag])
