"""编排：清单核验 -> 两版切分 -> 对比，产出结构化报告。"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .compare import compare_revisions
from .manifest import load_manifest, read_and_verify
from .rules import load_ruleset
from .segment import segment_source


def segment_all(fixtures_dir: Path, rules_path: Path) -> dict:
    """核验全部冻结源并在一版规则下切分。"""
    manifest = load_manifest(Path(fixtures_dir))
    ruleset = load_ruleset(Path(rules_path))
    sources = []
    for spec in manifest.sources:
        raw, verification = read_and_verify(spec, manifest.root)
        record = {"path": spec.path, "format": spec.format,
                  "verification": verification}
        if not verification["ok"]:
            # 清单核验失败：源失败，绝不继续解析。
            record["status"] = "failed"
            record["error"] = verification.get("error", "verification failed")
        else:
            record.update(segment_source(raw, spec, ruleset))
            record["verification"] = verification
        sources.append(record)
    return {
        "tool": f"logseg {__version__}",
        "revision": ruleset.revision,
        "rules_path": str(rules_path),
        "max_candidate_lines": ruleset.max_candidate_lines,
        "sources": sources,
    }


def run_all(fixtures_dir: Path, rules_a: Path, rules_b: Path) -> dict:
    seg_a = segment_all(fixtures_dir, rules_a)
    seg_b = segment_all(fixtures_dir, rules_b)
    comparison = compare_revisions(seg_a, seg_b)
    return {"seg_a": seg_a, "seg_b": seg_b, "comparison": comparison}


def summarize(seg: dict) -> list[str]:
    """给人看的一行式摘要。"""
    lines = [f"revision {seg['revision']}:"]
    for src in seg["sources"]:
        if src.get("status") != "ok":
            lines.append(f"  {src['path']}: FAILED ({src.get('error')})")
            continue
        units = src.get("blocks") or src.get("objects") or []
        counts: dict[str, int] = {}
        for unit in units:
            counts[unit["status"]] = counts.get(unit["status"], 0) + 1
        status = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(
            f"  {src['path']}: {len(src['events'])} events ({status})"
        )
    return lines
