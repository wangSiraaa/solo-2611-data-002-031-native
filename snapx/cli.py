"""snapx command line interface."""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

from . import PROJECT_ROOT, db
from . import exporter, generate, inspect_cmd, verify, worker, writer

PG_BIN_CANDIDATES = ["/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/*/bin"]


def _pg_bin() -> str:
    for cand in PG_BIN_CANDIDATES:
        for path in sorted(glob.glob(cand), reverse=True):
            if os.path.exists(os.path.join(path, "initdb")):
                return path
    return ""  # rely on PATH


def _run_pg(cmd: list, **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    bindir = _pg_bin()
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return subprocess.run(cmd, env=env, **kw)


def cmd_dbstart(args: argparse.Namespace) -> int:
    data = os.environ.get("SNAPX_PGDATA", os.path.join(PROJECT_ROOT, "pgsql-data"))
    port = os.environ.get("SNAPX_PGPORT", db.DEFAULT_PORT)
    log = os.path.join(PROJECT_ROOT, "pgsql.log")
    os.makedirs(data, exist_ok=True)
    if not os.path.exists(os.path.join(data, "PG_VERSION")):
        print(f"dbstart: initdb in {data}")
        r = _run_pg(["initdb", "-D", data, "-U", db.DEFAULT_USER, "-A", "trust", "-E", "UTF8"],
                    capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr)
            return 1
    sock = os.environ.get("SNAPX_PGSOCK", "/tmp")
    r = _run_pg(["pg_ctl", "-D", data, "-l", log, "-w", "-t", "60",
                 "-o", f"-p {port} -c listen_addresses=127.0.0.1 "
                       f"-c unix_socket_directories={sock}", "start"],
                capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and "already running" not in out:
        print(out)
        return 1
    for _ in range(100):
        if _run_pg(["pg_isready", "-h", "127.0.0.1", "-p", port],
                   capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    # create the database if missing
    check = _run_pg(["psql", "-h", "127.0.0.1", "-p", port, "-U", db.DEFAULT_USER,
                     "-d", "postgres", "-tAc",
                     f"SELECT 1 FROM pg_database WHERE datname='{db.DEFAULT_DB}'"],
                    capture_output=True, text=True)
    if check.stdout.strip() != "1":
        _run_pg(["createdb", "-h", "127.0.0.1", "-p", port, "-U", db.DEFAULT_USER,
                 db.DEFAULT_DB], capture_output=True, text=True)
    print(f"dbstart: PostgreSQL ready on 127.0.0.1:{port} (db={db.DEFAULT_DB}, "
          f"data={data}, log={log})")
    return 0


def cmd_dbstop(args: argparse.Namespace) -> int:
    data = os.environ.get("SNAPX_PGDATA", os.path.join(PROJECT_ROOT, "pgsql-data"))
    r = _run_pg(["pg_ctl", "-D", data, "-m", "fast", "stop"],
                capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    return r.returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="snapx",
                                description="consistent snapshot export tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dbstart", help="init/start the local standalone PostgreSQL")
    sub.add_parser("dbstop", help="stop the local PostgreSQL")

    g = sub.add_parser("generate", help="reset schema and load synthetic data")
    g.add_argument("--orders", type=int, default=240)
    g.add_argument("--seed", type=int, default=42)

    w = sub.add_parser("writer", help="synthetic concurrent writer")
    mode = w.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    w.add_argument("--interval", type=float, default=0.5)
    w.add_argument("--count", type=int, default=100)
    w.add_argument("--seed", type=int, default=None)
    w.add_argument("--gate-dir", default=None,
                   help="hold COMMIT until <gate>/commit.allow appears")
    w.add_argument("--gate-timeout", type=float, default=180.0)

    e = sub.add_parser("export", help="coordinator: export a consistent snapshot")
    e.add_argument("--export-id", required=True)
    e.add_argument("--shards", type=int, required=True)
    e.add_argument("--barrier-demo", action="store_true",
                   help="orchestrate gated writer + A/B worker groups")
    e.add_argument("--writer-seed", type=int, default=2026)
    e.add_argument("--gate-timeout", type=float, default=180.0)
    e.add_argument("--barrier-timeout", type=float, default=120.0)
    e.add_argument("--pause-after-acquire", type=float, default=0.0)
    e.add_argument("--max-shard-retries", type=int, default=3)
    e.add_argument("--baseline-per-shard", type=int, default=2)

    i = sub.add_parser("inspect", help="show export state")
    i.add_argument("--export-id", default=None)
    i.add_argument("--json", action="store_true")

    v = sub.add_parser("verify", help="re-check a published export")
    v.add_argument("--export-id", required=True)
    v.add_argument("--no-db", action="store_true")

    wk = sub.add_parser("_worker", help=argparse.SUPPRESS)
    wk.add_argument("--snapshot", required=True)
    wk.add_argument("--shard", type=int, required=True)
    wk.add_argument("--shards", type=int, required=True)
    wk.add_argument("--out", required=True)
    wk.add_argument("--control", required=True)
    wk.add_argument("--group", choices=["A", "B"], default="A")
    wk.add_argument("--coordinator-pid", type=int, default=0)
    wk.add_argument("--try", dest="try_no", type=int, default=1)
    wk.add_argument("--barrier-timeout", type=float, default=120.0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "dbstart": cmd_dbstart, "dbstop": cmd_dbstop,
        "generate": generate.cmd_generate, "writer": writer.cmd_writer,
        "export": exporter.cmd_export, "inspect": inspect_cmd.cmd_inspect,
        "verify": verify.cmd_verify, "_worker": worker.cmd_worker,
    }
    return handlers[args.cmd](args)
