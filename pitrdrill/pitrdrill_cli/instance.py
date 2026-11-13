"""PostgreSQL 实例生命周期与业务库构造。

仅管理本工具 initdb 出来的、带 pitrdrill 标记的实例;
所有连接走 127.0.0.1 独立端口 + 独立 Unix socket, 不触碰用户已有实例。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import TASK_TAG
from .config import DrillConfig, find_free_port
from .state import Store
from .util import (DrillError, append_jsonl,
                   lsn_to_int, now_iso, psql, psql_value, read_pidfile,
                   robust_write_text, run, sha256_file,
                   system_identifier, terminate_pid, wait_ready)

DB_NAME = "pitrdrill_biz"
APP_NAME = "pitrdrill"

SCHEMA_SQL = """
CREATE TABLE pitrdrill_parent (
    id           bigint PRIMARY KEY,
    name         text NOT NULL,
    version      bigint NOT NULL DEFAULT 0,
    payload      text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE pitrdrill_child (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id    bigint NOT NULL REFERENCES pitrdrill_parent(id),
    tick         bigint NOT NULL,
    note         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX pitrdrill_child_parent_idx ON pitrdrill_child(parent_id);
CREATE SEQUENCE pitrdrill_event_seq AS bigint START WITH 1;
CREATE TABLE pitrdrill_witness (
    marker       text PRIMARY KEY,
    event_seq    bigint NOT NULL DEFAULT nextval('pitrdrill_event_seq'),
    detail       jsonb NOT NULL,
    commit_lsn   pg_lsn NOT NULL DEFAULT pg_current_wal_lsn(),
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE pitrdrill_event_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_seq    bigint NOT NULL,
    kind         text NOT NULL,
    marker       text,
    detail       jsonb NOT NULL,
    commit_lsn   pg_lsn NOT NULL DEFAULT pg_current_wal_lsn(),
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO pitrdrill_parent (id, name, payload)
SELECT g, 'parent-' || g, 'seed'
FROM generate_series(1, 10) AS g;
"""


# ---------------------------------------------------------------- 包装脚本
def write_archive_push_wrapper(cfg: DrillConfig) -> Path:
    """生成 archive_command 调用的包装脚本(绝对路径, 无空格)。"""
    bindir = cfg.root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "pitrdrill-archive-push.sh"
    robust_write_text(wrapper,
        "#!/bin/sh\n"
        "# pitrdrill 自动生成: archive_command WAL 推送包装\n"
        f'exec "{sys.executable}" "{Path(__file__).resolve().parent.parent}/pitrdrill.py" '
        f'--root "{cfg.root}" archive-push "$1" "$2"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


def write_gated_restore_fetch_wrapper(cfg: DrillConfig, restore_name: str,
                                      set_dir: Path, log_path: Path,
                                      gate_file: Path) -> Path:
    """门控 restore_command: 门文件出现前所有 WAL 都"尚未归档"(返回 1)。

    用于演示归档迟到: 恢复实例真实进入归档等待, 放行后再继续重放。
    门文件出现后行为与普通拉取包装一致。
    """
    bindir = cfg.restore_dir(restore_name)
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "pitrdrill-restore-gated.sh"
    robust_write_text(wrapper,
        "#!/bin/sh\n"
        "# pitrdrill 自动生成: 门控 restore_command(归档迟到演练)\n"
        f'echo "[pitrdrill-gated] request=$1 dest=$2" >> "{log_path}"\n'
        f'if [ ! -f "{gate_file}" ]; then\n'
        f'  echo "[pitrdrill-gated] GATED $1 (归档尚未到位)" >> "{log_path}"\n'
        "  exit 1\n"
        "fi\n"
        f'src="{set_dir}/$1"\n'
        'if [ -f "$src" ]; then\n'
        '  cp -- "$src" "$2" && echo "[pitrdrill-gated] HIT $1" >> "'
        f'{log_path}" && exit 0\n'
        "fi\n"
        f'echo "[pitrdrill-gated] MISS $1" >> "{log_path}"\n'
        "exit 1\n"
    )
    wrapper.chmod(0o755)
    return wrapper


def write_restore_fetch_wrapper(cfg: DrillConfig, restore_name: str,
                                set_dir: Path, log_path: Path) -> Path:
    """生成 restore_command 包装: 从指定归档集合取 WAL, 每次取文件留痕。"""
    bindir = cfg.restore_dir(restore_name)
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "pitrdrill-restore-fetch.sh"
    robust_write_text(wrapper,
        "#!/bin/sh\n"
        "# pitrdrill 自动生成: restore_command WAL 拉取包装\n"
        f'echo "[pitrdrill-restore-fetch] request=$1 dest=$2" >> "{log_path}"\n'
        f'src="{set_dir}/$1"\n'
        'if [ -f "$src" ]; then\n'
        '  cp -- "$src" "$2" && echo "[pitrdrill-restore-fetch] HIT $1" >> "'
        f'{log_path}" && exit 0\n'
        'fi\n'
        f'echo "[pitrdrill-restore-fetch] MISS $1" >> "{log_path}"\n'
        "exit 1\n"
    )
    wrapper.chmod(0o755)
    return wrapper


# ---------------------------------------------------------------- initdb / 配置
def init_source(store: Store) -> dict:
    cfg = store.cfg
    if cfg.source_data.exists() and (cfg.source_data / "PG_VERSION").exists():
        raise DrillError(f"源数据目录已存在, 拒绝重复 initdb: {cfg.source_data}")
    cfg.ensure_dirs()
    cfg.source_socket.mkdir(parents=True, exist_ok=True)
    port = find_free_port(cfg.base_port)
    cfg.source_port = port

    argv = [cfg.pgbin / "initdb", "-D", cfg.source_data, "-U", "postgres",
            "--auth-local=trust", "--auth-host=trust", "-E", "UTF8",
            "--locale=C"]
    # 挂载点偶发 wrong ownership/EPERM, 清理半成品后有限重试
    last_err = ""
    for attempt in range(4):
        if cfg.source_data.exists():
            from .util import robust_rmtree
            robust_rmtree(cfg.source_data)
        r = run(argv, check=False)
        store.log_command(r.argv, r.rc, f"initdb attempt={attempt + 1}")
        if r.ok:
            break
        last_err = f"{r.stderr}\n{r.stdout}"
        transient = ("wrong ownership" in last_err
                     or "Operation not permitted" in last_err
                     or "Permission denied" in last_err)
        if not transient or attempt == 3:
            raise DrillError(f"initdb 失败:\n{last_err}")
        time.sleep(1.0 * (attempt + 1))

    wrapper = write_archive_push_wrapper(cfg)
    conf = cfg.source_data / "postgresql.conf"
    robust_write_text(conf,
        "# pitrdrill 源实例配置(本工具自动生成)\n"
        f"listen_addresses = '127.0.0.1'\n"
        f"port = {port}\n"
        f"unix_socket_directories = '{cfg.source_socket}'\n"
        "max_connections = 60\n"
        "shared_buffers = 128MB\n"
        "wal_level = replica\n"
        "fsync = on\n"
        "synchronous_commit = on\n"
        "full_page_writes = on\n"
        "max_wal_size = 256MB\n"
        "min_wal_size = 80MB\n"
        "archive_mode = on\n"
        f"archive_command = '{wrapper} %p %f'\n"
        "archive_timeout = 30s\n"
        "log_line_prefix = '%m [%p] %q%u@%d '\n"
        "log_min_messages = info\n"
        "log_checkpoints = on\n"
        "hot_standby = on\n"
    )
    # 身份标记: 强制退出后仍可辨认
    robust_write_text(cfg.marker_path("source", ""),
        f"task={TASK_TAG}\nkind=source\ncreated_at={now_iso()}\nport={port}\n")

    store.state.source = {
        "data_dir": str(cfg.source_data),
        "socket_dir": str(cfg.source_socket),
        "log_path": str(cfg.source_log),
        "port": port,
        "archive_dir": str(cfg.archive_dir),
        "archive_wrapper": str(wrapper),
        "created_at": now_iso(),
    }
    store.save()
    return store.state.source


def start_source(store: Store) -> int:
    cfg = store.cfg
    existing = read_pidfile(cfg.source_data / "postmaster.pid")
    if existing > 0:
        from .util import pid_alive, pg_isready
        if pid_alive(existing) and pg_isready(cfg, cfg.source_socket, cfg.source_port):
            store.state.source["pid"] = existing
            store.save()
            return existing
    r = run([cfg.pgbin / "pg_ctl", "-D", cfg.source_data,
             "-l", cfg.source_log, "-w", "-t", "60", "start"], check=False)
    store.log_command(r.argv, r.rc, "pg_ctl start source")
    if not r.ok:
        raise DrillError(f"源实例启动失败, 见日志 {cfg.source_log}")
    wait_ready(cfg, cfg.source_socket, cfg.source_port)
    pid = read_pidfile(cfg.source_data / "postmaster.pid")
    store.state.source["pid"] = pid
    store.state.source["system_identifier"] = system_identifier(cfg, cfg.source_data)
    cfg.wal_seg_size_bytes = int(psql_value(
        cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
        sql="SELECT pg_size_bytes(current_setting('wal_segment_size'));"))
    store.state.source["wal_seg_size_bytes"] = cfg.wal_seg_size_bytes
    store.save()
    return pid


def stop_source(store: Store, mode: str = "fast") -> None:
    cfg = store.cfg
    pid = read_pidfile(cfg.source_data / "postmaster.pid")
    if pid <= 0:
        return
    r = run([cfg.pgbin / "pg_ctl", "-D", cfg.source_data,
             "-m", mode, "-w", "-t", "60", "stop"], check=False)
    store.log_command(r.argv, r.rc, f"pg_ctl stop source {mode}")
    terminate_pid(pid)


def create_business_db(store: Store) -> None:
    cfg = store.cfg
    r = run([cfg.pgbin / "createdb", "-h", cfg.source_socket,
             "-p", str(cfg.source_port), "-U", "postgres", DB_NAME], check=False)
    if not r.ok and "already exists" not in r.stderr:
        raise DrillError(f"createdb 失败: {r.stderr}")
    r = psql(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
             sql=SCHEMA_SQL, dbname=DB_NAME)
    store.log_command([f"psql:{DB_NAME}", "schema"], r.rc, "create schema")
    if not r.ok:
        raise DrillError(f"业务库 schema 创建失败: {r.stderr}")
    store.add_event("schema_created", db=DB_NAME)


# ---------------------------------------------------------------- 事务见证 / restore point
def current_lsn(cfg: DrillConfig, socket_dir: Path, port: int,
                dbname: str = DB_NAME) -> str:
    return psql_value(cfg, socket_dir=socket_dir, port=port,
                      sql="SELECT pg_current_wal_lsn()::text;", dbname=dbname)


def current_timeline(cfg: DrillConfig, socket_dir: Path, port: int) -> int:
    return int(psql_value(
        cfg, socket_dir=socket_dir, port=port,
        sql="SELECT timeline_id FROM pg_control_checkpoint();"))


def insert_witness(cfg: DrillConfig, marker: str, detail: dict,
                   socket_dir: Path, port: int) -> dict:
    """在已提交事务中写入内容明确的标记, 返回事务见证信息。"""
    detail_sql = json.dumps(detail, ensure_ascii=False).replace("'", "''")
    sql = (
        "BEGIN;\n"
        f"INSERT INTO pitrdrill_witness(marker, detail) VALUES "
        f"('{marker}', '{detail_sql}'::jsonb);\n"
        f"INSERT INTO pitrdrill_event_log(event_seq, kind, marker, detail) "
        f"SELECT event_seq, 'witness', marker, detail FROM pitrdrill_witness "
        f"WHERE marker='{marker}';\n"
        "COMMIT;\n"
        f"SELECT marker || '|' || commit_lsn::text || '|' || event_seq "
        f"FROM pitrdrill_witness WHERE marker='{marker}';"
    )
    r = psql(cfg, socket_dir=socket_dir, port=port, sql=sql, dbname=DB_NAME)
    if not r.ok:
        raise DrillError(f"写入见证 {marker} 失败: {r.stderr}")
    m, lsn, seq = r.stdout.strip().split("|")
    return {"marker": m, "commit_lsn": lsn, "event_seq": int(seq),
            "detail": detail}


def create_restore_point(store: Store, name: str, note: str) -> dict:
    """创建命名 restore point 并确保其 WAL 已归档(不依赖客户端墙钟)。"""
    cfg = store.cfg
    sql = f"SELECT pg_create_restore_point('{name}')::text;"
    lsn = psql_value(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
                     sql=sql, dbname=DB_NAME)
    # 强制 WAL 切换并等待归档, 让 restore point 所在段进入归档目录
    psql(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
         sql="SELECT pg_switch_wal();", dbname=DB_NAME)
    _wait_archive_caught_up(cfg, lsn)
    rec = {"name": name, "lsn": lsn, "note": note,
           "timeline": current_timeline(cfg, cfg.source_socket, cfg.source_port),
           "created_at": now_iso()}
    store.state.restore_points.append(rec)
    store.add_event("restore_point", **rec)
    store.save()
    return rec


def _wait_archive_caught_up(cfg: DrillConfig, lsn: str, budget: float = 60.0) -> None:
    """轮询归档目录, 直到覆盖给定 LSN 的 WAL 段出现。"""
    from .util import lsn_segment, segment_name
    target_seg = segment_name(lsn_segment(lsn_to_int(lsn), cfg.wal_seg_size_bytes))
    deadline = time.time() + budget
    while time.time() < deadline:
        if (cfg.archive_dir / target_seg).exists():
            return
        time.sleep(0.4)
    raise DrillError(f"等待 WAL 段 {target_seg} 归档超时(LSN {lsn})")


def force_switch_wal(store: Store) -> str:
    cfg = store.cfg
    r = psql_value(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
                   sql="SELECT pg_switch_wal()::text;", dbname=DB_NAME)
    return r


# ---------------------------------------------------------------- 误操作
def run_mishap(store: Store) -> dict:
    """目标标记之后的可辨认误删除 + 正常新增(后续恢复应排除这些事务)。"""
    cfg = store.cfg
    before = current_lsn(cfg, cfg.source_socket, cfg.source_port)
    sql = """
BEGIN;
DELETE FROM pitrdrill_child WHERE parent_id IN (3, 7);
DELETE FROM pitrdrill_parent WHERE id IN (3, 7);
UPDATE pitrdrill_parent SET version = version + 999,
       payload = 'MISHAP-CORRUPTED', updated_at = clock_timestamp()
WHERE id = 5;
INSERT INTO pitrdrill_parent(id, name, payload)
VALUES (9001, 'post-target-normal-row', 'inserted-after-target');
INSERT INTO pitrdrill_event_log(event_seq, kind, marker, detail)
VALUES (nextval('pitrdrill_event_seq'), 'mishap', NULL,
        '{"op":"delete_parent_3_7","corrupt_parent_5":true,"insert_9001":true}'::jsonb);
COMMIT;
SELECT pg_current_wal_lsn()::text;
"""
    r = psql(cfg, socket_dir=cfg.source_socket, port=cfg.source_port,
             sql=sql, dbname=DB_NAME)
    if not r.ok:
        raise DrillError(f"误操作事务失败: {r.stderr}")
    after = r.stdout.strip().splitlines()[-1]
    force_switch_wal(store)
    _wait_archive_caught_up(cfg, after)
    rec = store.add_event("mishap", before_lsn=before, after_lsn=after,
                          deleted_parents=[3, 7],
                          corrupted_parent=5, inserted_parent=9001)
    return rec


# ---------------------------------------------------------------- 持续写入器
WRITER_TICK_SQL = (
    "INSERT INTO pitrdrill_child(parent_id, tick, note) "
    "VALUES (({pid}), {tick}, 'heartbeat') RETURNING id;"
)
WRITER_UPDATE_SQL = (
    "UPDATE pitrdrill_parent SET version = version + 1, "
    "updated_at = clock_timestamp(), payload = 'tick-{tick}' WHERE id = {pid};"
)
WRITER_DELETE_SQL = (
    "WITH old AS (DELETE FROM pitrdrill_child WHERE ctid IN ("
    "SELECT ctid FROM pitrdrill_child WHERE parent_id = {pid} "
    "ORDER BY id DESC OFFSET 20) RETURNING 1) SELECT count(*) FROM old;"
)


class HeartbeatWriter:
    """长驻 psql 会话, 持续 INSERT/UPDATE/DELETE, 事件落 JSONL 见证文件。"""

    def __init__(self, cfg: DrillConfig, event_file: Path,
                 tick_seconds: float = 0.5,
                 valid_parent_ids: list[int] | None = None):
        self.cfg = cfg
        self.event_file = event_file
        self.tick_seconds = tick_seconds
        # 误操作会删除部分父表, 写入器只能引用仍存在的父表, 否则触发外键错误
        self.valid_parent_ids = valid_parent_ids or list(range(1, 11))
        self.proc: subprocess.Popen | None = None
        self.tick = 0

    def set_valid_parents(self, ids: list[int]) -> None:
        self.valid_parent_ids = sorted(ids)

    def __enter__(self) -> "HeartbeatWriter":
        argv = [
            self.cfg.pgbin / "psql", "-v", "ON_ERROR_STOP=1", "-X", "-q",
            "-h", str(self.cfg.source_socket), "-p", str(self.cfg.source_port),
            "-U", "postgres", "-d", DB_NAME,
            "--set=PROMPT1=", "--set=PROMPT2=",
        ]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ, "LC_ALL": "C", "PGAPPNAME": APP_NAME},
        )
        self._send("\\set AUTOCOMMIT on")
        return self

    def _send(self, sql: str) -> str:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(sql + "\n")
        self.proc.stdin.write("\\echo @@TICK_DONE@@\n")
        self.proc.stdin.flush()
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise DrillError(
                    "写入器 psql 会话意外结束, 此前输出:\n" + "\n".join(lines[-10:]))
            line = line.rstrip("\n")
            if line.startswith("ERROR:") or line.startswith("FATAL:"):
                raise DrillError(
                    f"写入器 SQL 报错: {line}\nSQL: {sql}\n上下文: "
                    + "\n".join(lines[-5:]))
            if line == "@@TICK_DONE@@":
                return "\n".join(lines)
            lines.append(line)

    def beat(self) -> dict:
        self.tick += 1
        pid = self.valid_parent_ids[self.tick % len(self.valid_parent_ids)]
        ins = self._send(WRITER_TICK_SQL.format(pid=pid, tick=self.tick))
        child_id = ins.strip().splitlines()[0] if ins.strip() else ""
        self._send(WRITER_UPDATE_SQL.format(pid=pid, tick=self.tick))
        if self.tick % 5 == 0:
            self._send(WRITER_DELETE_SQL.format(pid=pid))  # 含删除行为
        lsn = self._send("SELECT pg_current_wal_lsn()::text;").strip()
        rec = {"ts": now_iso(), "tick": self.tick, "parent_id": pid,
               "child_id": int(child_id) if child_id.isdigit() else None,
               "lsn": lsn}
        append_jsonl(self.event_file, rec)
        return rec

    def run_for(self, seconds: float, on_beat=None) -> int:
        deadline = time.time() + seconds
        n = 0
        while time.time() < deadline:
            rec = self.beat()
            n += 1
            if on_beat:
                on_beat(rec)
            time.sleep(self.tick_seconds)
        return n

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write("\\q\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        self.proc = None

    def __exit__(self, *exc) -> None:
        self.stop()


# ---------------------------------------------------------------- 物理基础备份
def take_base_backup(store: Store, name: str, note: str) -> dict:
    """pg_basebackup 真实物理基础备份, 流式带上备份所需 WAL(-X stream)。"""
    cfg = store.cfg
    bdir = cfg.backup_dir(name)
    if bdir.exists():
        raise DrillError(f"备份目录已存在: {bdir}")
    label = f"{TASK_TAG}-{name}"
    argv = [
        cfg.pgbin / "pg_basebackup",
        "-h", str(cfg.source_socket), "-p", str(cfg.source_port),
        "-U", "postgres", "-D", bdir,
        "-F", "p", "-X", "stream", "-c", "fast",
        "-l", label, "--manifest-checksums=sha256",
        "--no-password",
    ]
    # 部分挂载点对批量 fchmod 偶发 EACCES, 清理半成品后有限重试
    last_err = ""
    for attempt in range(3):
        if bdir.exists():
            from .util import robust_rmtree
            robust_rmtree(bdir)
        bdir.mkdir(parents=True)
        r = run(argv, timeout=300)
        store.log_command(r.argv, r.rc, f"pg_basebackup attempt={attempt + 1}")
        if r.ok:
            break
        last_err = f"{r.stderr}\n{r.stdout}"
        if "Permission denied" not in last_err or attempt == 2:
            raise DrillError(f"pg_basebackup 失败: {last_err}")
        time.sleep(1.0)
    else:
        raise DrillError(f"pg_basebackup 多次重试仍失败: {last_err}")

    label_path = bdir / "backup_label"
    if not label_path.exists():
        raise DrillError("备份缺少 backup_label(不是有效的在线基础备份)")
    label_text = label_path.read_text()
    info = _parse_backup_label(label_text)

    r = run([cfg.pgbin / "pg_controldata", bdir])
    if r.ok:
        from .util import parse_controldata
        ctrl = parse_controldata(r.stdout)
    else:
        ctrl = {}
    backup_wal = sorted(p.name for p in (bdir / "pg_wal").iterdir()
                        if len(p.name) == 24) if (bdir / "pg_wal").exists() else []
    manifest = bdir / "backup_manifest"
    manifest_sha = sha256_file(manifest) if manifest.exists() else None

    rec = {
        "name": name, "note": note, "label": label,
        "path": str(bdir), "created_at": now_iso(),
        "backup_label": info,
        "start_lsn": info.get("START WAL LOCATION", "").split(" ")[0],
        "checkpoint_lsn": info.get("CHECKPOINT LOCATION", ""),
        "backup_timeline": int(info.get("START TIMELINE", "1")),
        "system_identifier": ctrl.get("Database system identifier", ""),
        "backup_wal_segments": backup_wal,
        "manifest_sha256": manifest_sha,
        "source_port": cfg.source_port,
    }
    # pg_verifybackup: 备份自身完整性
    vr = run([cfg.pgbin / "pg_verifybackup", bdir], timeout=120)
    rec["verifybackup_rc"] = vr.rc
    rec["verifybackup_output"] = (vr.stdout + vr.stderr).strip()
    store.log_command(vr.argv, vr.rc, "pg_verifybackup")

    store.state.backups[name] = rec
    store.add_event("base_backup", name=name, **{
        k: rec[k] for k in ("start_lsn", "checkpoint_lsn", "backup_timeline",
                           "system_identifier")})
    store.save()
    return rec


def _parse_backup_label(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def verify_backup_integrity(store: Store, name: str) -> dict:
    cfg = store.cfg
    bdir = cfg.backup_dir(name)
    if not bdir.exists():
        raise DrillError(f"备份不存在: {name}")
    vr = run([cfg.pgbin / "pg_verifybackup", bdir], timeout=120)
    return {"rc": vr.rc, "output": (vr.stdout + vr.stderr).strip()}
