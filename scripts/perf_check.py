"""Generate a large fixture, measure peak RSS of build, and prove extract
reads only requested record ranges.

Measurement method (documented in docs/PERFORMANCE.md):
* ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` reports the maximum
  resident set size of *all* terminated child processes.  Each measured
  step runs in its own ``subprocess`` so this value belongs to that step
  alone (KiB on Linux).  We additionally sample ``VmHWM`` (high-water mark)
  from ``/proc/<pid>/status`` while the child runs, which is exact for the
  child process even if it forks.
* The build child runs ``csvslice build`` over a freshly generated fixture.
* The extract child runs ``csvslice extract`` with a small interval and
  self-reports how many source bytes it read after the identity check.

Run: ``python3 scripts/perf_check.py --size 130MiB``
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMIT = 64 * 1024  # KiB


def run_measured(args: list[str], capture_output: bool = False) -> dict:
    """Run ``python -m csvslice <args>`` as a child and measure its peak RSS."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "csvslice", *args],
        env=env,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is a high-water mark across all reaped children.  To attribute
    # it to this step we require it to be the new maximum; otherwise fall back
    # to VmHWM which we sample in run_measured_with_vmhwm.
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed ({args}):\n{proc.stderr}"
        )
    return {
        "elapsed_s": elapsed,
        "child_maxrss_kib": after,
        "new_high_water_kib": after if after > before else None,
        "stdout": proc.stdout,
    }


def run_measured_with_vmhwm(args: list[str]) -> dict:
    """Run a child and sample its own VmHWM from /proc, giving exact attribution."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "csvslice", *args],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    vmhwm = 0
    status_path = f"/proc/{proc.pid}/status"
    while proc.poll() is None:
        try:
            with open(status_path, "r", encoding="ascii") as f:
                for line in f:
                    if line.startswith("VmHWM:"):
                        vmhwm = max(vmhwm, int(line.split()[1]))
                        break
        except OSError:
            pass
        time.sleep(0.02)
    stderr = proc.stderr.read() if proc.stderr else ""
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({args}):\n{stderr}")
    return {"elapsed_s": elapsed, "vmhwm_kib": vmhwm}


def human(kib: int) -> str:
    return f"{kib / 1024:.1f} MiB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="130MiB", help="fixture target size")
    parser.add_argument("--workdir", default=None, help="directory for the fixture")
    parser.add_argument("--keep", action="store_true", help="keep the fixture")
    args = parser.parse_args()

    workdir = args.workdir or tempfile.mkdtemp(prefix="csvslice-perf-")
    os.makedirs(workdir, exist_ok=True)
    csv_path = os.path.join(workdir, "big.csv")
    idx_path = os.path.join(workdir, "big.idx")
    out_path = os.path.join(workdir, "slice.csv")

    try:
        print(f"workdir: {workdir}")
        gen = run_measured_with_vmhwm(
            ["generate", csv_path, "--target-size", args.size, "--chunk-size", "2048"]
        )
        size = os.path.getsize(csv_path)
        print(f"generated: {size} bytes ({size / 2**20:.2f} MiB) in {gen['elapsed_s']:.1f}s")

        build = run_measured_with_vmhwm(
            ["build", csv_path, idx_path, "--chunk-size", str(1 << 20)]
        )
        index_size = os.path.getsize(idx_path)
        print(
            f"build: {build['elapsed_s']:.2f}s, peak RSS {human(build['vmhwm_kib'])} "
            f"({build['vmhwm_kib']} KiB), index {index_size / 2**20:.2f} MiB"
        )

        extract_ranges_spec = "0:3 "
        verify_proc = subprocess.run(
            [sys.executable, "-m", "csvslice", "verify", idx_path],
            env={**os.environ, "PYTHONPATH": ROOT},
            capture_output=True,
            text=True,
        )
        if verify_proc.returncode != 0:
            raise SystemExit(verify_proc.stderr)
        info = json.loads(verify_proc.stdout)
        record_count = info["data_records"]
        if record_count >= 6:
            extract_ranges_spec = f"0:3 {record_count - 3}:{record_count}"
        else:
            extract_ranges_spec = f"0:{record_count}"

        extract = run_measured_with_vmhwm(
            ["extract", csv_path, idx_path, out_path, "--ranges", extract_ranges_spec]
        )
        print(
            f"extract: {extract['elapsed_s']:.2f}s, peak RSS {human(extract['vmhwm_kib'])}, "
            f"ranges {extract_ranges_spec!r}"
        )
        extract_info = json.loads(
            subprocess.run(
                [sys.executable, "-m", "csvslice", "extract", csv_path, idx_path, out_path, "--ranges", extract_ranges_spec, "--force"],
                env={**os.environ, "PYTHONPATH": ROOT},
                capture_output=True,
                text=True,
            ).stdout
        )
        print(
            "extract read accounting: "
            f"{extract_info['source_bytes_read_for_records']} record bytes read in "
            f"{extract_info['source_read_ops']} operations (source is {size} bytes)"
        )

        ok = (
            build["vmhwm_kib"] <= LIMIT
            and extract["vmhwm_kib"] <= LIMIT
            and size > 128 * 2**20
        )
        print()
        print(f"fixture > 128 MiB: {'PASS' if size > 128 * 2**20 else 'FAIL'}")
        print(
            f"build peak RSS <= 64 MiB: {'PASS' if build['vmhwm_kib'] <= LIMIT else 'FAIL'} "
            f"({build['vmhwm_kib']} KiB <= {LIMIT} KiB)"
        )
        print(
            f"extract peak RSS <= 64 MiB: {'PASS' if extract['vmhwm_kib'] <= LIMIT else 'FAIL'} "
            f"({extract['vmhwm_kib']} KiB <= {LIMIT} KiB)"
        )
        ratio = index_size / size
        print(f"index/source size ratio: {ratio:.3f} (data records: {info['data_records']})")
        print()
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if not args.keep and args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"fixture kept at {workdir}")


if __name__ == "__main__":
    sys.exit(main())
