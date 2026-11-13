"""通用工具: 进程执行、psql 封装、LSN/WAL 段计算、controldata 解析、哈希、日志。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import DrillConfig


class DrillError(RuntimeError):
    """演练业务错误(可预期失败, 如预检不通过)。"""


# ---------------------------------------------------------------- 进程执行
@dataclass
class ProcResult:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(argv: Sequence[str | Path], *, env: dict | None = None,
        stdin: str | None = None, timeout: int | None = None,
        check: bool = False, cwd: Path | None = None) -> ProcResult:
    argv = [str(a) for a in argv]
    e = os.environ.copy()
    e.setdefault("LC_ALL", "C")
    if env:
        e.update(env)
    p = subprocess.run(
        argv, input=stdin, capture_output=True, text=True,
        env=e, timeout=timeout, cwd=str(cwd) if cwd else None,
    )
    res = ProcResult(argv=argv, rc=p.returncode,
                     stdout=p.stdout or "", stderr=p.stderr or "")
    if check and not res.ok:
        raise DrillError(
            f"命令失败 rc={res.rc}: {' '.join(argv)}\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}")
    return res


def start_process(argv: Sequence[str | Path], *, log_path: Path,
                  env: dict | None = None, cwd: Path | None = None) -> int:
    """启动长驻进程, stdout/stderr 重定向到日志, 返回 PID。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lf = log_path.open("ab", buffering=0)
    e = os.environ.copy()
    e.setdefault("LC_ALL", "C")
    if env:
        e.update(env)
    p = subprocess.Popen(
        [str(a) for a in argv], stdout=lf, stderr=subprocess.STDOUT,
        env=e, cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    return p.pid


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pidfile(path: Path) -> int:
    try:
        return int(path.read_text().strip().splitlines()[0])
    except (FileNotFoundError, IndexError, ValueError):
        return -1


def terminate_pid(pid: int, *, timeout: int = 20) -> bool:
    """优雅停止, 超时后 SIGKILL。返回是否曾在运行。"""
    if not pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(0.5)
    return True


# ---------------------------------------------------------------- psql 封装
def psql(cfg: DrillConfig, *, socket_dir: Path, port: int, sql: str,
         dbname: str = "postgres", timeout: int = 60,
         tuples_only: bool = True, user: str = "postgres") -> ProcResult:
    argv = [
        cfg.pgbin / "psql", "-v", "ON_ERROR_STOP=1",
        "-h", str(socket_dir), "-p", str(port), "-U", user,
        "-d", dbname, "-X", "-q",
    ]
    if tuples_only:
        argv.append("-tA")
    argv.extend(("-F", "|"))
    return run(argv, stdin=sql, timeout=timeout)


def psql_value(cfg: DrillConfig, *, socket_dir: Path, port: int,
               sql: str, dbname: str = "postgres") -> str:
    r = psql(cfg, socket_dir=socket_dir, port=port, sql=sql, dbname=dbname)
    if not r.ok:
        raise DrillError(f"psql 查询失败: {r.stderr.strip()}\nSQL: {sql}")
    return r.stdout.strip()


def pg_isready(cfg: DrillConfig, socket_dir: Path, port: int) -> bool:
    r = run([cfg.pgbin / "pg_isready", "-h", str(socket_dir),
             "-p", str(port), "-U", "postgres", "-q"])
    return r.ok


def wait_ready(cfg: DrillConfig, socket_dir: Path, port: int,
               *, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pg_isready(cfg, socket_dir, port):
            return
        time.sleep(0.3)
    raise DrillError(f"数据库在 {timeout}s 内未就绪: {socket_dir}:{port}")


# ---------------------------------------------------------------- LSN / WAL 段
def lsn_to_int(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def int_to_lsn(value: int) -> str:
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:08X}"


def lsn_segment(lsn_int: int, wal_seg_size: int) -> int:
    # 段内偏移掩码: wal_seg_size-1; 段逻辑序号 = LSN // seg_size
    return lsn_int // wal_seg_size


def segment_name(seg_no: int, timeline: int = 1) -> str:
    """逻辑段号 -> 24 字符 WAL 文件名 (TTTTTTTTLLLLLLLLSSSSSSSS)。"""
    log = seg_no // 256
    seg = seg_no % 256
    return f"{timeline:08X}{log:08X}{seg:08X}"


def parse_segment_name(name: str) -> tuple[int, int]:
    """WAL 文件名 -> (timeline, 逻辑段号)。"""
    base = Path(name).name
    if len(base) != 24:
        raise DrillError(f"非法 WAL 段名: {name}")
    tli = int(base[0:8], 16)
    log = int(base[8:16], 16)
    seg = int(base[16:24], 16)
    return tli, log * 256 + seg


def segment_range(start_lsn: str, end_lsn: str, wal_seg_size: int,
                  timeline: int = 1) -> list[str]:
    """返回 [start_lsn 所在段 .. end_lsn 所在段] 的全部段名(含端点)。"""
    a = lsn_segment(lsn_to_int(start_lsn), wal_seg_size)
    b = lsn_segment(lsn_to_int(end_lsn), wal_seg_size)
    return [segment_name(i, timeline) for i in range(a, b + 1)]


# ---------------------------------------------------------------- controldata
def parse_controldata(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def read_controldata(cfg: DrillConfig, data_dir: Path) -> dict[str, str]:
    r = run([cfg.pgbin / "pg_controldata", data_dir], check=False)
    if not r.ok:
        raise DrillError(f"pg_controldata 失败: {r.stderr.strip()}")
    return parse_controldata(r.stdout)


def system_identifier(cfg: DrillConfig, data_dir: Path) -> str:
    return read_controldata(cfg, data_dir)["Database system identifier"]


# ---------------------------------------------------------------- 哈希 / 文件
def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def tail_text(path: Path, max_bytes: int = 8000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def robust_rmtree(path: Path, *, attempts: int = 4) -> None:
    """带重试的目录删除, 应对挂载点偶发 EPERM; 失败耗尽才抛出。"""
    if not path.exists():
        return

    def _on_error(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    last = None
    for i in range(attempts):
        try:
            shutil.rmtree(path, onerror=_on_error)
            return
        except OSError as e:  # pragma: no cover - 取决于文件系统
            last = e
            time.sleep(0.6 * (i + 1))
    raise DrillError(f"目录删除多次重试仍失败 {path}: {last}")


def robust_write_text(path: Path, text: str, *, attempts: int = 4) -> None:
    """带重试的文本写入; 必要时补 owner 写位, 应对挂载点瞬时 EPERM。"""
    last = None
    for i in range(attempts):
        try:
            if path.exists():
                os.chmod(path, (path.stat().st_mode & 0o700) | 0o600)
            path.write_text(text)
            return
        except OSError as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise DrillError(f"文件写入多次重试仍失败 {path}: {last}")


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def robust_copytree(src: Path, dst: Path, *, attempts: int = 4) -> None:
    """带重试的目录复制。

    部分挂载点(网络/virtiofs)对批量 create/chmod 偶发 EPERM/EACCES,
    清理半成品后有限重试; 重试耗尽才抛出, 避免单次瞬时错误中断演练。
    """
    last: Exception | None = None
    for i in range(attempts):
        robust_rmtree(dst)
        try:
            shutil.copytree(src, dst, symlinks=True)
            enforce_pgdata_permissions(dst)
            return
        except (PermissionError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise DrillError(f"目录复制多次重试仍失败 {src} -> {dst}: {last}")


def enforce_pgdata_permissions(data_dir: Path) -> None:
    """postmaster 要求 PGDATA 为 0700/0750; 递归去掉 group/other 权限位。"""
    os.chmod(data_dir, 0o700)
    for root, dirs, files in os.walk(data_dir):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
        for f in files:
            fp = os.path.join(root, f)
            if os.path.islink(fp):
                continue
            try:
                mode = os.stat(fp).st_mode & 0o777
                os.chmod(fp, mode & 0o700)  # 保留 owner 权限, 去掉 group/other
            except OSError:
                pass


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """归档集合优先硬链接(同文件系统零成本), 不支持时回退复制。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ---------------------------------------------------------------- 时间与 JSONL
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def wait_deadline(seconds: float) -> float:
    return time.time() + seconds


def sleep_briefly() -> None:
    time.sleep(0.3)


def human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}TiB"


def dir_size(path: Path) -> int:
    """统计目录占用, 按 (st_dev, st_ino) 去重, 避免硬链接被重复计字节。"""
    total = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, files in os.walk(path):
        for fn in files:
            fp = Path(root) / fn
            try:
                st = fp.lstat()
            except FileNotFoundError:
                continue
            if os.path.islink(fp):
                total += st.st_size  # 符号链接本身很小, 直接计
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def env_pgdata(data_dir: Path) -> dict:
    return {"PGDATA": str(data_dir)}


def list_sorted(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(p.name for p in path.iterdir())
