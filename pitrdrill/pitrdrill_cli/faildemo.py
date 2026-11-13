"""失败场景制造: 缺 WAL 段 / 错备份身份 / 错误时间线 / 截断归档。

所有破坏只作用于:
  - 隔离的归档集合副本(archive-sets/<demo>), 不动源归档目录
  - 隔离的备份副本(backups/<demo>-*), 不动真实基础备份
这样失败演练可重复, 且源库与有效归档始终完好。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .archive import build_archive_set, set_segment_names
from .state import Store
from .util import (DrillError, now_iso, robust_copytree, run, segment_range)


def _clone_backup(store: Store, src_name: str, dst_name: str) -> Path:
    cfg = store.cfg
    dst = cfg.backup_dir(dst_name)
    robust_copytree(cfg.backup_dir(src_name), dst)
    pidf = dst / "postmaster.pid"
    if pidf.exists():
        pidf.unlink()
    return dst


def make_gap_set(store: Store, set_name: str, backup_name: str,
                 target_name: str) -> dict:
    """缺 WAL 段: 从完整集合中删除目标之前的一个中间段。"""
    cfg = store.cfg
    backup = store.state.backups[backup_name]
    rp = next(r for r in store.state.restore_points if r["name"] == target_name)
    start_lsn = backup["checkpoint_lsn"] or backup["start_lsn"]
    required = segment_range(start_lsn, rp["lsn"],
                             cfg.wal_seg_size_bytes, rp["timeline"])
    # 优先选不在备份自带 pg_wal 中的段, 保证外部缺口不可被备份弥补
    backup_wal = set(backup.get("backup_wal_segments", []))
    candidates = [s for s in required if s not in backup_wal]
    if len(candidates) < 2:
        candidates = required
    victim = candidates[len(candidates) // 2]

    build_archive_set(store, set_name, exclude=[victim])
    # 硬链接集合中确认 victim 确实不存在
    remaining = set_segment_names(cfg, set_name)
    if victim in remaining:
        raise DrillError("缺口制造失败: 段仍存在于集合中")
    rec = {"demo": "gap", "set": set_name, "removed_segment": victim,
           "required": required, "created_at": now_iso()}
    store.state.failure_demos[set_name] = rec
    store.save()
    return rec


def make_wrong_identity_backup(store: Store, src_backup: str,
                               dst_backup: str) -> dict:
    """错备份身份: 复制备份后篡改 pg_control 中的 system identifier。

    使用 pg_resetwal 仅改身份会顺带清 WAL 状态, 因此这里直接对
    pg_control 做字节级演示性破坏并记录, 预检的 system identifier 比对会拒绝它。
    更稳妥的做法: 用另一个 initdb 集群的 pg_control 替换。
    """
    cfg = store.cfg
    dst = _clone_backup(store, src_backup, dst_backup)
    # 建立一个一次性"外来"集群, 取其 pg_control 代表另一个数据库;
    # 放在本机 /tmp 避开工作目录挂载点的瞬时属性抖动
    import tempfile
    foreign_root = Path(tempfile.mkdtemp(prefix="pitrdrill-foreign-"))
    foreign = foreign_root / "data"
    r = run([cfg.pgbin / "initdb", "-D", foreign, "-U", "postgres",
             "--auth-local=trust", "-E", "UTF8", "--locale=C"], check=False)
    if not r.ok:
        raise DrillError(f"外来集群 initdb 失败: {r.stderr[:200]}")
    foreign_control = foreign / "global" / "pg_control"
    target_control = dst / "global" / "pg_control"
    shutil.copy2(foreign_control, target_control)
    from .util import parse_controldata
    # 在删除临时集群前读出其身份(也可直接对已替换控制文件的 dst 读取)
    foreign_id = parse_controldata(
        run([cfg.pgbin / "pg_controldata", foreign]).stdout
    )["Database system identifier"]
    shutil.rmtree(foreign_root, ignore_errors=True)
    rec = {"demo": "wrong_identity", "backup": dst_backup,
           "foreign_system_identifier": foreign_id,
           "path": str(dst), "created_at": now_iso()}
    # 登记为一个(故意错误的)备份候选
    store.state.backups[dst_backup] = {
        "name": dst_backup, "note": "故障演练: 外来 pg_control(错身份)",
        "path": str(dst), "created_at": now_iso(),
        "backup_label": store.state.backups[src_backup].get("backup_label", {}),
        "start_lsn": store.state.backups[src_backup].get("start_lsn", ""),
        "checkpoint_lsn": store.state.backups[src_backup].get("checkpoint_lsn", ""),
        "backup_timeline": store.state.backups[src_backup]["backup_timeline"],
        "system_identifier": foreign_id,
        "backup_wal_segments": store.state.backups[src_backup].get(
            "backup_wal_segments", []),
        "manifest_sha256": None, "verifybackup_rc": -1,
        "verifybackup_output": "故意替换 pg_control, 预期校验失败",
        "corrupt": True,
    }
    store.state.failure_demos[dst_backup] = rec
    store.save()
    return rec


def make_wrong_timeline_set(store: Store, set_name: str) -> dict:
    """错误时间线: 构造一个集合, 把其中段名重命名为时间线 2(内容仍是 TLI1)。"""
    cfg = store.cfg
    build_archive_set(store, set_name)
    sdir = cfg.set_dir(set_name)
    renamed = []
    for p in list(sdir.iterdir()):
        if len(p.name) == 24 and p.name.startswith("00000001"):
            new_name = "00000002" + p.name[8:]
            target = sdir / new_name
            if target.exists():
                target.unlink()
            p.rename(target)
            renamed.append((p.name, new_name))
            # 同步处理 sidecar
            sc = sdir / (p.name + ".sha256")
            if sc.exists():
                sc.rename(sdir / (new_name + ".sha256"))
    if not renamed:
        raise DrillError("时间线演练失败: 集合中没有可重命名的段")
    rec = {"demo": "wrong_timeline", "set": set_name,
           "renamed": renamed[:4], "created_at": now_iso()}
    store.state.failure_demos[set_name] = rec
    store.save()
    return rec


def make_truncated_set(store: Store, set_name: str, backup_name: str,
                       target_name: str) -> dict:
    """截断归档: 把目标之前的一个段截断(去掉尾部 8KiB 并破坏 sidecar 一致性)。"""
    cfg = store.cfg
    backup = store.state.backups[backup_name]
    rp = next(r for r in store.state.restore_points if r["name"] == target_name)
    start_lsn = backup["checkpoint_lsn"] or backup["start_lsn"]
    required = segment_range(start_lsn, rp["lsn"],
                             cfg.wal_seg_size_bytes, rp["timeline"])
    backup_wal = set(backup.get("backup_wal_segments", []))
    candidates = [s for s in required if s not in backup_wal] or required
    victim = candidates[len(candidates) // 2]

    build_archive_set(store, set_name)
    sdir = cfg.set_dir(set_name)
    target = sdir / victim
    if not target.exists():  # 可能仅在备份 pg_wal 中, 先从源归档补
        src = cfg.archive_dir / victim
        if not src.exists():
            raise DrillError(f"截断演练失败: 归档中找不到 {victim}")
        shutil.copy2(src, target)

    # 关键: 集合文件默认是源归档的硬链接(共享 inode), 直接 truncate 会污染源归档。
    # 先解除硬链接、替换为独立 inode 的副本, 所有破坏只作用于集合副本。
    source_seg = cfg.archive_dir / victim
    source_size_before = source_seg.stat().st_size if source_seg.exists() else None
    tmp_independent = target.with_suffix(".independent")
    shutil.copy2(target, tmp_independent)
    os.replace(tmp_independent, target)
    if source_seg.exists():
        # 确保替换后集合文件与源归档已是不同 inode
        assert os.stat(target).st_ino != os.stat(source_seg).st_ino, \
            "截断前未能解除硬链接, 为保护源归档中止"

    size_before = target.stat().st_size
    # 截断尾部 8192 字节
    with target.open("r+b") as f:
        f.truncate(size_before - 8192)
    size_after = target.stat().st_size
    # 破坏后必须确认源归档段大小未受影响
    if source_size_before is not None:
        assert source_seg.stat().st_size == source_size_before, \
            "源归档段被截断演练波及, 中止"
    # sidecar 保留旧哈希 -> 哈希复核必然报警
    rec = {"demo": "truncated", "set": set_name, "segment": victim,
           "size_before": size_before, "size_after": size_after,
           "source_archive_untouched": True,
           "created_at": now_iso()}
    store.state.failure_demos[set_name] = rec
    store.save()
    return rec
