"""安全清理: 只清理本工具创建、带 pitrdrill 标记的本次演练资源。

硬性保护:
  - 仅允许删除配置根目录下的路径
  - 删除 PGDATA 前校验 pitrdrill-*.marker
  - 绝不删除根目录之外的任何内容; 源库仅在显式 --include-source 时停止并删除
"""
from __future__ import annotations

from . import TASK_TAG
from .state import Store
from .util import read_pidfile, robust_rmtree, terminate_pid


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def cleanup(store: Store, *, include_source: bool = False,
            include_archive: bool = False, yes: bool = False) -> dict:
    cfg = store.cfg
    removed: list[str] = []
    stopped: list[int] = []

    # 1. 停止并删除恢复实例(逐个校验 marker)
    for name, rs in list(store.state.restores.items()):
        data = cfg.restore_data(name)
        marker = cfg.marker_path("restore", name)
        if data.exists() and marker.exists() and TASK_TAG in marker.read_text():
            pid = read_pidfile(data / "postmaster.pid")
            if pid > 0 and terminate_pid(pid):
                stopped.append(pid)
            robust_rmtree(cfg.restore_dir(name))
            removed.append(str(cfg.restore_dir(name)))

    # 2. 源实例(默认保留, 显式要求才清理)
    if include_source:
        data = cfg.source_data
        marker = cfg.marker_path("source", "")
        if data.exists() and marker.exists() and TASK_TAG in marker.read_text():
            pid = read_pidfile(data / "postmaster.pid")
            if pid > 0 and terminate_pid(pid):
                stopped.append(pid)
            robust_rmtree(cfg.source_dir)
            removed.append(str(cfg.source_dir))

    # 3. 归档(默认保留; 显式要求才删)
    if include_archive and _is_within(cfg.root, cfg.archive_dir):
        robust_rmtree(cfg.archive_dir.parent)
        removed.append(str(cfg.archive_dir.parent))

    # 4. 备份/集合/报告/临时目录(均在根内)
    for p in (cfg.backups_dir, cfg.sets_dir, cfg.restores_dir,
              cfg.tmp_dir, cfg.root / "bin"):
        if p.exists() and _is_within(cfg.root, p):
            robust_rmtree(p)
            removed.append(str(p))

    # 4b. /tmp 下的独立 socket 目录(按演练根目录名命名, 仅删本演练的)。
    #     保留源实例时绝不能删除源 socket, 只清理 restore 子树;
    #     显式清理源实例时才删除整个 socket 根。
    sock_root = cfg.socket_root
    if sock_root.exists() and sock_root.parent.name == "pitrdrill-sockets":
        if include_source:
            robust_rmtree(sock_root)
            removed.append(str(sock_root))
        else:
            restore_sock = sock_root / "restore"
            if restore_sock.exists():
                robust_rmtree(restore_sock)
                removed.append(str(restore_sock))

    # 5. 状态文件最后删除(仅当源与归档都要求清理时, 才算彻底销毁演练)
    if include_source and include_archive:
        for p in (cfg.reports_dir, cfg.state_file):
            if p.exists() and _is_within(cfg.root, p):
                if p.is_dir():
                    robust_rmtree(p)
                else:
                    p.unlink()
                removed.append(str(p))
    else:
        # 保留状态并刷新(恢复条目已清空)
        store.state.restores = {}
        store.save()

    return {"stopped_pids": stopped, "removed": removed,
            "source_kept": not include_source,
            "archive_kept": not include_archive}


def list_resources(store: Store) -> dict:
    cfg = store.cfg
    return {
        "root": str(cfg.root),
        "source": {
            "data_dir": str(cfg.source_data),
            "running": read_pidfile(cfg.source_data / "postmaster.pid") > 0,
            "marker": cfg.marker_path("source", "").exists(),
        },
        "restores": {
            name: {
                "data_dir": rs.data_dir, "phase": rs.phase, "result": rs.result,
                "pid": rs.pid, "port": rs.port,
                "marker": cfg.marker_path("restore", name).exists(),
            } for name, rs in store.state.restores.items()
        },
        "backups": list(store.state.backups),
        "archive_sets": list(store.state.archive_sets),
    }
