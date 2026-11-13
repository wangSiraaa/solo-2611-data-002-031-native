"""Cross-check the report_helper with the site's independent truth export.

The helper never calls this script or the truth endpoint during collection;
verification is a separate, post-hoc step:

    python tools/verify_reports.py --truth http://HOST:PORT/__truth.csv \
        --state-dir .runtime/state --summary-csv .runtime/report_summary.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_truth(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=15) as r:
            text = r.read().decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    truth = {}
    for row in csv.DictReader(io.StringIO(text)):
        truth[(row["org_label"], row["month_label"], row["type_label"])] = row
    return truth


def find_states(state_dir: Path) -> list[dict]:
    out = []
    for p in sorted(state_dir.glob("*/state.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def verify(state_dir: Path, truth: dict) -> tuple[list[dict], bool]:
    rows = []
    all_ok = True
    for s in find_states(state_dir):
        key = (s["org_label"], s["month_label"], s["type_label"])
        exp = truth.get(key)
        problems = []
        if s.get("save_state") != "completed":
            problems.append(f"save_state={s.get('save_state')}")
        elif exp is None:
            problems.append("no truth row for selection")
        else:
            report = state_dir / _safe(s["task"]) / (s.get("final_file") or "")
            if not s.get("final_file") or not report.exists():
                problems.append("completed state but no readable file")
            else:
                with report.open(encoding="utf-8") as fh:
                    data = list(csv.reader(fh))
                n = len(data) - 1
                total = sum(float(r[-1].replace(",", "")) for r in data[1:])
                if n != int(exp["row_count"]):
                    problems.append(f"rows {n} != truth {exp['row_count']}")
                if f"{total:.2f}" != exp["total"]:
                    problems.append(f"total {total:.2f} != truth {exp['total']}")
                if s.get("csv_total") != exp["total"]:
                    problems.append(
                        f"state total {s.get('csv_total')} != truth {exp['total']}")
                if not s.get("qid"):
                    problems.append("no query id recorded")
        ok = not problems
        all_ok = all_ok and ok
        rows.append({"task": s["task"], "ok": ok,
                     "org": s["org_label"], "month": s["month_label"],
                     "type": s["type_label"],
                     "truth_rows": exp["row_count"] if exp else "",
                     "truth_total": exp["total"] if exp else "",
                     "report_rows": s.get("csv_row_count"),
                     "report_total": s.get("csv_total"),
                     "qid": s.get("qid"), "detail": "; ".join(problems)})
    return rows, all_ok


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:60]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True,
                    help="URL or path to the site's independent truth export")
    ap.add_argument("--state-dir", default=".runtime/state")
    ap.add_argument("--summary-csv", default=None)
    args = ap.parse_args(argv)
    truth = load_truth(args.truth)
    rows, ok = verify(Path(args.state_dir), truth)
    print(f"{'task':<14}{'ok':<4}{'truth(rows,total)':<26}"
          f"{'report(rows,total)':<26}detail")
    for r in rows:
        mark = "ok" if r["ok"] else "FAIL"
        print(f"{r['task']:<14}{mark:<4}{str(r['truth_rows'])+','+str(r['truth_total']):<26}"
              f"{str(r['report_rows'])+','+str(r['report_total']):<26}{r['detail']}")
    if args.summary_csv:
        with open(args.summary_csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                               ["task", "ok"])
            w.writeheader()
            w.writerows(rows)
    print(f"\n{sum(1 for r in rows if r['ok'])}/{len(rows)} verified")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
