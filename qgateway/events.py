"""诊断事件日志。

记录仅用于诊断与可观测性，不参与任何控制决策；控制面一律以数据库真实状态
（pg_stat_activity、连接事务状态、驱动异常）为准。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


class EventLogger:
    def __init__(self, log_path: Path | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.log_path = log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record = {"ts": time.time(), "kind": kind, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
