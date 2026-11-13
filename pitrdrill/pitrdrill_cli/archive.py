"""WAL 归档管理: archive_command 推送端、归档索引、归档集合与完整性检查。

归档目录中每个 WAL 段附带 .sha256 sidecar, 并在 archive-index.jsonl 中
记录 (文件名, 大小, sha256, 时间线, 逻辑段号, 入库时间), 供:
  - 截断/篡改识别 (大小必须等于 wal_segment_size, 哈希可复核, pg_waldump 可读)
  - 缺口识别 (按逻辑段号连续)
  - 错误时间线识别
归档集合(archive-sets/)是归档目录某一时刻的快照视图, 演练期间允许继续增长,
预检阶段可在受限预算内显式等待新文件。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from .config import DrillConfig
from .state import Store
from .util import (DrillError, append_jsonl, hardlink_or_copy, now_iso,
                   parse_segment_name, read_jsonl, robust_write_text, run,
                   sha256_file, segment_name)


def archive_push(cfg: DrillConfig, wal_path: str, wal_name: str) -> int:
    """archive_command 入口: 幂等复制 WAL/history 到归档目录并写索引。

    成功返回 0; 目标已存在且内容一致也返回 0 (archive_command 重试安全)。
    """
    src = Path(wal_path)
    if not src.is_absolute() and not src.exists() and os.environ.get("PGDATA"):
        candidate = Path(os.environ["PGDATA"]) / wal_path
        if candidate.exists():
            src = candidate
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    dst = cfg.archive_dir / wal_name

    if not src.exists():
        print(f"[pitrdrill-archive] 源文件不存在: {src}")
        return 1

    if dst.exists():
        if dst.stat().st_size == src.stat().st_size and \
                sha256_file(dst) == sha256_file(src):
            print(f"[pitrdrill-archive] 已归档且一致, 幂等成功: {wal_name}")
            return 0
        print(f"[pitrdrill-archive] 同名文件内容冲突, 拒绝覆盖: {wal_name}")
        return 1

    tmp = dst.with_suffix(".part")
    shutil.copy2(src, tmp)
    digest = sha256_file(tmp)
    tmp.replace(dst)

    sidecar = dst.with_name(dst.name + ".sha256")
    robust_write_text(sidecar, f"{digest}  {wal_name}\n")

    record = {
        "ts": now_iso(), "file": wal_name,
        "size": dst.stat().st_size, "sha256": digest,
    }
    try:
        tli, seg_no = parse_segment_name(wal_name)
        record["timeline"] = tli
        record["seg_no"] = seg_no
    except DrillError:
        record["timeline"] = None
        record["seg_no"] = None
        record["history"] = wal_name.endswith(".history")
    append_jsonl(cfg.archive_index, record)
    print(f"[pitrdrill-archive] 已归档 {wal_name} ({dst.stat().st_size} 字节)")
    return 0


def list_archived_segments(cfg: DrillConfig) -> list[str]:
    rows = read_jsonl(cfg.archive_index)
    return sorted(r["file"] for r in rows if not r["file"].endswith(".history"))


def archive_index_map(cfg: DrillConfig) -> dict[str, dict]:
    return {r["file"]: r for r in read_jsonl(cfg.archive_index)}


def wait_for_segment(cfg: DrillConfig, wal_name: str, budget: float,
                     *, deadline_extend_hint: bool = False) -> bool:
    """在预算内显式等待某归档文件出现(归档集合/归档目录允许继续增长)。"""
    deadline = time.time() + budget
    while time.time() < deadline:
        if (cfg.archive_dir / wal_name).exists():
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 归档集合
def build_archive_set(store: Store, set_name: str, *,
                      include: list[str] | None = None,
                      exclude: list[str] | None = None,
                      from_archive: bool = True) -> dict:
    """从归档目录建立归档集合快照(硬链接, 不复制字节)。

    include/exclude 使用 WAL 段名精确匹配; 默认包含全部已归档段与 history。
    """
    cfg = store.cfg
    sdir = cfg.set_dir(set_name)
    sdir.mkdir(parents=True, exist_ok=True)
    idx = archive_index_map(cfg)
    chosen: list[str] = []
    for name in sorted(idx):
        if include is not None and name not in include:
            continue
        if exclude and name in exclude:
            continue
        src = cfg.archive_dir / name
        if not src.exists():
            continue
        hardlink_or_copy(src, sdir / name)
        if (cfg.archive_dir / (name + ".sha256")).exists():
            hardlink_or_copy(cfg.archive_dir / (name + ".sha256"),
                             sdir / (name + ".sha256"))
        chosen.append(name)

    manifest = {
        "set": set_name, "created_at": now_iso(),
        "source_archive": str(cfg.archive_dir),
        "files": chosen,
        "excluded": exclude or [],
        "index": {n: {k: idx[n][k] for k in ("size", "sha256", "timeline", "seg_no")
                      if k in idx[n]} for n in chosen},
    }
    robust_write_text(
        sdir / "set-manifest.json",
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2))
    store.state.archive_sets[set_name] = {
        "path": str(sdir), "created_at": manifest["created_at"],
        "files": chosen, "excluded": exclude or [],
    }
    store.save()
    return manifest


def set_segment_names(cfg: DrillConfig, set_name: str) -> list[str]:
    sdir = cfg.set_dir(set_name)
    if not sdir.exists():
        raise DrillError(f"归档集合不存在: {set_name}")
    return sorted(p.name for p in sdir.iterdir()
                  if len(p.name) == 24 and not p.name.endswith(".sha256"))


def set_history_files(cfg: DrillConfig, set_name: str) -> list[str]:
    sdir = cfg.set_dir(set_name)
    if not sdir.exists():
        return []
    return sorted(p.name for p in sdir.iterdir() if p.name.endswith(".history"))


# ---------------------------------------------------------------- 完整性检查
def verify_segment_bytes(cfg: DrillConfig, path: Path, expected_size: int,
                         expected_sha256: str | None = None) -> list[str]:
    """返回问题清单; 空清单表示通过。识别截断/篡改。"""
    problems: list[str] = []
    if not path.exists():
        return [f"文件缺失: {path.name}"]
    size = path.stat().st_size
    if size != expected_size:
        problems.append(
            f"{path.name}: 大小异常 {size} != 预期 {expected_size}(疑似截断/损坏)")
    if expected_sha256:
        actual = sha256_file(path)
        if actual != expected_sha256:
            problems.append(f"{path.name}: sha256 不匹配(疑似篡改/写入不完整)")
    else:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if not h.hexdigest():
            problems.append(f"{path.name}: 无法读取")
    # pg_waldump 可读性: 截断段会在末尾报错, 但仍可能部分可读
    r = run([cfg.pgbin / "pg_waldump", path], timeout=30)
    if r.rc != 0 and "invalid magic" in r.stderr.lower():
        problems.append(f"{path.name}: pg_waldump 报告 invalid magic(非本数据库WAL/损坏)")
    elif r.rc != 0 and size != expected_size:
        problems.append(f"{path.name}: pg_waldump 解析报错且大小异常: "
                        f"{r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'rc!=0'}")
    return problems


def waldump_timeline_evidence(cfg: DrillConfig, path: Path) -> str:
    r = run([cfg.pgbin / "pg_waldump", path], timeout=30)
    # 取首行(含段内首个记录的 TLI)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else f"rc={r.rc} {r.stderr.strip()[:200]}"


def gap_analysis(required: list[str], available: list[str],
                 backup_segments: list[str]) -> dict:
    """比对所需段与可用来源(归档集合 + 基础备份自带 pg_wal)。

    基础备份可能包含其完成所需的 WAL, 不能仅凭"未在外部目录出现"判不可恢复。
    """
    avail = set(available) | set(backup_segments)
    missing = [s for s in required if s not in avail]
    missing_external_only = [s for s in required if s not in set(available)]
    covered_by_backup = sorted(set(missing_external_only) & set(backup_segments))
    return {
        "required": required,
        "available_in_set": sorted(set(available)),
        "present_in_backup_pgwal": sorted(set(backup_segments)),
        "covered_by_backup": covered_by_backup,
        "missing": missing,
        "complete": not missing,
    }


def timeline_check(required_timeline: int, set_names: list[str]) -> list[str]:
    problems = []
    for n in set_names:
        tli, _ = parse_segment_name(n)
        if tli != required_timeline:
            problems.append(
                f"{n}: 时间线 {tli} 与要求的 {required_timeline} 不符(错误时间线)")
    return problems


def next_segment_name(name: str) -> str:
    tli, seg_no = parse_segment_name(name)
    return segment_name(seg_no + 1, tli)
