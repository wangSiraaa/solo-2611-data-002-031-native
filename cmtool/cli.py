"""命令行：compare / apply-decisions / verify。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import report
from .decisions import apply_decisions, check_binding
from .migrate import run_migration
from .model import digest_json, load_comments, load_document, load_json


def _load_inputs(args):
    old_doc = load_document(args.old)
    new_doc = load_document(args.new)
    comments = load_comments(args.comments)
    return old_doc, new_doc, comments


def _write_outputs(out_dir, old_doc, new_doc, migrated, unresolved, extra=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = {"old_digest": old_doc.digest, "new_digest": new_doc.digest}
    (out / "migrated.json").write_text(
        json.dumps({**base, "migrated": migrated}, ensure_ascii=False,
                   indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out / "unresolved.json").write_text(
        json.dumps({**base, "unresolved": unresolved}, ensure_ascii=False,
                   indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out / "report.html").write_text(
        report.render(old_doc, new_doc, migrated, unresolved), encoding="utf-8"
    )
    summary = {
        **base,
        "comments_total": len(migrated) + len(unresolved),
        "migrated": len(migrated),
        "unresolved": len(unresolved),
    }
    if extra:
        summary.update(extra)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summary


def _print_summary(summary, out_dir):
    print(f"old 摘要: {summary['old_digest']}")
    print(f"new 摘要: {summary['new_digest']}")
    print(f"批注总数 {summary['comments_total']}："
          f"已迁移 {summary['migrated']}，待人工 {summary['unresolved']}")
    print(f"输出目录: {out_dir}（migrated.json / unresolved.json / report.html / summary.json）")


def cmd_compare(args) -> int:
    old_doc, new_doc, comments = _load_inputs(args)
    result = run_migration(old_doc, new_doc, comments)
    summary = _write_outputs(args.out, old_doc, new_doc,
                             result["migrated"], result["unresolved"])
    _print_summary(summary, args.out)
    for u in result["unresolved"]:
        print(f"  待处理 {u['comment_id']}: {u['reason']} — {u['detail']}")
    if result["unresolved"]:
        print("存在待人工处理项：可参考 summary.json 中的摘要编写 decisions.json，"
              "再运行 apply-decisions。")
    return 0


def cmd_apply_decisions(args) -> int:
    old_doc, new_doc, comments = _load_inputs(args)
    result = run_migration(old_doc, new_doc, comments)
    decisions_obj = load_json(args.decisions)
    comments_by_id = {c.cid: c for c in comments}

    problems = check_binding(decisions_obj, old_doc, new_doc)
    if problems:
        report_obj = {
            "binding_ok": False,
            "problems": problems,
            "applied": [],
            "rejected": [],
        }
        # 源文已变化：拒绝沿用旧决定，未解决项保持原样
        summary = _write_outputs(args.out, old_doc, new_doc,
                                 result["migrated"], result["unresolved"],
                                 extra={"decisions": "rejected_stale"})
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "decisions_report.json").write_text(
            json.dumps(report_obj, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _print_summary(summary, args.out)
        for p in problems:
            print(f"拒绝沿用旧决定：{p}", file=sys.stderr)
        return 1

    applied, rejected, remaining = apply_decisions(
        decisions_obj, old_doc, new_doc, comments_by_id,
        result["migrated"], result["unresolved"],
    )
    migrated = sorted(result["migrated"] + applied, key=lambda m: m["comment_id"])
    summary = _write_outputs(args.out, old_doc, new_doc, migrated, remaining,
                             extra={"decisions_applied": len(applied),
                                    "decisions_rejected": len(rejected)})
    report_obj = {
        "binding_ok": True,
        "decisions_digest": digest_json(decisions_obj),
        "applied": [d["comment_id"] for d in applied],
        "rejected": rejected,
    }
    (Path(args.out) / "decisions_report.json").write_text(
        json.dumps(report_obj, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _print_summary(summary, args.out)
    for a in applied:
        print(f"  已按决定迁移 {a['comment_id']} -> {a['paragraph_id']} "
              f"[{a['start']},{a['end']})")
    for r in rejected:
        print(f"  决定被拒绝 {r.get('comment_id')}: {r['reason']}", file=sys.stderr)
    return 1 if rejected else 0


def cmd_verify(args) -> int:
    """复核输出：每条旧批注有且仅有一个去向；已确认锚点逐条重新截取核对。"""
    old_doc, new_doc, comments = _load_inputs(args)
    out = Path(args.out)
    problems = []
    try:
        mig_obj = load_json(out / "migrated.json")
        unr_obj = load_json(out / "unresolved.json")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取输出文件：{exc}", file=sys.stderr)
        return 1
    if mig_obj.get("old_digest") != old_doc.digest or \
       mig_obj.get("new_digest") != new_doc.digest:
        problems.append("migrated.json 的源文摘要与当前输入不一致（输出已过期）")
    if unr_obj.get("old_digest") != old_doc.digest or \
       unr_obj.get("new_digest") != new_doc.digest:
        problems.append("unresolved.json 的源文摘要与当前输入不一致（输出已过期）")

    migrated = mig_obj.get("migrated", [])
    unresolved = unr_obj.get("unresolved", [])
    seen = {}
    for m in migrated:
        cid = m.get("comment_id")
        if cid in seen:
            problems.append(f"{cid} 出现多次")
        seen[cid] = "migrated"
        para = new_doc.by_id.get(m.get("paragraph_id"))
        if para is None:
            problems.append(f"{cid}: 新版本中不存在段落 {m.get('paragraph_id')}")
            continue
        s, e = m.get("start"), m.get("end")
        if not (isinstance(s, int) and isinstance(e, int)
                and 0 <= s <= e <= len(para.text)):
            problems.append(f"{cid}: 范围 [{s},{e}) 非法")
            continue
        sliced = para.text[s:e]
        if sliced != m.get("quote"):
            problems.append(
                f"{cid}: 锚点复核失败，切片 {sliced!r} 与记录 quote {m.get('quote')!r} 不符"
            )
    for u in unresolved:
        cid = u.get("comment_id")
        if cid in seen:
            problems.append(f"{cid} 同时出现在 migrated 与 unresolved")
        seen[cid] = "unresolved"
        if not u.get("reason"):
            problems.append(f"{cid}: 未解决项缺少原因")
    for c in comments:
        if c.cid not in seen:
            problems.append(f"{c.cid} 没有任何去向（既未迁移也未列为未解决）")
    extra = [cid for cid in seen if cid not in {c.cid for c in comments}]
    for cid in extra:
        problems.append(f"输出中出现输入里不存在的批注 {cid}")

    if problems:
        print(f"复核未通过（{len(problems)} 个问题）：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"复核通过：{len(migrated)} 条已迁移锚点均与新文一致，"
          f"{len(unresolved)} 条未解决项均有明确原因，无批注丢失。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cmtool",
        description="离线批注迁移工具：旧版本批注 -> 新版本文档",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p):
        p.add_argument("--old", required=True, help="旧版本文档 JSON")
        p.add_argument("--new", required=True, help="新版本文档 JSON")
        p.add_argument("--comments", required=True, help="旧批注 JSON")
        p.add_argument("--out", required=True, help="输出目录")

    p_compare = sub.add_parser("compare", help="比较两版本并尝试自动迁移")
    _common(p_compare)
    p_compare.set_defaults(func=cmd_compare)

    p_apply = sub.add_parser("apply-decisions", help="应用人工决定后重新导出")
    _common(p_apply)
    p_apply.add_argument("--decisions", required=True, help="人工决定 JSON")
    p_apply.set_defaults(func=cmd_apply_decisions)

    p_verify = sub.add_parser("verify", help="复核输出完整性与锚点正确性")
    _common(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
