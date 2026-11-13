"""Shared utilities: sharding rule, canonical JSON, hashing, file barriers."""

import hashlib
import json
import os
import re
import time

EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SNAPSHOT_ID_RE = re.compile(r"^[0-9A-Fa-f]{1,10}-[0-9A-Fa-f]{1,10}(-[0-9A-Fa-f]{1,10})?$")


def shard_of(order_id: int, shards: int) -> int:
    """The single, stable sharding rule used by every worker and validator."""
    return order_id % shards


def canon(obj) -> str:
    """Canonical one-line JSON (stable key order, compact) -> stable digests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        pass


def wait_for(path: str, timeout: float, poll: float = 0.05, abort=None) -> bool:
    """Poll until `path` exists. `abort()` returning True cancels the wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        if abort is not None and abort():
            return False
        time.sleep(poll)
    return os.path.exists(path)


def write_file_atomic(path: str, data: str) -> None:
    """Write tmp file in the same directory, fsync, then rename."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, obj) -> None:
    write_file_atomic(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: str) -> list:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"
