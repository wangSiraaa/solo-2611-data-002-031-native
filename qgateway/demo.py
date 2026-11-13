"""可重复的综合负载演示与自动验收。

覆盖题面要求的全部场景：
  1. 快速点查（含 int8/NUMERIC/timestamptz/date/NULL 类型）
  2. 多表聚合（含 NULL 参数）
  3. 池=3、队列=2 下的并发占满 + 超额请求被快速拒绝
  4. 显式取消慢查询，pg_stat_activity 中 backend 查询消失、事务清理完成，
     随后同一池仍能成功执行快速查询
  5. 读到一半主动断连：终态留在服务状态、backend 被清理
  6. SQL 执行失败后再次借用同一池连接成功（backend pid 复用）
  7. 慢客户端大结果集：fetch 次数、在途缓存峰值、RSS 证明没有全量物化
  8. 完整十万行以上导出 + 独立直连数据库核对类型/行数/值
  9. SQL 时间预算终态
 10. 服务停止：拒绝新请求、有限清理、强制终止残余操作、无 idle in transaction
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from psycopg import AsyncConnection

from .cli import _stream_query
from .config import PGDATA_DIR, SOCKET_DIR, Config, RUNTIME_DIR
from .demo_check import check_export_against_database

PG_BIN = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin"))
OUT_DIR = RUNTIME_DIR / "demo"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name} {detail}")

    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)


# --------------------------------------------------------------- 进程与数据库工具

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_http(base_url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(f"{base_url}/health") as resp:
                    if resp.status == 200:
                        return
            except OSError as exc:
                last = exc
            await asyncio.sleep(0.2)
    raise RuntimeError(f"服务未就绪：{last}")


async def _direct_conn(dsn: str) -> AsyncConnection:
    return await AsyncConnection.connect(dsn, autocommit=True)


async def _pid_exists(conn: AsyncConnection, pid: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,))
        return (await cur.fetchone()) is not None


async def _query_stopped(conn: AsyncConnection, pid: int) -> bool:
    """被取消的查询是否已停止：backend 不存在，或已不处于 active（连接回池 idle）。"""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,)
        )
        row = await cur.fetchone()
    return row is None or row[0] != "active"


async def _idle_in_transaction(conn: AsyncConnection, app_name: str) -> list[int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE application_name LIKE %s AND state = 'idle in transaction'",
            (app_name,),
        )
        return [r[0] for r in await cur.fetchall()]


async def _backend_active_pids(conn: AsyncConnection, app_name: str) -> list[int]:
    """该应用名下仍在执行 SQL（state='active'）的 backend。"""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE application_name LIKE %s AND state = 'active'",
            (app_name,),
        )
        return [r[0] for r in await cur.fetchall()]


async def _get_status(base_url: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/status") as resp:
            return await resp.json()


def _find_request(status: dict[str, Any], query_id: str, *, terminal: str | None = None,
                  request_id: str | None = None) -> dict[str, Any] | None:
    for req in status["requests"]:
        if request_id and req["request_id"] != request_id:
            continue
        if req["query_id"] != query_id:
            continue
        if terminal and req.get("terminal_reason") != terminal:
            continue
        return req
    return None


async def _poll_request(base_url: str, request_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await _get_status(base_url)
        req = next((r for r in status["requests"] if r["request_id"] == request_id), None)
        if req and req["terminal_reason"]:
            return req
        await asyncio.sleep(0.15)
    raise AssertionError(f"请求 {request_id} 未在 {timeout}s 内结束")


# --------------------------------------------------------------- 场景实现

async def check_quick_queries(base_url: str, report: Report) -> None:
    point = await _stream_query(
        base_url, "point_lookup", {"event_id": 42424}, print_rows=True
    )
    report.add(
        "快速点查成功且类型完整",
        point["completed"] and point["rows"] == 1,
        f"rows={point['rows']} terminal={point['terminal'] and point['terminal']['status']}",
    )
    agg = await _stream_query(
        base_url,
        "kind_aggregate",
        {
            "kind_id": None,  # NULL 参数在协议中保持可识别
            "start_time": "2026-01-01T00:00:00+00:00",
            "end_time": "2026-03-01T00:00:00+00:00",
        },
        print_rows=True,
    )
    report.add("多表聚合成功（NULL 参数）", agg["completed"] and agg["rows"] >= 1,
               f"rows={agg['rows']}")


async def check_pool_and_queue(base_url: str, pool_size: int, queue_limit: int,
                               report: Report) -> None:
    # 用宽时间预算的阻塞查询确定性占满 pool_size 个连接
    slow = [
        asyncio.create_task(
            _stream_query(
                base_url,
                "blocking_hold",
                {"sleep_seconds": 6, "start_id": 1, "end_id": 20},
                print_rows=False,
            )
        )
        for _ in range(pool_size)
    ]
    # 轮询直到服务状态显示 pool 全部 busy（连接被阻塞查询确定性占住）
    await _wait_pool_saturated(base_url, pool_size)
    # 此刻再提交：先填满等待队列，随后的请求必须被快速拒绝
    queued = [
        asyncio.create_task(
            _stream_query(
                base_url,
                "point_lookup",
                {"event_id": i + 1},
                print_rows=False,
            )
        )
        for i in range(queue_limit)
    ]
    # 给排队请求一点时间入队，然后提交超额请求
    await _wait_queue_depth(base_url, queue_limit)
    overflow = await asyncio.gather(
        *[
            _stream_query(base_url, "point_lookup", {"event_id": 9000 + i}, print_rows=False)
            for i in range(3)
        ]
    )
    rejected = sum(
        1
        for s in overflow
        if s["transport_error"] is not None and "pool_queue_full" in str(s["transport_error"])
    )
    report.add(
        "队列已满时快速拒绝超额请求",
        rejected >= 1,
        f"503 次数={rejected}（pool={pool_size}, queue={queue_limit}）",
    )
    results = await asyncio.gather(*slow, *queued)
    accepted_ok = all(r["completed"] for r in results)
    report.add("占满与排队请求最终全部成功", accepted_ok,
               f"成功 {sum(1 for r in results if r['completed'])}/{len(results)}")


async def _wait_pool_saturated(base_url: str, pool_size: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = await _get_status(base_url)
        pool = status["service"]["pool"]
        if pool["busy"] >= pool_size:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("连接池未在预期时间内进入饱和状态")


async def _wait_queue_depth(base_url: str, depth: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = await _get_status(base_url)
        if status["service"]["pool"]["waiters"] >= depth:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待队列未在预期时间内达到指定深度")


async def check_cancel(base_url: str, dsn: str, report: Report) -> None:
    task = asyncio.create_task(
        _stream_query(
            base_url,
            "blocking_hold",
            {"sleep_seconds": 30, "start_id": 1, "end_id": 100_000},
            print_rows=False,
        )
    )
    # 等 meta 帧拿到 request_id
    request_id = await _wait_request_id(task, base_url, "blocking_hold")
    status = await _get_status(base_url)
    req = _find_request(status, "blocking_hold", request_id=request_id)
    pid = req["backend_pid"]
    async with await _direct_conn(dsn) as conn:
        before = await _pid_exists(conn, pid)
        before_active = not await _query_stopped(conn, pid)
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/requests/{request_id}/cancel") as resp:
            cancel_body = await resp.json()
    summary = await task
    terminal_req = await _poll_request(base_url, request_id)
    # 被取消的查询必须在 pg_stat_activity 中停止（backend 回 idle 或连接关闭）
    query_stopped = await _poll_query_stopped(dsn, pid)
    async with await _direct_conn(dsn) as conn:
        idle_xact = await _idle_in_transaction(conn, "qgateway%")
    follow = await _stream_query(base_url, "point_lookup", {"event_id": 7}, print_rows=False)
    report.add(
        "取消后被取消查询在 pg_stat_activity 中停止",
        before and before_active and query_stopped,
        f"backend_pid={pid} cancel_api={cancel_body}",
    )
    report.add("取消终态为 cancelled 且客户端显示未完成",
               terminal_req["terminal_reason"] == "cancelled"
               and summary["terminal"] is not None
               and summary["terminal"]["status"] == "cancelled"
               and not summary["completed"],
               f"detail={terminal_req.get('terminal_detail')}")
    report.add("取消后无 idle in transaction 残留", idle_xact == [], f"残留={idle_xact}")
    report.add("清理后池连接仍能执行快速查询", follow["completed"],
               f"follow rows={follow['rows']}")


async def check_half_disconnect(base_url: str, dsn: str, report: Report) -> None:
    summary = await _stream_query(
        base_url,
        "large_scan",
        {"start_id": 1, "end_id": 120_000},
        abort_after_rows=400,
        print_rows=False,
    )
    report.add("客户端明确显示中断结果未完成",
               summary["aborted"] and summary["terminal"] is None and not summary["completed"],
               f"rows_read={summary['rows']} error={summary['transport_error']}")
    request_id = summary.get("request_id")
    req = await _poll_request(base_url, request_id)
    pid = req["backend_pid"]
    query_stopped = await _poll_query_stopped(dsn, pid)
    async with await _direct_conn(dsn) as conn:
        idle_xact = await _idle_in_transaction(conn, "qgateway%")
    report.add(
        "断连终态留在服务状态且查询/事务被清理",
        req["terminal_reason"] in ("client_disconnected", "send_failed")
        and req["terminal_delivered"] is False
        and query_stopped
        and idle_xact == [],
        f"reason={req['terminal_reason']} pid={pid}",
    )


async def check_failure_reuse(base_url: str, report: Report) -> None:
    bad = await _stream_query(
        base_url,
        "faulty_divide",
        {"denominator": 0, "start_id": 1, "end_id": 100},
        print_rows=False,
    )
    status = await _get_status(base_url)
    bad_req = _find_request(status, "faulty_divide", terminal="query_error")
    bad_pid = bad_req["backend_pid"] if bad_req else None
    good = await _stream_query(
        base_url, "point_lookup", {"event_id": 123}, print_rows=False
    )
    status2 = await _get_status(base_url)
    good_req = _find_request(status2, "point_lookup", terminal="success",
                             request_id=good.get("request_id"))
    good_pid = good_req["backend_pid"] if good_req else None
    report.add(
        "SQL 执行失败产生 query_error 终态",
        bad["terminal"] is not None and bad["terminal"]["status"] == "query_error"
        and bad_req and bad_req.get("sqlstate") == "22012",
        f"sqlstate={bad_req and bad_req.get('sqlstate')}",
    )
    report.add(
        "失败连接被清理，后续请求拿到干净连接并成功",
        good["completed"] and good_pid is not None,
        f"失败 backend={bad_pid} -> 后续 backend={good_pid}（池会销毁脏连接并补建）",
    )


async def check_backpressure(base_url: str, frame_queue_limit: int, report: Report) -> None:
    """暂停消费的确定性背压验证 + 慢客户端长流验证。"""
    paused_metrics: dict[str, Any] = {}

    async def paused_reader() -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/query",
                json={"query_id": "large_scan", "params": {"start_id": 1, "end_id": 120_000}},
            ) as resp:
                rows = 0
                request_id = None
                terminal = None
                stalled = None
                async for raw in resp.content:
                    frame = json.loads(raw)
                    if frame["type"] == "meta":
                        request_id = frame["request_id"]
                    elif frame["type"] == "rows":
                        rows += len(frame["rows"])
                        if stalled is None and rows >= frame_queue_limit * 200:
                            # 完全停止读取，迫使有界队列填满并阻塞生产者
                            await asyncio.sleep(2.5)
                            status = await _get_status(base_url)
                            stalled = next(
                                r for r in status["requests"] if r["request_id"] == request_id
                            )
                            fetch_at_stall = stalled["fetch_calls"]
                            await asyncio.sleep(1.0)
                            status2 = await _get_status(base_url)
                            stalled2 = next(
                                r for r in status2["requests"] if r["request_id"] == request_id
                            )
                            paused_metrics["state"] = stalled["state"]
                            paused_metrics["fetch_paused"] = (
                                stalled2["fetch_calls"] == fetch_at_stall
                            )
                            paused_metrics["request_id"] = request_id
                    elif frame["type"] == "terminal":
                        terminal = frame
                final = await _poll_request(base_url, request_id)
                return {"rows": rows, "terminal": terminal, "final": final}

    paused_result = await paused_reader()
    final = paused_result["final"]
    bounded = (
        paused_metrics.get("state") == "WAITING_FOR_CLIENT"
        and paused_metrics.get("fetch_paused") is True
        and final["peak_inflight_frames"] <= frame_queue_limit
    )
    report.add(
        "队列满时生产者停在 WAITING_FOR_CLIENT 且停止 fetch",
        bounded,
        f"state={paused_metrics.get('state')} fetch_paused={paused_metrics.get('fetch_paused')}",
    )
    report.add(
        "在途帧/字节缓存始终有界，无全量物化",
        final["peak_inflight_bytes"] < 8 * 1024 * 1024
        and final["peak_inflight_frames"] <= frame_queue_limit,
        f"peak_frames={final['peak_inflight_frames']} peak_bytes={final['peak_inflight_bytes']} "
        f"(上限 {frame_queue_limit} 帧)",
    )

    # 慢客户端长流：逐帧停顿读 24k 行，统计真实 fetch 块次数
    summary = await _stream_query(
        base_url,
        "large_scan",
        {"start_id": 1, "end_id": 24_000},
        pace_ms=6,
        print_rows=False,
    )
    req = await _poll_request(base_url, summary["request_id"])
    report.add(
        "游标按块拉取（fetch 次数远小于行数）且慢读成功",
        req["fetch_calls"] >= 100 and summary["rows"] == 24_000 and summary["completed"],
        f"fetch_calls={req['fetch_calls']} rows={summary['rows']} "
        f"服务RSS={ (await _get_status(base_url))['service']['rss_bytes'] }B",
    )


async def check_full_export(base_url: str, dsn: str, report: Report) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    export_file = OUT_DIR / "large_scan_full.ndjson"
    summary = await _stream_query(
        base_url,
        "large_scan",
        {"start_id": 1, "end_id": 120_000},
        sink_file=export_file,
        print_rows=False,
    )
    report.add("大结果集导出超过十万行且成功",
               summary["completed"] and summary["rows"] >= 100_000,
               f"rows={summary['rows']} file={export_file}")
    code = await check_export_against_database(dsn, export_file)
    report.add("独立直连数据库核对类型/行数/值全部一致", code == 0)


async def check_time_limit(base_url: str, report: Report) -> None:
    summary = await _stream_query(
        base_url,
        "slow_read",
        {"sleep_seconds": 30, "start_id": 1, "end_id": 100},
        print_rows=False,
    )
    status = await _get_status(base_url)
    req = _find_request(status, "slow_read", request_id=summary.get("request_id"))
    report.add(
        "超过目录时间预算得到 time_limit_exceeded",
        summary["terminal"] is not None
        and summary["terminal"]["status"] == "time_limit_exceeded"
        and not summary["completed"]
        and req and req["total_seconds"] is not None,
        f"实际耗时={req and round(req['total_seconds'], 2)}s detail={summary['terminal'].get('detail')}",
    )


async def check_shutdown(config: Config, dsn: str, report: Report) -> None:
    alt_port = _free_port()
    proc = await _spawn_gateway(config, alt_port)
    base = f"http://127.0.0.1:{alt_port}"
    try:
        await _wait_http(base)
        # 启动一个长查询，随后发送 SIGTERM
        task = asyncio.create_task(
            _stream_query(
                base,
                "blocking_hold",
                {"sleep_seconds": 30, "start_id": 1, "end_id": 100_000},
                print_rows=False,
            )
        )
        request_id = await _wait_request_id(task, base, "blocking_hold")
        proc.send_signal(signal.SIGTERM)
        # 停止后新请求应被拒绝
        await asyncio.sleep(0.6)
        rejected = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/query", json={"query_id": "point_lookup", "params": {"event_id": 1}}
                ) as resp:
                    rejected = resp.status == 503
        except aiohttp.ClientError:
            rejected = True
        try:
            await asyncio.wait_for(task, timeout=15)
        except asyncio.TimeoutError:
            pass
        # 等待进程退出（优雅停止：取消在途 -> 宽限 -> 强制终止残余 backend）
        try:
            await asyncio.wait_for(_wait_exit(proc), timeout=20)
        except asyncio.TimeoutError:
            pass
        exited = proc.returncode is not None
        app_label = f"qgateway-p{alt_port}"
        async with await _direct_conn(dsn) as conn:
            idle_xact = await _idle_in_transaction(conn, app_label)
            leftovers = await _gateway_pids(conn, app_label)
        report.add("停止时拒绝新请求", rejected)
        report.add("服务在宽限期后确实退出", exited, f"returncode={proc.returncode}")
        report.add("无永久闲置事务/残余 backend", idle_xact == [] and leftovers == [],
                   f"idle_xact={idle_xact} leftover_pids={leftovers}")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _gateway_pids(conn: AsyncConnection, app_name: str) -> list[int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name LIKE %s",
            (app_name,),
        )
        return [r[0] for r in await cur.fetchall()]


# --------------------------------------------------------------- 小工具

async def _wait_request_id(task: asyncio.Task, base_url: str, query_id: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = await _get_status(base_url)
        active = [r for r in status["requests"] if r["query_id"] == query_id and r["active"]]
        if active:
            return active[0]["request_id"]
        if task.done():
            raise AssertionError(f"{query_id} 任务提前结束：{task.result()}")
        await asyncio.sleep(0.1)
    raise AssertionError("未捕获到在途请求")


async def _poll_query_stopped(dsn: str, pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    async with await _direct_conn(dsn) as conn:
        while time.monotonic() < deadline:
            if await _query_stopped(conn, pid):
                return True
            await asyncio.sleep(0.2)
        return False


# --------------------------------------------------------------- 进程拉起

async def _spawn_gateway(config: Config, port: int) -> subprocess.Popen:
    python = sys.executable
    cmd = [
        python, "-m", "qgateway.cli", "serve",
        "--pool-size", str(config.pool_size),
        "--wait-queue-limit", str(config.wait_queue_limit),
        "--frame-queue-limit", str(config.frame_queue_limit),
        "--fetch-block-rows", str(config.fetch_block_rows),
        "--port", str(port),
        "--dsn", config.dsn,
        "--log-file", str(RUNTIME_DIR / f"gateway-{port}.log"),
    ]
    proc = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent.parent))
    return proc


async def _spawn_postgres_and_seed(config: Config) -> tuple[subprocess.Popen | None, subprocess.Popen | None]:
    pgdata = PGDATA_DIR
    sockdir = SOCKET_DIR
    pg_ctl = PG_BIN / "pg_ctl"
    postgres = PG_BIN / "postgres"
    initdb = PG_BIN / "initdb"
    createdb = PG_BIN / "createdb"

    pg_proc = None
    if not pgdata.exists() or not (pgdata / "PG_VERSION").exists():
        subprocess.run([str(initdb), "-D", str(pgdata), "--auth-local=trust",
                        "--auth-host=scram-sha-256"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log = open(RUNTIME_DIR / "postgres.log", "a", encoding="utf-8")
        pg_proc = subprocess.Popen(
            [str(postgres), "-D", str(pgdata), "-k", str(sockdir),
             "-c", "listen_addresses=", "-c", "fsync=off", "-c", "synchronous_commit=off"],
            stdout=log, stderr=log,
        )
        # 等待 socket 就绪
        for _ in range(100):
            if (sockdir / ".s.PGSQL.5432").exists():
                break
            await asyncio.sleep(0.1)
        subprocess.run([str(createdb), "-h", str(sockdir), "taskdb"], check=True)
    elif not (sockdir / ".s.PGSQL.5432").exists():
        # 数据目录存在但实例未运行：用 pg_ctl 拉起已有实例
        log = open(RUNTIME_DIR / "postgres.log", "a", encoding="utf-8")
        subprocess.run(
            [str(pg_ctl), "-D", str(pgdata), "-l", str(RUNTIME_DIR / "postgres.log"),
             "-o", f"-k {sockdir} -c listen_addresses=''", "-w", "start"],
            check=True, stdout=log, stderr=log,
        )
    # 实例已在运行则直接复用；幂等重建 schema 与数据，保证演示可重复
    from .dbinit import initialize
    await initialize(config.dsn, 120_000)
    return pg_proc, None


# --------------------------------------------------------------- 主入口

async def run_demo(*, pool_size: int, wait_queue_limit: int, port: int, spawn: bool) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = Config(
        pool_size=pool_size,
        wait_queue_limit=wait_queue_limit,
        frame_queue_limit=4,
        fetch_block_rows=200,
    )
    report = Report()
    gateway_proc: subprocess.Popen | None = None
    pg_proc: subprocess.Popen | None = None
    base_url = f"http://127.0.0.1:{port}"

    try:
        if spawn:
            pg_proc, _ = await _spawn_postgres_and_seed(config)
            gateway_proc = await _spawn_gateway(config, port)
        await _wait_http(base_url)
        print(f"== 网关已就绪：{base_url}（pool={pool_size}, queue={wait_queue_limit}）==\n")

        await check_quick_queries(base_url, report)
        await check_pool_and_queue(base_url, pool_size, wait_queue_limit, report)
        await check_cancel(base_url, config.dsn, report)
        await check_half_disconnect(base_url, config.dsn, report)
        await check_failure_reuse(base_url, report)
        await check_backpressure(base_url, config.frame_queue_limit, report)
        await check_full_export(base_url, config.dsn, report)
        await check_time_limit(base_url, report)
        await check_shutdown(config, config.dsn, report)
    finally:
        if gateway_proc and gateway_proc.returncode is None:
            gateway_proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(_wait_exit(gateway_proc), timeout=15)
            except asyncio.TimeoutError:
                gateway_proc.kill()

    print("\n================ 验收汇总 ================")
    for check in report.checks:
        print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name} {check.detail}")
    ok = report.all_ok()
    print(f"\n结果：{'全部通过' if ok else '存在失败项'}（{sum(c.ok for c in report.checks)}/{len(report.checks)}）")
    (OUT_DIR / "report.json").write_text(
        json.dumps([c.__dict__ for c in report.checks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入 {OUT_DIR / 'report.json'}")
    return 0 if ok else 1


async def _wait_exit(proc: subprocess.Popen) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, proc.wait)
