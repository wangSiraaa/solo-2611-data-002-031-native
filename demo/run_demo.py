#!/usr/bin/env python3
"""Run real-PTY demo scenarios and audit each with the independent verifier.

This is a demo/orchestration harness only.  The assistant itself never reads
SQLite; the verifier is a separate process invoked here after each run.

Usage:
    .venv/bin/python demo/run_demo.py [--dir demo/records]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
FAULTS = (
    "drop_after_save_once",
    "reject_save_once",
    "conflict_before_save_once",
    "unknown_prompt_once",
    "query_unavailable_once",
)

SCENARIOS = [
    {
        "name": "01-happy-path",
        "title": "正常保存：逐条编辑、退出后回读一致",
        "faults": {},
        "expect": "applied",
    },
    {
        "name": "02-unchanged",
        "title": "无变化记录不保存",
        "faults": {},
        "csv": "demo/csv/unchanged.csv",
        "expect": "unchanged",
    },
    {
        "name": "03-old-name-mismatch",
        "title": "预期旧名称不符：停在查询，不编辑",
        "faults": {},
        "csv": "demo/csv/mismatch.csv",
        "expect": "unchanged",
        "plan_only": True,
    },
    {
        "name": "04-not-found",
        "title": "编码查不到：停在查询",
        "faults": {},
        "csv": "demo/csv/notfound.csv",
        "expect": "unchanged",
        "plan_only": True,
    },
    {
        "name": "05-conflict",
        "title": "修订冲突：重新查询并报告，不覆盖",
        "faults": {"conflict_before_save_once": True},
        "expect": "any",
    },
    {
        "name": "06-unknown-popup",
        "title": "未知弹窗：停止并保留现场，不盲按回车",
        "faults": {"unknown_prompt_once": True},
        "expect": "unchanged",
    },
    {
        "name": "07-reject-save",
        "title": "保存被业务规则拒绝：记录反馈后继续",
        "faults": {"reject_save_once": True},
        "expect": "any",
    },
    {
        "name": "08-query-unavailable",
        "title": "查询暂不可用：短暂重试后读取",
        "faults": {"query_unavailable_once": True},
        "expect": "applied",
    },
    {
        "name": "09-drop-after-save",
        "title": "成功提示前断线：重启终端按编码回读，仅确认当前值",
        "faults": {"drop_after_save_once": True},
        "expect": "applied",
    },
]


def write_faults(path: Path, overrides: dict) -> None:
    config = {name: False for name in FAULTS}
    config.update(overrides)
    path.write_text(json.dumps(config), encoding="utf-8")


def run(cmd: list[str], log: Path, env: dict | None = None) -> int:
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, check=False)
    log.write_text(
        "$ " + " ".join(cmd) + "\n\n" + result.stdout + "\n--- stderr ---\n" + result.stderr,
        encoding="utf-8",
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="demo/records")
    ap.add_argument("--only", default=None, help="只运行名称包含该子串的场景")
    args = ap.parse_args(argv)

    out_root = (ROOT / args.dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    index = []
    failures = 0

    for scenario in SCENARIOS:
        if args.only and args.only not in scenario["name"]:
            continue
        work = out_root / scenario["name"]
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        write_faults(work / "faults-run.json", scenario["faults"])
        csv = str(ROOT / scenario.get("csv", "fixtures/changes.csv"))
        extra = ["--plan-only"] if scenario.get("plan_only") else ["--yes"]
        assistant_cmd = [
            PY, "-m", "terminal_assistant",
            "--csv", csv,
            "--db", str(work / "legacy.sqlite"),
            "--faults", str(work / "faults-run.json"),
            "--app", str(ROOT / "fixtures" / "legacy_app.py"),
            "--report", str(work / "report.json"),
            "--raw-dir", str(work / "raw"),
            "--settle", "0.15",
            *extra,
        ]
        assistant_rc = run(assistant_cmd, work / "assistant.log")

        verifier_cmd = [
            PY, str(ROOT / "tools" / "verify_db.py"),
            "--db", str(work / "legacy.sqlite"),
            "--csv", csv,
            "--expect", scenario.get("expect", "any"),
            "--json", str(work / "verify.json"),
        ]
        verifier_rc = run(verifier_cmd, work / "verifier.log")

        report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        verify = json.loads((work / "verify.json").read_text(encoding="utf-8")) \
            if (work / "verify.json").exists() else {}
        counts = report.get("counts", {})
        statuses = {item["code"]: item["status"] for item in report.get("items", [])}
        ok = assistant_rc == 0 and verifier_rc in (0, 1)
        if verifier_rc == 1 and not scenario.get("allow_partial"):
            ok = False
        if not ok:
            failures += 1
        index.append({
            "name": scenario["name"],
            "title": scenario["title"],
            "assistant_exit": assistant_rc,
            "verifier_exit": verifier_rc,
            "statuses": statuses,
            "counts": counts,
            "verifier_ok": verify.get("expectation_ok"),
            "history_total": verify.get("history_total"),
        })
        print(f"[{'OK' if ok else 'FAIL'}] {scenario['name']}: {scenario['title']}")
        print(f"      statuses={statuses} assistant_rc={assistant_rc} verifier_rc={verifier_rc}")

    (out_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n记录目录: {out_root}")
    print(f"失败场景: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
