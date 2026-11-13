"""演练持久化状态。

强制退出后, 通过 drill-state.json + PGDATA 内的 pitrdrill-*.marker
仍可辨认本工具创建的实例与目录, 做到只清理本次演练资源。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import TASK_TAG
from .config import STATE_NAME, DrillConfig
from .util import now_iso

# 阶段状态机
PHASE_INIT = "INITIALIZED"
PHASE_SOURCE_RUNNING = "SOURCE_RUNNING"
PHASE_WORKLOAD = "WORKLOAD"
PHASE_BACKUP = "BASE_BACKUP"
PHASE_RESTORE_PREFLIGHT = "RESTORE_PREFLIGHT"
PHASE_RESTORE_WAITING_ARCHIVE = "WAITING_ARCHIVE"
PHASE_RESTORE_REPLAYING = "REPLAYING"
PHASE_TARGET_REACHED = "TARGET_REACHED"
PHASE_PROMOTED = "PROMOTED"
PHASE_FAILED = "FAILED"

# 报告要求区分的四种(+提升)结果分类
RESULT_TARGET_REACHED = "TARGET_REACHED"
RESULT_WAITING_ARCHIVE = "WAITING_ARCHIVE"
RESULT_FAILED = "FAILED"
RESULT_PROMOTED = "PROMOTED"


@dataclass
class RestoreState:
    name: str
    backup: str
    target: str
    archive_set: str
    data_dir: str
    socket_dir: str
    log_path: str
    port: int
    pid: int = -1
    phase: str = PHASE_RESTORE_PREFLIGHT
    result: str = ""
    gated: bool = False
    gate_file: str = ""
    preflight: dict[str, Any] = field(default_factory=dict)
    failure_reasons: list[str] = field(default_factory=list)
    created_at: str = ""
    target_evidence: dict[str, Any] = field(default_factory=dict)
    promoted_timeline: int | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    commands: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DrillState:
    task_tag: str = TASK_TAG
    created_at: str = field(default_factory=now_iso)
    phase: str = PHASE_INIT
    config: dict = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    backups: dict[str, dict[str, Any]] = field(default_factory=dict)
    restore_points: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    restores: dict[str, RestoreState] = field(default_factory=dict)
    archive_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_demos: dict[str, dict[str, Any]] = field(default_factory=dict)
    command_log: list[dict[str, Any]] = field(default_factory=list)

    # ---- 持久化 ----
    def to_json(self) -> dict:
        return {
            "task_tag": self.task_tag,
            "created_at": self.created_at,
            "phase": self.phase,
            "config": self.config,
            "source": self.source,
            "backups": self.backups,
            "restore_points": self.restore_points,
            "events": self.events,
            "restores": {k: _rs_to_dict(v) for k, v in self.restores.items()},
            "archive_sets": self.archive_sets,
            "failure_demos": self.failure_demos,
            "command_log": self.command_log,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "DrillState":
        s = cls()
        s.task_tag = payload.get("task_tag", TASK_TAG)
        s.created_at = payload.get("created_at", now_iso())
        s.phase = payload.get("phase", PHASE_INIT)
        s.config = payload.get("config", {})
        s.source = payload.get("source", {})
        s.backups = payload.get("backups", {})
        s.restore_points = payload.get("restore_points", [])
        s.events = payload.get("events", [])
        s.restores = {
            k: RestoreState(**v) for k, v in payload.get("restores", {}).items()
        }
        s.archive_sets = payload.get("archive_sets", {})
        s.failure_demos = payload.get("failure_demos", {})
        s.command_log = payload.get("command_log", [])
        return s


def _rs_to_dict(rs: RestoreState) -> dict:
    return {
        "name": rs.name, "backup": rs.backup, "target": rs.target,
        "archive_set": rs.archive_set, "data_dir": rs.data_dir,
        "socket_dir": rs.socket_dir, "log_path": rs.log_path, "port": rs.port,
        "pid": rs.pid, "phase": rs.phase, "result": rs.result,
        "gated": rs.gated, "gate_file": rs.gate_file,
        "preflight": rs.preflight, "failure_reasons": rs.failure_reasons,
        "created_at": rs.created_at,
        "target_evidence": rs.target_evidence,
        "promoted_timeline": rs.promoted_timeline,
        "verification": rs.verification, "commands": rs.commands,
    }


class Store:
    """状态文件原子读写 + 命令/阶段留痕。"""

    def __init__(self, cfg: DrillConfig):
        self.cfg = cfg
        self.path: Path = cfg.state_file
        self.state: DrillState = DrillState()
        if self.path.exists():
            self.reload()
        else:
            self.state.config = cfg.to_json()
            self.save()

    def reload(self) -> None:
        with self.path.open(encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("task_tag") != TASK_TAG:
            raise RuntimeError(f"状态文件任务标识不符, 拒绝操作: {self.path}")
        self.state = DrillState.from_json(payload)

    def save(self) -> None:
        self.cfg.ensure_dirs()
        fd, tmp = tempfile.mkstemp(dir=str(self.cfg.root), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state.to_json(), f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def set_phase(self, phase: str) -> None:
        self.state.phase = phase
        self.save()

    def log_command(self, argv: list[str], rc: int, where: str,
                    extra: dict | None = None) -> None:
        rec = {"ts": now_iso(), "argv": argv, "rc": rc, "where": where}
        if extra:
            rec.update(extra)
        self.state.command_log.append(rec)
        # 命令日志可能很长, 保留最近 500 条在状态内, 全量另存
        self.save()

    def add_event(self, kind: str, **fields: Any) -> dict:
        rec = {"ts": now_iso(), "kind": kind, **fields}
        self.state.events.append(rec)
        self.save()
        return rec

    def get_restore(self, name: str) -> RestoreState:
        if name not in self.state.restores:
            raise KeyError(name)
        return self.state.restores[name]

    def upsert_restore(self, rs: RestoreState) -> None:
        self.state.restores[rs.name] = rs
        self.save()


def state_exists(root: Path) -> bool:
    return (root / STATE_NAME).exists()
