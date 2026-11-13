"""CLI entry point: run a task list, show stages, rerun only unfinished work."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from .config import ConfigError, ReportTask, load_config, load_tasks
from .engine import TaskRunner
from .state import StateStore, TaskState
from .trace import Trace, read_events

STAGE_LABELS = {
    "pending": "等待开始",
    "login": "登录",
    "org": "选择机构",
    "params": "报表参数",
    "preview": "预览",
    "download": "下载",
    "done": "完成",
    "failed": "失败",
}


def _fmt_task(t: ReportTask) -> str:
    return f"{t.org_label} / {t.month_label} / {t.type_label}"


def _status_line(state: TaskState) -> str:
    rows = f" rows={state.csv_row_count}" if state.csv_row_count else ""
    return (f"{state.task:<36} stage={STAGE_LABELS.get(state.stage, state.stage)}"
            f" save={state.save_state}{rows}")


def cmd_run(args) -> int:
    try:
        cfg = load_config(args.config)
        tasks = load_tasks(args.tasks)
    except (ConfigError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    store = StateStore(args.state_dir)
    trace = Trace(args.trace_dir, run_id)
    print(f"运行 {run_id}  站点 {cfg.base_url}  任务 {len(tasks)} 项")
    results = []
    for task in tasks:
        existing = store.load(task.name)
        if existing and existing.stage == "done" and \
                existing.save_state == "completed" and \
                existing.final_file and \
                (store.dir_for(task.name) / existing.final_file).exists() and \
                existing.org_label == task.org_label and \
                existing.month_label == task.month_label and \
                existing.type_label == task.type_label:
            print(f"[跳过] {_fmt_task(task)} 已完成")
            results.append(("skipped", existing))
            continue
        print(f"[执行] {_fmt_task(task)}")
        trace.event(task.name, "pending", "task_start",
                    org_label=task.org_label, month_label=task.month_label,
                    type_label=task.type_label)
        runner = TaskRunner(cfg, task, store, trace, log=print)
        state = runner.run()
        if state.stage == "done" and state.save_state == "completed":
            print(f"  [完成] 行数={state.csv_row_count} 合计={state.csv_total} "
                  f"文件={state.final_file}")
            results.append(("done", state))
        else:
            print(f"  [失败] {state.last_error}")
            results.append(("failed", state))
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
    trace.close()
    _write_summary(Path(args.trace_dir) / f"summary-{run_id}.json", results, trace)
    ok = sum(1 for s, _ in results if s in ("done", "skipped"))
    bad = len(results) - ok
    print(f"\n完成 {ok} 项，失败 {bad} 项。轨迹: {trace.path}")
    if args.summary_csv:
        _write_summary_csv(args.summary_csv, results)
    return 0 if bad == 0 else 1


def _write_summary(path: Path, results, trace):
    payload = {
        "run_id": trace.run_id,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tasks": [
            {"result": result, "state": state.__dict__}
            for result, state in results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _write_summary_csv(path: str, results):
    import csv
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["task", "result", "org", "month", "type", "stage",
                    "save_state", "qid", "rows", "preview_rows", "total",
                    "preview_total", "file", "recoveries", "last_error"])
        for result, s in results:
            w.writerow([s.task, result, s.org_label, s.month_label, s.type_label,
                        s.stage, s.save_state, s.qid, s.csv_row_count,
                        s.preview_row_count, s.csv_total, s.preview_total,
                        s.final_file, s.recoveries, s.last_error or ""])


def cmd_status(args) -> int:
    store = StateStore(args.state_dir)
    paths = sorted(Path(args.state_dir).glob("*/state.json"))
    if not paths:
        print("尚无可查看的任务状态。")
        return 0
    print(f"{'任务':<36} {'阶段':<6} {'保存状态':<16} {'行数':>5} {'恢复':>4}")
    done = []
    for p in paths:
        try:
            s = TaskState(**json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            continue
        print(f"{s.task[:35]:<36} {STAGE_LABELS.get(s.stage, s.stage):<8}"
              f" {s.save_state:<18} {str(s.csv_row_count or ''):>5}"
              f" {s.recoveries:>4}")
        if s.save_state == "completed":
            done.append(s.task)
    print(f"\n已完成内容核对 {len(done)} 项。")
    return 0


def cmd_trace(args) -> int:
    trace_file = Path(args.trace_dir) / "trace.jsonl"
    events = read_events(trace_file)
    if not events:
        print(f"没有轨迹文件: {trace_file}")
        return 1
    for ev in events:
        if args.task and ev.get("task") != args.task:
            continue
        if args.kind and ev.get("event") != args.kind:
            continue
        extra = {k: v for k, v in ev.items()
                 if k not in ("run_id", "task", "seq", "stage", "event", "ts")}
        print(f"{ev.get('ts','')} [{ev.get('task')}] {ev.get('stage')}"
              f" {ev.get('event')} {json.dumps(extra, ensure_ascii=False)}")
    return 0


def cmd_events_summary(args) -> int:
    events = read_events(Path(args.trace_dir) / "trace.jsonl")
    counter = Counter((e.get("stage"), e.get("event")) for e in events)
    for (stage, event), n in sorted(counter.items()):
        print(f"{stage:<10} {event:<22} {n}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="本地报表表单采集助手")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="执行全部任务（自动跳过已完成项）")
    r.add_argument("--config", required=True)
    r.add_argument("--tasks", required=True)
    r.add_argument("--state-dir", default=".runtime/state")
    r.add_argument("--trace-dir", default=".runtime/trace")
    r.add_argument("--summary-csv", default=None)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="查看任务当前阶段/成功列表")
    s.add_argument("--state-dir", default=".runtime/state")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("trace", help="打印执行轨迹")
    t.add_argument("--trace-dir", default=".runtime/trace")
    t.add_argument("--task", default=None)
    t.add_argument("--kind", default=None)
    t.set_defaults(func=cmd_trace)

    e = sub.add_parser("events", help="按阶段/事件统计轨迹")
    e.add_argument("--trace-dir", default=".runtime/trace")
    e.set_defaults(func=cmd_events_summary)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
