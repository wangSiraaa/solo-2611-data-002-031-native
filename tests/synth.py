"""Small synthetic fixtures for tests, constructed so answers are hand-checkable.

These are generated once at import time into temporary files; they are NOT
derived from the provided fixtures and contain no expected-answer table.
"""
from __future__ import annotations

import csv
import json
import os


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def linear_plan(path, shared_sd=0.0, seed=123,
               combinations=None, reference_unit="mg/L",
               response_unit="mV", degrees=(1, 2), working_range=(0, 20)):
    plan = {
        "reference_unit": reference_unit,
        "response_unit": response_unit,
        "reference_shared_offset_sd": shared_sd,
        "shared_error_id": "test-cert",
        "working_range": list(working_range),
        "candidate_degrees": list(degrees),
        "seed": seed,
        "combinations": combinations or [],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)


def linear_calibration(path):
    """y = 3 + 2x exactly, 4 levels, 2 repeats each (batches B1/B2)."""
    rows = []
    for i, x in enumerate([0, 5, 10, 15]):
        for k in range(2):
            rows.append([f"L{i}", x, f"B{k+1}", 3 + 2 * x])
    _write_csv(path, ["level_id", "reference_value", "measurement_batch", "response"],
               rows)


def linear_samples(path, truths):
    """Sample responses exact for y=3+2x; two repeats equal to avoid noise."""
    rows = []
    for sid, c in truths.items():
        for k in range(2):
            rows.append([sid, k + 1, 3 + 2 * c])
    _write_csv(path, ["sample_id", "repeat_id", "response"], rows)


def curved_calibration(path):
    """y = 10 + 5x + 0.5x^2 exactly on [0,20], 5 levels, 2 repeats each."""
    rows = []
    for i, x in enumerate([0, 5, 10, 15, 20]):
        for k in range(2):
            rows.append([f"L{i}", x, f"B{k+1}", 10 + 5 * x + 0.5 * x * x])
    _write_csv(path, ["level_id", "reference_value", "measurement_batch", "response"],
               rows)


def curved_samples(path, truths):
    rows = []
    for sid, c in truths.items():
        for k in range(2):
            rows.append([sid, k + 1, 10 + 5 * c + 0.5 * c * c])
    _write_csv(path, ["sample_id", "repeat_id", "response"], rows)


def nonmonotone_calibration(path):
    """y = 100 + 10x - 0.4x^2 on [0,30]: increasing then decreasing, vertex x=12.5."""
    rows = []
    for i, x in enumerate([0, 10, 20, 30]):
        for k in range(2):
            rows.append([f"L{i}", x, f"B{k+1}", 100 + 10 * x - 0.4 * x * x])
    _write_csv(path, ["level_id", "reference_value", "measurement_batch", "response"],
               rows)


def flat_calibration(path):
    """y = 5 constant: near-zero slope, must refuse inversion."""
    rows = []
    for i, x in enumerate([0, 5, 10, 15]):
        for k in range(2):
            rows.append([f"L{i}", x, f"B{k+1}", 5.0])
    _write_csv(path, ["level_id", "reference_value", "measurement_batch", "response"],
               rows)


def too_few_levels_calibration(path):
    """Only 3 levels: degree 2 must be ineligible (needs 4), degree 1 eligible
    (needs 3 for grouped leave-one-level-out with residual dof)."""
    rows = []
    for i, x in enumerate([0, 5, 10]):
        for k in range(2):
            rows.append([f"L{i}", x, f"B{k+1}", 2 + 3 * x])
    _write_csv(path, ["level_id", "reference_value", "measurement_batch", "response"],
               rows)
