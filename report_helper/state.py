"""Persistent per-task state and the task temporary area.

Each task gets its own directory under the temp area.  Download bytes are
written to ``<task>/.partial/`` and only promoted to ``<task>/report.csv``
after content validation succeeds; ``state.json`` distinguishes:

  * downloaded_file  - bytes arrived (may still fail validation)
  * completed        - preview<->download identity/content/rowcount verified
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SaveState = Literal["none", "downloaded_file", "completed", "failed"]


@dataclass
class TaskState:
    task: str
    org_label: str
    month_label: str
    type_label: str
    stage: str = "pending"              # pending/login/org/params/preview/download/done/failed
    save_state: SaveState = "none"
    downloaded_file: str | None = None
    final_file: str | None = None
    sha256: str | None = None
    qid: str | None = None
    preview_columns: list[str] = field(default_factory=list)
    preview_row_count: int | None = None
    preview_total: str | None = None
    csv_columns: list[str] = field(default_factory=list)
    csv_row_count: int | None = None
    csv_total: str | None = None
    attempts: int = 0
    recoveries: int = 0
    last_error: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    updated: str = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def task_dir(root: str | Path, task_name: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in task_name)[:60]
    return Path(root) / (safe or "task")


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir_for(self, task_name: str) -> Path:
        d = task_dir(self.root, task_name)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".partial").mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, task_name: str) -> Path:
        return self.dir_for(task_name) / "state.json"

    def load(self, task_name: str) -> TaskState | None:
        p = self.path_for(task_name)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        known = set(TaskState.__dataclass_fields__)
        filtered = {k: v for k, v in raw.items() if k in known}
        try:
            return TaskState(**filtered)
        except TypeError:
            return None

    def save(self, state: TaskState) -> Path:
        state.updated = _now()
        p = self.path_for(state.task)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def write_partial(self, task_name: str, data: bytes, name: str = "download.bin") -> Path:
        d = self.dir_for(task_name) / ".partial"
        p = d / name
        p.write_bytes(data)
        return p

    def promote(self, task_name: str, partial: Path,
                final_name: str = "report.csv") -> tuple[Path, str]:
        d = self.dir_for(task_name)
        final = d / final_name
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        os.replace(partial, final)
        return final, digest

    def clear_partial(self, task_name: str):
        d = self.dir_for(task_name) / ".partial"
        for child in d.glob("*"):
            try:
                child.unlink()
            except OSError:
                pass
