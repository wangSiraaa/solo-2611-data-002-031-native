"""Execution trace (JSONL) and page-structure summaries.

Sensitive material (session cookies, passwords) is never written.  Option
machine values *are* recorded because they explain how a label was resolved;
they are harmless for this synthetic site and useful for diagnostics.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Trace:
    def __init__(self, directory: str | Path, run_id: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir = self.dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "trace.jsonl"
        self.events_path = self.path
        self.run_id = run_id
        self._seq = 0
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, task: str, stage: str, kind: str, **fields) -> dict:
        with self._lock:
            self._seq += 1
            payload = {"run_id": self.run_id, "task": task, "seq": self._seq,
                       "stage": stage, "event": kind, "ts": _now(), **fields}
            self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._fh.flush()
            return payload

    def save_page_summary(self, task: str, stage: str, summary: dict) -> str:
        name = f"{_safe(task)}-{self._seq:03d}-{_safe(stage)}.json"
        path = self.pages_dir / name
        text = json.dumps(summary, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        return str(path.relative_to(self.dir))

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def _safe(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in text]
    out = "".join(keep)[:40]
    return out or "task"


def read_events(path: str | Path) -> list[dict]:
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
