"""冻结清单的读取与源文件核验。

读取任何源之前先核对声明的字节长度与 SHA256；不符则该源失败，
绝不带着不匹配的源继续解析。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceSpec:
    path: str  # 清单中声明的相对路径（相对于 root）
    declared_bytes: int
    declared_sha256: str
    format: str  # "text" | "jsonl"
    last_event_complete: bool | None  # true / false / 未声明(None)


@dataclass(frozen=True)
class Manifest:
    root: Path  # 源相对路径的解析根
    encoding: str
    sources: tuple[SourceSpec, ...]


def load_manifest(fixtures_dir: Path) -> Manifest:
    """读取 fixtures 目录下的 manifest.json。

    源路径相对于 fixtures 目录的父目录解析（清单内路径形如
    "fixtures/sources/app.log"）。
    """
    fixtures_dir = Path(fixtures_dir)
    manifest_path = fixtures_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    sources = []
    for entry in doc["files"]:
        sources.append(
            SourceSpec(
                path=entry["path"],
                declared_bytes=entry["bytes"],
                declared_sha256=entry["sha256"],
                format=entry["format"],
                last_event_complete=entry.get("last_event_complete"),
            )
        )
    return Manifest(
        root=fixtures_dir.parent,
        encoding=doc.get("encoding", "UTF-8"),
        sources=tuple(sources),
    )


def read_and_verify(spec: SourceSpec, root: Path) -> tuple[bytes | None, dict]:
    """读取源字节并核验长度与 SHA256。

    返回 (raw, verification)；核验失败时 raw 为 None，调用方必须
    把该源标记为失败，不得继续解析。
    """
    verification = {
        "declared_bytes": spec.declared_bytes,
        "declared_sha256": spec.declared_sha256,
        "actual_bytes": None,
        "actual_sha256": None,
        "ok": False,
    }
    full = Path(root) / spec.path
    try:
        raw = full.read_bytes()
    except OSError as exc:
        verification["error"] = f"read failed: {exc}"
        return None, verification
    verification["actual_bytes"] = len(raw)
    verification["actual_sha256"] = hashlib.sha256(raw).hexdigest()
    if len(raw) != spec.declared_bytes:
        verification["error"] = (
            f"byte length mismatch: declared {spec.declared_bytes}, "
            f"actual {len(raw)}"
        )
        return None, verification
    if verification["actual_sha256"] != spec.declared_sha256:
        verification["error"] = "sha256 mismatch"
        return None, verification
    verification["ok"] = True
    return raw, verification
