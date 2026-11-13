"""配置：网关可调参数，全部有明确默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 运行时根目录：日志、导出文件。可由 QGATEWAY_RUNTIME 覆盖。
RUNTIME_DIR = Path(os.environ.get("QGATEWAY_RUNTIME", Path(__file__).resolve().parent.parent / ".runtime"))
# virtiofs/网络挂载既不能承载 postgres 数据目录，也不能承载其 Unix socket，
# 因此数据目录与 socket 统一放本地磁盘，可由 QGATEWAY_PGDATA 覆盖。
PGDATA_DIR = Path(os.environ.get("QGATEWAY_PGDATA", "/tmp/qgateway-pgdata"))
SOCKET_DIR = PGDATA_DIR


def default_dsn() -> str:
    """默认通过 Unix socket 连接本地专用实例（见 scripts/init_db.sh）。"""
    socket_dir = str(SOCKET_DIR)
    user = os.environ.get("QGATEWAY_DB_USER", os.environ.get("USER", "node"))
    dbname = os.environ.get("QGATEWAY_DB_NAME", "taskdb")
    return f"host={socket_dir} user={user} dbname={dbname}"


@dataclass
class Config:
    dsn: str = field(default_factory=default_dsn)
    host: str = "127.0.0.1"
    port: int = 8080
    pool_size: int = 3
    # 等待连接的队列上限；池忙且队列满时直接 503 快速拒绝
    wait_queue_limit: int = 2
    # 有界背压：生产者与网络消费者之间允许在途的帧缓存数量
    frame_queue_limit: int = 4
    # 单个服务端命名游标每次 fetchmany 的行数（真实 fetch 块大小）
    fetch_block_rows: int = 200
    # 优雅停止时给在途请求的有限清理时间
    shutdown_grace_seconds: float = 6.0
    app_name: str = "qgateway"
    log_events: bool = True
    log_path: Path | None = None
