"""Static report generation (HTML + Markdown), no interactive server.

The report is self-contained and traces every sample estimate back to its
readings and every selection step back to the reference levels.
"""
from __future__ import annotations

import html
import json
import math
from typing import Optional

from .apply import ApplyResult
from .compare import CompareResult
from .fit import FitResult


def _fmt(v, digits=6):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return "—"
        if v == 0:
            return "0"
        if abs(v) >= 1e5 or abs(v) < 1e-4:
            return f"{v:.{digits}g}"
        return f"{v:.{digits}g}"
    return str(v)


def _interval(iv):
    if not iv:
        return "—"
    return f"[{_fmt(iv[0])}, {_fmt(iv[1])}]"


def _esc(s) -> str:
    return html.escape(str(s))


def build_markdown(fit: FitResult, app: ApplyResult, comp: CompareResult) -> str:
    p = fit.to_dict()
    lines = []
    lines.append("# Calibration run report\n")
    lines.append("Static report generated from the local calibration tool. "
                 "Synthetic data; no instrument connection.\n")

    lines.append("## Run declaration\n")
    lines.append(f"- Reference unit: `{fit.reference_unit}`")
    lines.append(f"- Response unit: `{fit.response_unit}`")
    lines.append(f"- Shared error source: `{fit.shared_error_id}` "
                 f"(certificate offset SD = {_fmt(fit.reference_shared_offset_sd)} "
                 f"{fit.reference_unit})")
    lines.append(f"- Working range: {fit.working_range} {fit.reference_unit}")
    lines.append(f"- Calibration support: [{_fmt(fit.support_low)}, "
                 f"{_fmt(fit.support_high)}] {fit.reference_unit}")
    lines.append(f"- Monte Carlo draws: {app.draws}, seed: {app.seed}, "
                 f"interval level: {app.interval_level}")
    lines.append("")

    lines.append("## Model selection (grouped leave-one-level-out CV)\n")
    lines.append("Candidates (each reference level is held out as a group; its "
                 "repeats are never split):\n")
    lines.append("| degree | eligible | CV RMSE (mV) | CV MSE | reason |")
    lines.append("|---|---|---|---|---|")
    for c in fit.candidates:
        lines.append(f"| {c.degree} | {_fmt(c.eligible)} | {_fmt(c.cv_rmse)} | "
                     f"{_fmt(c.cv_mse)} | {_esc(c.reason)} |")
    lines.append("")
    if fit.selected:
        lines.append(f"**Selected degree {fit.degree}** by lowest pooled CV MSE.")
    else:
        lines.append("**No model selected.**")
    lines.append("")

    lines.append("## Fitted model\n")
    if fit.degree is not None:
        terms = []
        for d, c in enumerate(fit.coefficients):
            if d == 0:
                terms.append(f"{_fmt(c)}")
            elif d == 1:
                terms.append(f"{_fmt(c)}·x")
            else:
                terms.append(f"{_fmt(c)}·x^{d}")
        lines.append(f"`response = {' + '.join(terms)}`")
        lines.append("")
        lines.append(f"- Residual SD: {_fmt(fit.residual_sd)} "
                     f"(dof {fit.residual_dof})")
        lines.append(f"- Monotone on support: {_fmt(fit.monotone)} "
                     f"({fit.monotone_direction})")
        lines.append(f"- Minimum |df/dx| on support: {_fmt(fit.min_abs_derivative)}")
    lines.append("")

    lines.append("### Reference levels\n")
    lines.append("| level | reference | n | batches | response mean | response SD |")
    lines.append("|---|---|---|---|---|---|")
    for lv in fit.levels:
        lines.append(f"| {_esc(lv.level_id)} | {_fmt(lv.reference_value)} | {lv.n} | "
                     f"{', '.join(lv.batches)} | {_fmt(lv.response_mean)} | "
                     f"{_fmt(lv.response_sd)} |")
    lines.append("")

    lines.append("## Sample results\n")
    lines.append(f"Unit: `{fit.reference_unit}`. Intervals are central "
                 f"{int(app.interval_level*100)}% from common-draw Monte Carlo.\n")
    lines.append("| sample | status | estimate | interval | response mean | repeats |")
    lines.append("|---|---|---|---|---|---|")
    for s in app.samples:
        reps = ", ".join(f"{_fmt(r['response'])}" for r in s.repeats)
        lines.append(f"| {_esc(s.sample_id)} | {s.status} | {_fmt(s.estimate)} | "
                     f"{_interval(s.interval)} | {_fmt(s.response_mean)} | {reps} |")
    lines.append("")

    lines.append("## Combinations (shared-error aware)\n")
    lines.append("| combination | coefficient sum | estimate | interval | total SD | "
                 "shared contribution | naive independent SD |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in comp.combinations:
        lines.append(f"| {_esc(c.combo_id)} | {_fmt(c.coefficient_sum)} | "
                     f"{_fmt(c.estimate)} | {_interval(c.interval)} | "
                     f"{_fmt(c.total_sd)} | {_fmt(c.shared_contribution_sd)} | "
                     f"{_fmt(c.naive_independent_sd)} |")
    lines.append("")

    lines.append("## Issues\n")
    if fit.issues or app.issues or comp.issues:
        for i in list(fit.issues) + list(app.issues) + list(comp.issues):
            loc = f" line {i.line}" if i.line is not None else ""
            lines.append(f"- **{i.severity}** `{i.code}` [{_esc(i.item)}{loc}]: "
                         f"{_esc(i.message)}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def build_html(fit: FitResult, app: ApplyResult, comp: CompareResult) -> str:
    md_like = build_markdown(fit, app, comp)
    # A minimal, dependency-free static HTML wrapper. We render from structured
    # data rather than naively converting markdown.
    fit_d = fit.to_dict()
    parts = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<title>Calibration report</title>")
    parts.append("<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:70rem;"
                 "line-height:1.45}table{border-collapse:collapse;margin:.8rem 0}"
                 "th,td{border:1px solid #bbb;padding:.25rem .5rem;text-align:left}"
                 "code{background:#f2f2f2;padding:.1rem .3rem}."
                 "err{color:#a00}.warn{color:#a60}.muted{color:#666}</style></head><body>")
    parts.append("<h1>Calibration run report</h1>")
    parts.append("<p class='muted'>Static report from the local calibration tool. "
                 "Synthetic data; no instrument connection, no authentication.</p>")

    parts.append("<h2>Run declaration</h2><ul>")
    parts.append(f"<li>Reference unit: <code>{_esc(fit.reference_unit)}</code></li>")
    parts.append(f"<li>Response unit: <code>{_esc(fit.response_unit)}</code></li>")
    parts.append(f"<li>Shared error source: <code>{_esc(fit.shared_error_id)}</code>, "
                 f"certificate offset SD = {_fmt(fit.reference_shared_offset_sd)} "
                 f"{_esc(fit.reference_unit)}</li>")
    parts.append(f"<li>Working range: {_esc(fit.working_range)} "
                 f"{_esc(fit.reference_unit)}</li>")
    parts.append(f"<li>Calibration support: [{_fmt(fit.support_low)}, "
                 f"{_fmt(fit.support_high)}] {_esc(fit.reference_unit)}</li>")
    parts.append(f"<li>Monte Carlo draws: {app.draws}, seed: {app.seed}, "
                 f"interval level: {app.interval_level}</li>")
    parts.append("</ul>")

    parts.append("<h2>Model selection (grouped leave-one-level-out CV)</h2>")
    parts.append("<table><tr><th>degree</th><th>eligible</th><th>CV RMSE</th>"
                 "<th>CV MSE</th><th>reason</th></tr>")
    for c in fit.candidates:
        parts.append(f"<tr><td>{c.degree}</td><td>{_fmt(c.eligible)}</td>"
                     f"<td>{_fmt(c.cv_rmse)}</td><td>{_fmt(c.cv_mse)}</td>"
                     f"<td>{_esc(c.reason)}</td></tr>")
    parts.append("</table>")
    if fit.selected:
        parts.append(f"<p><strong>Selected degree {fit.degree}</strong> by lowest "
                     "pooled CV mean squared error.</p>")
    else:
        parts.append("<p class='err'><strong>No model selected.</strong></p>")

    parts.append("<h2>Fitted model</h2>")
    if fit.degree is not None:
        terms = []
        for d, c in enumerate(fit.coefficients):
            if d == 0:
                terms.append(_fmt(c))
            elif d == 1:
                terms.append(f"{_fmt(c)}·x")
            else:
                terms.append(f"{_fmt(c)}·x<sup>{d}</sup>")
        parts.append(f"<p><code>response = {' + '.join(terms)}</code></p>")
        parts.append(f"<ul><li>Residual SD: {_fmt(fit.residual_sd)} "
                     f"(dof {fit.residual_dof})</li>"
                     f"<li>Monotone on support: {_fmt(fit.monotone)} "
                     f"({_esc(fit.monotone_direction)})</li>"
                     f"<li>Minimum |df/dx|: {_fmt(fit.min_abs_derivative)}</li></ul>")
    parts.append("<h3>Reference levels</h3>")
    parts.append("<table><tr><th>level</th><th>reference</th><th>n</th><th>batches</th>"
                 "<th>response mean</th><th>response SD</th></tr>")
    for lv in fit.levels:
        parts.append(f"<tr><td>{_esc(lv.level_id)}</td>"
                     f"<td>{_fmt(lv.reference_value)}</td><td>{lv.n}</td>"
                     f"<td>{_esc(', '.join(lv.batches))}</td>"
                     f"<td>{_fmt(lv.response_mean)}</td>"
                     f"<td>{_fmt(lv.response_sd)}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Sample results</h2>")
    parts.append(f"<p>Unit <code>{_esc(fit.reference_unit)}</code>; intervals are "
                 f"central {int(app.interval_level*100)}% from common-draw Monte "
                 "Carlo. Every sample shares the certificate offset and the fitted "
                 "parameters, so samples are not independent.</p>")
    parts.append("<table><tr><th>sample</th><th>status</th><th>estimate</th>"
                 "<th>interval</th><th>response mean</th><th>repeats</th></tr>")
    for s in app.samples:
        reps = ", ".join(_fmt(r['response']) for r in s.repeats)
        cls = "" if s.status == "ok" else " class='warn'"
        parts.append(f"<tr{cls}><td>{_esc(s.sample_id)}</td><td>{s.status}</td>"
                     f"<td>{_fmt(s.estimate)}</td><td>{_interval(s.interval)}</td>"
                     f"<td>{_fmt(s.response_mean)}</td><td>{_esc(reps)}</td></tr>")
    parts.append("</table>")
    for s in app.samples:
        if s.notes:
            parts.append(f"<p class='muted'>{_esc(s.sample_id)}: "
                         f"{_esc('; '.join(s.notes))}</p>")

    parts.append("<h2>Combinations (shared-error aware)</h2>")
    parts.append("<table><tr><th>combination</th><th>coefficient sum</th>"
                 "<th>estimate</th><th>interval</th><th>total SD</th>"
                 "<th>shared contribution</th><th>naive independent SD</th></tr>")
    for c in comp.combinations:
        parts.append(f"<tr><td>{_esc(c.combo_id)}</td>"
                     f"<td>{_fmt(c.coefficient_sum)}</td>"
                     f"<td>{_fmt(c.estimate)}</td>"
                     f"<td>{_interval(c.interval)}</td>"
                     f"<td>{_fmt(c.total_sd)}</td>"
                     f"<td>{_fmt(c.shared_contribution_sd)}</td>"
                     f"<td>{_fmt(c.naive_independent_sd)}</td></tr>")
    parts.append("</table>")
    for c in comp.combinations:
        parts.append(f"<p class='muted'>{_esc(c.combo_id)}: "
                     f"{_esc(c._explanation())}</p>")

    parts.append("<h2>Issues</h2><ul>")
    all_issues = list(fit.issues) + list(app.issues) + list(comp.issues)
    if all_issues:
        for i in all_issues:
            cls = "err" if i.severity == "error" else "warn"
            loc = f" line {i.line}" if i.line is not None else ""
            parts.append(f"<li class='{cls}'><strong>{i.severity}</strong> "
                         f"<code>{_esc(i.code)}</code> "
                         f"[{_esc(i.item)}{loc}]: {_esc(i.message)}</li>")
    else:
        parts.append("<li>none</li>")
    parts.append("</ul>")

    parts.append("<h2>Reproducibility</h2>")
    parts.append(f"<p>Model JSON carries the coefficients, coefficient covariance, "
                 f"residual SD, support, and the calibration readings. Monte Carlo "
                 f"seed {app.seed} with {app.draws} draws. The model artefact "
                 "(<code>model.json</code>) is inspectable and sufficient to recompute "
                 "these intervals.</p>")

    parts.append("<details><summary>Assumptions (published)</summary><ul>")
    for a in app.assumptions + comp.assumptions:
        parts.append(f"<li>{_esc(a)}</li>")
    parts.append("</ul></details>")

    parts.append("<details><summary>Trace: calibration readings</summary>")
    parts.append("<table><tr><th>line</th><th>level</th><th>reference</th>"
                 "<th>batch</th><th>response</th></tr>")
    for r in fit_d["calibration_readings"]:
        parts.append(f"<tr><td>{r['line']}</td><td>{_esc(r['level_id'])}</td>"
                     f"<td>{_fmt(r['reference_value'])}</td>"
                     f"<td>{_esc(r['measurement_batch'])}</td>"
                     f"<td>{_fmt(r['response'])}</td></tr>")
    parts.append("</table></details>")

    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(path_html: str, fit: FitResult, app: ApplyResult,
                 comp: CompareResult, path_md: Optional[str] = None) -> None:
    with open(path_html, "w", encoding="utf-8") as fh:
        fh.write(build_html(fit, app, comp))
    if path_md:
        with open(path_md, "w", encoding="utf-8") as fh:
            fh.write(build_markdown(fit, app, comp))
