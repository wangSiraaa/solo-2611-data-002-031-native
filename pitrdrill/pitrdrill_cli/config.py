"""演练配置与目录布局。

所有路径默认位于工作目录下的单一根目录中, 便于界定磁盘清理边界;
所有实例使用独立端口与独立 Unix socket 目录(置于 /tmp, 避免 108 字节限制问题)。
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

PGBIN = Path(os.environ.get("PGBIN", "/usr/lib/postgresql/16/bin"))
STATE_NAME = "drill-state.json"


def default_root() -> Path:
    return Path(os.environ.get("PITRDRILL_ROOT", "/workspace/pitrdrill-work"))


def find_free_port(preferred: int | None = None) -> int:
    """让内核分配空闲 TCP 端口; 优先尝试偏好端口。"""
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates.extend(range(55432, 55532))
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用端口")


@dataclass
class DrillConfig:
    root: Path = field(default_factory=default_root)
    pgbin: Path = PGBIN
    # 阶段等待预算(秒): 等归档文件 / 等恢复到达目标
    archive_wait_budget: int = 120
    recovery_wait_budget: int = 180
    # 失败演示(缺 WAL)中, 等待归档的受限预算
    gap_wait_budget: int = 30
    # 心跳写入间隔(秒)与默认持续秒数
    tick_seconds: float = 0.5
    workload_seconds: float = 20.0

    # 运行期分配(init/源实例启动时写入)
    source_port: int = 0
    base_port: int = 55432
    restore_port_base: int = 55500
    wal_seg_size_bytes: int = 16 * 1024 * 1024

    # ---- 目录布局 ----
    # 注意: 部分工作目录挂载点(网络/virtiofs)不支持对 unix socket 做 chmod,
    # 因此 socket 统一放到本机 /tmp 下的专用目录; 数据文件仍全部在 root 内。
    @property
    def socket_root(self) -> Path:
        return Path(os.environ.get("PITRDRILL_SOCKET_ROOT",
                                   "/tmp/pitrdrill-sockets")) / self.root.name

    @property
    def state_file(self) -> Path:
        return self.root / STATE_NAME

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def source_data(self) -> Path:
        return self.source_dir / "data"

    @property
    def source_socket(self) -> Path:
        return self.socket_root / "source"

    @property
    def source_log(self) -> Path:
        return self.source_dir / "source.log"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive" / "wal"

    @property
    def archive_index(self) -> Path:
        return self.root / "archive" / "archive-index.jsonl"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def sets_dir(self) -> Path:
        return self.root / "archive-sets"

    @property
    def restores_dir(self) -> Path:
        return self.root / "restores"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def backup_dir(self, name: str) -> Path:
        return self.backups_dir / name

    def set_dir(self, name: str) -> Path:
        return self.sets_dir / name

    def restore_dir(self, name: str) -> Path:
        return self.restores_dir / name

    def restore_data(self, name: str) -> Path:
        return self.restore_dir(name) / "data"

    def restore_socket(self, name: str) -> Path:
        return self.socket_root / "restore" / name

    def restore_log(self, name: str) -> Path:
        return self.restore_dir(name) / "restore.log"

    def restore_config_file(self, name: str) -> Path:
        return self.restore_dir(name) / "recovery.settings"

    def marker_path(self, kind: str, name: str) -> Path:
        """PGDATA 内部身份标记, 强制退出后仍可辨认本工具创建的实例。"""
        root = self.source_data if kind == "source" else self.restore_data(name)
        return root / f"pitrdrill-{kind}.marker"

    def ensure_dirs(self) -> None:
        for d in (
            self.root, self.source_dir, self.archive_dir, self.backups_dir,
            self.sets_dir, self.restores_dir, self.reports_dir, self.tmp_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ---- 序列化 ----
    def to_json(self) -> dict:
        d = asdict(self)
        d["root"] = str(self.root)
        d["pgbin"] = str(self.pgbin)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "DrillConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        cfg = cls(root=Path(d["root"]), pgbin=Path(d.get("pgbin", str(PGBIN))))
        for k, v in d.items():
            if k in known and k not in ("root", "pgbin"):
                setattr(cfg, k, v)
        return cfg

    @classmethod
    def load(cls, root: Path | None = None) -> "DrillConfig":
        root = root or default_root()
        with (root / STATE_NAME).open(encoding="utf-8") as f:
            payload = json.load(f)
        return cls.from_json(payload["config"])
