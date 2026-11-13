"""命令行入口。

子命令：
  verify   只核验清单与冻结源（长度 + SHA256）
  segment  用一版规则切分全部源，输出结构化切分与候选证据
  compare  对比两份切分结果，输出改版报告
  run      verify + 两版 segment + compare，一次产出全部文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compare import compare_revisions
from .manifest import load_manifest, read_and_verify
from .report import run_all, segment_all, summarize


def _dump(doc: dict, out: Path | None) -> None:
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False)
    if out is None:
        print(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.fixtures))
    results = []
    ok_all = True
    for spec in manifest.sources:
        _, verification = read_and_verify(spec, manifest.root)
        results.append({"path": spec.path, **verification})
        ok_all = ok_all and verification["ok"]
    _dump({"sources": results, "all_ok": ok_all}, args.out)
    return 0 if ok_all else 1


def cmd_segment(args: argparse.Namespace) -> int:
    seg = segment_all(Path(args.fixtures), Path(args.rules))
    _dump(seg, args.out)
    for line in summarize(seg):
        print(line, file=sys.stderr)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    with open(args.a, "r", encoding="utf-8") as fh:
        seg_a = json.load(fh)
    with open(args.b, "r", encoding="utf-8") as fh:
        seg_b = json.load(fh)
    _dump(compare_revisions(seg_a, seg_b), args.out)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    fixtures = Path(args.fixtures)
    rules_a = Path(args.rules_a) if args.rules_a else fixtures / "templates-a.json"
    rules_b = Path(args.rules_b) if args.rules_b else fixtures / "templates-b.json"
    report = run_all(fixtures, rules_a, rules_b)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _dump(report["seg_a"], out_dir / "seg-a.json")
    _dump(report["seg_b"], out_dir / "seg-b.json")
    _dump(report["comparison"], out_dir / "comparison.json")
    for line in summarize(report["seg_a"]) + summarize(report["seg_b"]):
        print(line, file=sys.stderr)
    failed = any(
        src.get("status") != "ok"
        for seg in (report["seg_a"], report["seg_b"])
        for src in seg["sources"]
    )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logseg",
        description="两版有界事件语法下的全局一致日志切分与改版对比",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="核验清单与冻结源")
    p_verify.add_argument("--fixtures", default="fixtures")
    p_verify.add_argument("--out", type=Path, default=None)
    p_verify.set_defaults(func=cmd_verify)

    p_seg = sub.add_parser("segment", help="用一版规则切分全部源")
    p_seg.add_argument("--fixtures", default="fixtures")
    p_seg.add_argument("--rules", required=True)
    p_seg.add_argument("--out", type=Path, default=None)
    p_seg.set_defaults(func=cmd_segment)

    p_cmp = sub.add_parser("compare", help="对比两份切分结果")
    p_cmp.add_argument("--a", required=True)
    p_cmp.add_argument("--b", required=True)
    p_cmp.add_argument("--out", type=Path, default=None)
    p_cmp.set_defaults(func=cmd_compare)

    p_run = sub.add_parser("run", help="核验 + 两版切分 + 对比")
    p_run.add_argument("--fixtures", default="fixtures")
    p_run.add_argument("--rules-a", default=None)
    p_run.add_argument("--rules-b", default=None)
    p_run.add_argument("--out", default="outputs")
    p_run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
