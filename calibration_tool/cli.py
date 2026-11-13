"""Command-line entry point: fit / apply / compare / report.

Usage:
  python -m calibration_tool run  --cal fixtures/calibration.csv \
       --samples fixtures/samples.csv --plan fixtures/plan.json --out out/
  python -m calibration_tool fit  ...  (writes model.json)
  python -m calibration_tool apply --model out/model.json --samples ... --out ...
  python -m calibration_tool compare --model ... --samples ... --plan ... --out ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .apply import ApplyResult, apply_model
from .compare import compare_model
from .fit import CalibrationModel, fit_model
from .report import write_report
from .validation import (InputBundle, load_inputs, load_plan, parse_calibration,
                         parse_samples)


def _dump(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, allow_nan=False)
        fh.write("\n")


def _print_issues(issues):
    for i in issues:
        loc = f" line {i.line}" if i.line is not None else ""
        print(f"[{i.severity}] {i.code} ({i.item}{loc}): {i.message}", file=sys.stderr)


def cmd_run(args):
    bundle = load_inputs(args.cal, args.samples, args.plan)
    _print_issues(bundle.issues)
    if not bundle.ok:
        print("Input validation failed; see issues above.", file=sys.stderr)
        os.makedirs(args.out, exist_ok=True)
        _dump({"schema": "calibration-run/1",
               "ok": False,
               "issues": [i.to_dict() for i in bundle.issues]},
              os.path.join(args.out, "result.json"))
        return 2
    fit, model = fit_model(bundle)
    _print_issues(fit.issues)
    app = apply_model(model, bundle.sample_rows, draws=args.draws)
    _print_issues(app.issues)
    comp = compare_model(model, app, bundle.plan.get("combinations", []))
    _print_issues(comp.issues)

    os.makedirs(args.out, exist_ok=True)
    _dump(model.to_dict(), os.path.join(args.out, "model.json"))
    _dump(app.to_dict(), os.path.join(args.out, "apply.json"))
    _dump(comp.to_dict(), os.path.join(args.out, "compare.json"))
    bundle_ok = not any(i.severity == "error" for i in fit.issues)
    run_result = {
        "schema": "calibration-run/1",
        "ok": bool(fit.ok and bundle_ok),
        "model_selected_degree": fit.degree,
        "model_invertible": fit.ok,
        "issues": [i.to_dict() for i in
                   list(bundle.issues) + list(fit.issues) +
                   list(app.issues) + list(comp.issues)],
        "artifacts": {
            "model": "model.json",
            "apply": "apply.json",
            "compare": "compare.json",
            "report_html": "report.html",
            "report_md": "report.md",
        },
    }
    _dump(run_result, os.path.join(args.out, "result.json"))
    write_report(os.path.join(args.out, "report.html"), fit, app, comp,
                 os.path.join(args.out, "report.md"))
    return 0 if run_result["ok"] else 1


def _load_model(path):
    with open(path, "r", encoding="utf-8") as fh:
        return CalibrationModel.from_dict(json.load(fh))


def cmd_fit(args):
    # fit needs only calibration + plan; samples are optional for this entry.
    plan, plan_issues = load_plan(args.plan)
    issues = list(plan_issues)
    if plan:
        from .validation import validate_plan
        issues.extend(validate_plan(plan))
    cal_rows, cal_issues = parse_calibration(args.cal)
    issues.extend(cal_issues)
    for i in issues:
        _print_issues([i])
    sample_rows: list = []
    if args.samples and os.path.exists(args.samples):
        sample_rows, sample_issues = parse_samples(args.samples)
        issues.extend(sample_issues)
        _print_issues(sample_issues)
    bundle = InputBundle(plan=plan, cal_rows=cal_rows, sample_rows=sample_rows,
                         issues=issues)
    if not bundle.ok:
        print("Input validation failed; see issues above.", file=sys.stderr)
        return 2
    fit, model = fit_model(bundle)
    _print_issues(fit.issues)
    os.makedirs(args.out, exist_ok=True)
    _dump(model.to_dict(), os.path.join(args.out, "model.json"))
    return 0 if fit.ok else 1


def cmd_apply(args):
    model = _load_model(args.model)
    bundle = load_inputs(args.cal, args.samples, args.plan)
    _print_issues(bundle.issues)
    if not bundle.ok:
        return 2
    app = apply_model(model, bundle.sample_rows, draws=args.draws)
    _print_issues(app.issues)
    os.makedirs(args.out, exist_ok=True)
    _dump(app.to_dict(), os.path.join(args.out, "apply.json"))
    return 0


def cmd_compare(args):
    model = _load_model(args.model)
    bundle = load_inputs(args.cal, args.samples, args.plan)
    _print_issues(bundle.issues)
    if not bundle.ok:
        return 2
    app = apply_model(model, bundle.sample_rows, draws=args.draws)
    comp = compare_model(model, app, bundle.plan.get("combinations", []))
    _print_issues(comp.issues)
    os.makedirs(args.out, exist_ok=True)
    _dump(comp.to_dict(), os.path.join(args.out, "compare.json"))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="calibration_tool")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--cal", default="fixtures/calibration.csv")
        sp.add_argument("--samples", default="fixtures/samples.csv")
        sp.add_argument("--plan", default="fixtures/plan.json")
        sp.add_argument("--out", default="out")
        sp.add_argument("--draws", type=int, default=4000)
    sp = sub.add_parser("run", help="fit + apply + compare + report")
    common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("fit", help="fit and write model.json")
    common(sp)
    sp.set_defaults(func=cmd_fit)

    sp = sub.add_parser("apply", help="apply a saved model to samples")
    sp.add_argument("--model", required=True)
    common(sp)
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("compare", help="compare using a saved model")
    sp.add_argument("--model", required=True)
    common(sp)
    sp.set_defaults(func=cmd_compare)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
