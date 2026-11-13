# Calibration tool

Local, offline calibration for one run: fit a response model against reference
levels, invert unknown-sample readings, and compare samples while accounting
for the errors they share. No instrument connection, no authentication, no
server.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` pins `numpy>=1.26,<3` and `scipy>=1.11,<2`; Python 3.11 is
assumed. `scipy` is available but the current implementation only needs NumPy
(the covariance/inversion code is closed-form), so it works either way.

## Inputs

* `fixtures/calibration.csv` — columns `level_id, reference_value,
  measurement_batch, response`. Repeated readings of the same level are
  retained; `measurement_batch` marks the batch of each reading.
* `fixtures/samples.csv` — columns `sample_id, repeat_id, response`; each row
  is one reading of a sample.
* `fixtures/plan.json` — units, shared certificate offset SD, working range,
  candidate degrees, RNG seed, and named linear combinations of samples.

## Run

```sh
# Full pipeline: model + sample results + combinations + static report
.venv/bin/python -m calibration_tool run \
    --cal fixtures/calibration.csv --samples fixtures/samples.csv \
    --plan fixtures/plan.json --out out/

# Or step by step
.venv/bin/python -m calibration_tool fit     --out out/          # writes model.json
.venv/bin/python -m calibration_tool apply   --model out/model.json --out out/
.venv/bin/python -m calibration_tool compare --model out/model.json --out out/
```

`--draws N` sets the Monte Carlo draw count (default 4000). `--samples` is
optional for `fit`. Exit codes: `0` success, `1` fitted but inverted curve
unusable, `2` invalid input (each problem is itemised on stderr).

## Outputs (in `--out`)

* `model.json` — inspectable model: coefficients (ascending powers), coefficient
  covariance, residual SD, support, selected degree, all candidate CV scores,
  reference-level summaries, and every calibration reading with its line number.
  Sufficient to recompute sample intervals without re-fitting.
* `apply.json` — per-sample estimates, intervals, status, and the error
  components (shared certificate offset, calibration parameters, reading noise).
* `compare.json` — combination estimates, intervals, coefficient sums, the
  shared-error contribution, the naive independent SD for contrast, and the
  cross-sample estimate covariance from the common draws.
* `report.html`, `report.md` — static, self-contained report tracing selection
  back to reference levels and each estimate back to the sample readings.
* `result.json` — machine-readable run status and all issues.

## Method and assumptions

* **Model.** `response = p0 + p1*x (+ p2*x^2)` on reference value `x` and
  response in the declared units. Readings are pooled across batches. Batch is
  treated as a diagnostic only: `samples.csv` carries no batch label, so a batch
  offset cannot be applied at inversion; with the balanced design the constant
  batch effect cancels from the level means that drive the fit.
* **Selection.** Candidate degrees come from the plan. Grouped
  leave-one-**reference-level**-out cross-validation holds out all repeats of a
  level together (repeats of one level are never split), fits on the rest, and
  predicts the held-out readings. The degree with the lowest pooled CV MSE wins;
  near-ties (relative `1e-9`) prefer the simpler degree. A degree is eligible
  only with at least `degree+2` distinct levels.
* **Inversion.** Bisection on the strictly monotone supported interval — never
  an arbitrary root of the quadratic. The support is the overlap of the
  calibration reference span and the declared working range. Responses outside
  the fitted response range are **not** extrapolated: the sample is marked
  `out_of_range` for review. If the curve is non-monotone on support or its
  slope is numerically zero, inversion is refused and every sample is marked
  `not_invertible`, with the reason recorded in `model.json`.
* **Shared certificate offset.** One offset `delta ~ N(0, sigma_shared^2)` is
  common to all reference levels (it shifts the certificate reference values)
  and is therefore common to every sample inverted with this calibration. The
  point estimate is the nominal inverse; the interval adds the common offset,
  so single-sample and combination intervals both carry it. Calibration
  parameter uncertainty is propagated through the OLS coefficient covariance.
* **Shared draws.** Parameter and certificate draws are generated **once** and
  reused for every sample. Combinations are formed replicate-by-replicate, so
  correlated estimates are handled correctly: a difference (coefficient sum 0)
  cancels the shared offset, a mean (sum 1) retains it. Each combination reports
  the empirical total SD, the shared contribution `|sum a_s| * sigma_shared`, and
  the naive independent SD for comparison.
* **Reproducibility.** All randomness uses `plan.seed`. Per-sample reading
  streams are derived from `(seed, sample_id)`, and shared draws from the seed,
  so reordering input rows does not change any interval.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The suite includes independent hand checks rather than "the fit succeeded":
exact linear inversion compared with `(y-3)/2`; curved inversion compared with
the quadratic formula on the increasing branch; a decreasing curve; a
non-monotone curve and a flat curve that must refuse inversion; insufficient
levels rejecting degree 2; out-of-range samples not extrapolated; illegal units,
missing values and duplicate repeats itemised per row; row-order invariance;
seed reproducibility; and an independent resampling of an `S2-S1` difference
(separate RNG, raw sampling from the published covariance) checked against the
tool's interval. The shared-offset cases confirm the difference cancels and the
mean retains the common term.
