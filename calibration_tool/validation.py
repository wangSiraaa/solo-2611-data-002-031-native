"""Input loading and item-by-item validation.

Every problem is reported against the concrete row / level / sample / plan
field so the operator can fix it. Validation never silently drops a bad value;
parse functions emit rows only for fully usable records and record an issue for
anything else.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from typing import Optional

from .issues import Issue
from .units import validate_units

CAL_COLUMNS = ["level_id", "reference_value", "measurement_batch", "response"]
SAMPLE_COLUMNS = ["sample_id", "repeat_id", "response"]

# Candidate degrees this tool can fit *and invert*. Degree 1 and 2 only: the
# inverse of a quadratic is chosen analytically on the monotone branch, never by
# picking an arbitrary root. Other degrees would need their own inverse policy.
SUPPORTED_DEGREES = (1, 2)


@dataclass(frozen=True)
class CalRow:
    level_id: str
    reference_value: float
    measurement_batch: str
    response: float
    line: int


@dataclass(frozen=True)
class SampleRow:
    sample_id: str
    repeat_id: str
    response: float
    line: int


@dataclass
class InputBundle:
    plan: dict
    cal_rows: list[CalRow]
    sample_rows: list[SampleRow]
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _as_float(text, item, line, issues, required_unit_ctx=""):
    if text is None or str(text).strip() == "":
        issues.append(Issue("error", "missing_value", item,
                            f"required value is missing{required_unit_ctx}", line))
        return None
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        issues.append(Issue("error", "not_a_number", item,
                            f"value {text!r} is not a finite number{required_unit_ctx}", line))
        return None
    if not math.isfinite(value):
        issues.append(Issue("error", "not_a_number", item,
                            f"value {text!r} is not finite{required_unit_ctx}", line))
        return None
    return value


def load_plan(path: str) -> tuple[dict, list[Issue]]:
    issues: list[Issue] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except FileNotFoundError:
        return {}, [Issue("error", "file_missing", "plan.json", f"plan not found: {path}")]
    except json.JSONDecodeError as exc:
        return {}, [Issue("error", "bad_json", "plan.json",
                          f"plan is not valid JSON: {exc}")]
    if not isinstance(plan, dict):
        return {}, [Issue("error", "bad_json", "plan.json", "plan root must be an object")]
    return plan, issues


def validate_plan(plan: dict) -> list[Issue]:
    issues: list[Issue] = []

    # Units are validated separately but merged into the same issue stream.
    issues.extend(validate_units(plan))

    # Shared certificate offset SD.
    offset = plan.get("reference_shared_offset_sd")
    if not isinstance(offset, (int, float)) or isinstance(offset, bool) \
            or not math.isfinite(float(offset)):
        issues.append(Issue("error", "bad_shared_offset", "plan.reference_shared_offset_sd",
                            "reference_shared_offset_sd must be a finite number"))
    elif float(offset) < 0:
        issues.append(Issue("error", "bad_shared_offset", "plan.reference_shared_offset_sd",
                            "reference_shared_offset_sd must be >= 0"))

    shared_id = plan.get("shared_error_id")
    if not isinstance(shared_id, str) or not shared_id.strip():
        issues.append(Issue("error", "missing_shared_error_id", "plan.shared_error_id",
                            "shared_error_id is required so the common offset can be named"))

    # Working range.
    wr = plan.get("working_range")
    if not isinstance(wr, (list, tuple)) or len(wr) != 2 or \
            not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
                    for v in wr):
        issues.append(Issue("error", "bad_working_range", "plan.working_range",
                            "working_range must be two finite numbers [low, high]"))
    elif not (float(wr[0]) < float(wr[1])):
        issues.append(Issue("error", "bad_working_range", "plan.working_range",
                            "working_range low must be strictly less than high"))

    # Candidate degrees.
    degrees = plan.get("candidate_degrees")
    if not isinstance(degrees, (list, tuple)) or len(degrees) == 0:
        issues.append(Issue("error", "bad_degrees", "plan.candidate_degrees",
                            "candidate_degrees must be a non-empty list"))
    else:
        clean = []
        for d in degrees:
            if not isinstance(d, int) or isinstance(d, bool):
                issues.append(Issue("error", "bad_degrees", "plan.candidate_degrees",
                                    f"candidate degree {d!r} is not an integer"))
            elif d not in SUPPORTED_DEGREES:
                issues.append(Issue("error", "unsupported_degree", "plan.candidate_degrees",
                                    f"degree {d} is not supported; this tool fits and "
                                    f"inverts {list(SUPPORTED_DEGREES)} only"))
            elif d in clean:
                issues.append(Issue("error", "duplicate_degree", "plan.candidate_degrees",
                                    f"degree {d} is listed more than once"))
            else:
                clean.append(d)
        if clean and len(clean) != len(set(clean)):
            pass  # already reported
        if not clean and not any(i.code in ("bad_degrees", "unsupported_degree")
                                 for i in issues):
            issues.append(Issue("error", "bad_degrees", "plan.candidate_degrees",
                                "no usable candidate degree remains"))

    # Seed for reproducible Monte Carlo.
    seed = plan.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        issues.append(Issue("error", "bad_seed", "plan.seed",
                            "seed must be a non-negative integer for reproducible sampling"))

    # Combinations.
    combos = plan.get("combinations", [])
    if not isinstance(combos, list):
        issues.append(Issue("error", "bad_combinations", "plan.combinations",
                            "combinations must be a list"))
    else:
        seen_ids = set()
        for idx, combo in enumerate(combos):
            item = f"plan.combinations[{idx}]"
            if not isinstance(combo, dict) or "id" not in combo or "terms" not in combo:
                issues.append(Issue("error", "bad_combination", item,
                                    "each combination needs 'id' and 'terms'"))
                continue
            cid = combo["id"]
            if not isinstance(cid, str) or not cid.strip():
                issues.append(Issue("error", "bad_combination", item,
                                    "combination id must be a non-empty string"))
            elif cid in seen_ids:
                issues.append(Issue("error", "duplicate_combination", item,
                                    f"combination id {cid!r} is duplicated"))
            else:
                seen_ids.add(cid)
            terms = combo["terms"]
            if not isinstance(terms, dict) or not terms:
                issues.append(Issue("error", "bad_combination", item,
                                    "terms must be a non-empty mapping sample_id -> coefficient"))
                continue
            for sid, coef in terms.items():
                if not isinstance(sid, str) or not sid.strip():
                    issues.append(Issue("error", "bad_combination", item,
                                        "term sample_id must be a non-empty string"))
                if not isinstance(coef, (int, float)) or isinstance(coef, bool) \
                        or not math.isfinite(float(coef)):
                    issues.append(Issue("error", "bad_combination", item,
                                        f"coefficient for {sid!r} must be a finite number"))
            if len(terms) < 1:
                issues.append(Issue("error", "bad_combination", item,
                                    "at least one term is required"))
    return issues


def parse_calibration(path: str) -> tuple[list[CalRow], list[Issue]]:
    issues: list[Issue] = []
    rows: list[CalRow] = []
    try:
        fh = open(path, "r", encoding="utf-8", newline="")
    except FileNotFoundError:
        return [], [Issue("error", "file_missing", "calibration.csv",
                          f"calibration file not found: {path}")]
    with fh:
        reader = csv.DictReader(fh)
        missing = [c for c in CAL_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            return [], [Issue("error", "missing_columns", "calibration.csv",
                              f"missing required columns: {missing}")]
        for line_no, raw in enumerate(reader, start=2):
            level_id = (raw.get("level_id") or "").strip()
            batch = (raw.get("measurement_batch") or "").strip()
            bad = False
            if not level_id:
                issues.append(Issue("error", "missing_value", "calibration.level_id",
                                    "level_id is missing", line_no))
                bad = True
            if not batch:
                issues.append(Issue("error", "missing_value", "calibration.measurement_batch",
                                    "measurement_batch is missing; batch is required to "
                                    "keep repeat structure", line_no))
                bad = True
            ref = _as_float(raw.get("reference_value"), "calibration.reference_value",
                            line_no, issues, " (reference_unit)")
            resp = _as_float(raw.get("response"), "calibration.response",
                             line_no, issues, " (response_unit)")
            if bad or ref is None or resp is None:
                continue
            rows.append(CalRow(level_id, ref, batch, resp, line_no))

    # Level -> reference value must be consistent.
    level_ref: dict[str, float] = {}
    for row in rows:
        prev = level_ref.get(row.level_id)
        if prev is None:
            level_ref[row.level_id] = row.reference_value
        elif not math.isclose(prev, row.reference_value, rel_tol=0, abs_tol=1e-9):
            issues.append(Issue("error", "inconsistent_level_reference", f"level {row.level_id}",
                                f"reference_value {row.reference_value} conflicts with "
                                f"{prev} for the same level_id", row.line))
    # Duplicate lines (same level, batch, response) are legitimate replicates.
    return rows, issues


def parse_samples(path: str) -> tuple[list[SampleRow], list[Issue]]:
    issues: list[Issue] = []
    rows: list[SampleRow] = []
    try:
        fh = open(path, "r", encoding="utf-8", newline="")
    except FileNotFoundError:
        return [], [Issue("error", "file_missing", "samples.csv",
                          f"samples file not found: {path}")]
    with fh:
        reader = csv.DictReader(fh)
        missing = [c for c in SAMPLE_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            return [], [Issue("error", "missing_columns", "samples.csv",
                              f"missing required columns: {missing}")]
        seen_repeat: set[tuple[str, str]] = set()
        for line_no, raw in enumerate(reader, start=2):
            sid = (raw.get("sample_id") or "").strip()
            rid = (raw.get("repeat_id") or "").strip()
            bad = False
            if not sid:
                issues.append(Issue("error", "missing_value", "samples.sample_id",
                                    "sample_id is missing", line_no))
                bad = True
            if not rid:
                issues.append(Issue("error", "missing_value", "samples.repeat_id",
                                    "repeat_id is missing", line_no))
                bad = True
            resp = _as_float(raw.get("response"), "samples.response",
                             line_no, issues, " (response_unit)")
            if bad or resp is None:
                continue
            key = (sid, rid)
            if key in seen_repeat:
                issues.append(Issue("error", "duplicate_repeat",
                                    f"sample {sid} repeat {rid}",
                                    "repeat_id is duplicated; refusing to treat a "
                                    "copied row as an independent replicate", line_no))
                continue
            seen_repeat.add(key)
            rows.append(SampleRow(sid, rid, resp, line_no))
    return rows, issues


def load_inputs(cal_path: str, samples_path: str, plan_path: str) -> InputBundle:
    plan, plan_issues = load_plan(plan_path)
    issues: list[Issue] = list(plan_issues)
    if plan:
        issues.extend(validate_plan(plan))
    cal_rows, cal_issues = parse_calibration(cal_path)
    issues.extend(cal_issues)
    sample_rows, sample_issues = parse_samples(samples_path)
    issues.extend(sample_issues)

    # Cross-check: combination sample ids must exist among samples.
    if isinstance(plan, dict) and isinstance(plan.get("combinations"), list):
        sample_ids = {r.sample_id for r in sample_rows}
        for idx, combo in enumerate(plan["combinations"]):
            if not isinstance(combo, dict) or not isinstance(combo.get("terms"), dict):
                continue
            for sid in combo["terms"]:
                if sid not in sample_ids:
                    issues.append(Issue("error", "unknown_sample",
                                        f"plan.combinations[{idx}]",
                                        f"term references sample {sid!r} which is not "
                                        f"present in samples.csv"))
    return InputBundle(plan=plan, cal_rows=cal_rows, sample_rows=sample_rows,
                       issues=issues)
