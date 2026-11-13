"""End-to-end CLI and artifact tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest


def _run(args, cwd):
    return subprocess.run([sys.executable, "-m", "calibration_tool", *args],
                          cwd=cwd, capture_output=True, text=True)


def test_cli_run_writes_artifacts():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as out:
        r = _run(["run", "--out", out], repo)
        assert r.returncode == 0, r.stderr
        for name in ("model.json", "apply.json", "compare.json",
                     "report.html", "report.md", "result.json"):
            assert os.path.exists(os.path.join(out, name))
        model = json.load(open(os.path.join(out, "model.json")))
        assert model["schema"] == "calibration-model/1"
        assert model["ok"] is True
        assert model["selection"]["selected_degree"] == 2
        # Model must be sufficient to recompute: coefficients + covariance.
        assert model["model"]["coefficient_covariance"] is not None
        assert len(model["calibration_readings"]) == 24
        result = json.load(open(os.path.join(out, "result.json")))
        assert result["ok"] is True


def test_cli_illegal_unit_exit_code():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as tmp:
        plan = json.load(open(os.path.join(repo, "fixtures", "plan.json")))
        plan["reference_unit"] = "stone"
        pp = os.path.join(tmp, "plan.json")
        json.dump(plan, open(pp, "w"))
        r = _run(["run", "--plan", pp, "--out", os.path.join(tmp, "out")], repo)
        assert r.returncode == 2
        # Itemised message should land on stderr.
        assert "unit_illegal" in r.stderr and "reference_unit" in r.stderr


def test_cli_out_of_range_marks_review():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as out:
        r = _run(["run", "--out", out], repo)
        assert r.returncode == 0
        app = json.load(open(os.path.join(out, "apply.json")))
        by = {s["sample_id"]: s for s in app["samples"]}
        assert by["S4"]["status"] == "out_of_range"
        assert by["S4"]["estimate"] is None
        for sid in ("S1", "S2", "S3"):
            assert by[sid]["status"] == "ok"
            assert by[sid]["interval"][0] <= by[sid]["estimate"] <= by[sid]["interval"][1]
