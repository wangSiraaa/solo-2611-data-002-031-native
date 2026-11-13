"""aiohttp HTTP 层：路由、逐帧 NDJSON 流、传输错误捕获与优雅停止。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

from aiohttp import ClientConnectionError, ServerDisconnectedError, web

from . import catalog as catalog_mod
from .config import Config
from .engine import (
    REJECTED_BAD_PARAMS,
    REJECTED_QUEUE_FULL,
    REJECTED_SHUTDOWN,
    Engine,
)
from .events import EventLogger
from .frames import T_CLIENT_DISCONNECTED, T_SEND_FAILED
from .monitor import Monitor
from .pool import BoundedPool, PoolClosed, PoolQueueFull
from .registry import QUEUED, Registry, RequestRecord

routes = web.RouteTableDef()


@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    return web.json_response({"ok": True, "closing": engine.closing})


@routes.get("/catalog")
async def get_catalog(request: web.Request) -> web.Response:
    return web.json_response({"queries": catalog_mod.catalog_snapshot()})


@routes.post("/query")
async def post_query(request: web.Request) -> web.StreamResponse:
    engine: Engine = request.app["engine"]
    registry: Registry = request.app["registry"]
    logger: EventLogger = request.app["logger"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="请求体必须是 JSON")
    query_id = body.get("query_id")
    if not isinstance(query_id, str):
        raise web.HTTPBadRequest(text="缺少字符串类型的 query_id")
    try:
        entry = catalog_mod.get(query_id)
    except KeyError:
        raise web.HTTPNotFound(text=f"未知 query_id: {query_id}")

    record = RequestRecord(
        request_id=uuid.uuid4().hex,
        query_id=query_id,
        params=body.get("params") or {},
        created_wall=time.time(),
        state=QUEUED,
    )
    registry.add(record)
    try:
        ctx = engine.build_context(record, entry, body.get("params") or {})
    except PoolQueueFull as exc:
        record.finish(REJECTED_QUEUE_FULL, detail=str(exc))
        raise web.HTTPServiceUnavailable(
            text='{"error":"pool_queue_full","detail":"连接池与等待队列均已满"}',
            content_type="application/json",
        )
    except PoolClosed as exc:
        record.finish(REJECTED_SHUTDOWN, detail=str(exc))
        raise web.HTTPServiceUnavailable(
            text='{"error":"shutting_down","detail":"服务停止中，拒绝新请求"}',
            content_type="application/json",
        )
    except ValueError as exc:
        record.finish(REJECTED_BAD_PARAMS, detail=str(exc))
        raise web.HTTPBadRequest(
            text=f'{{"error":"bad_params","detail":"{str(exc)}"}}',
            content_type="application/json",
        )

    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store"},
    )
    # 先尝试取出第一帧：若池满/池关闭，路由阶段就能回 JSON 错误而不是已发送的流
    stream = engine.stream(ctx)
    try:
        first = await stream.__anext__()
    except PoolQueueFull:
        await stream.aclose()
        raise web.HTTPServiceUnavailable(
            text='{"error":"pool_queue_full","detail":"连接池与等待队列均已满"}',
            content_type="application/json",
        )
    except PoolClosed:
        await stream.aclose()
        raise web.HTTPServiceUnavailable(
            text='{"error":"shutting_down","detail":"服务停止中，拒绝新请求"}',
            content_type="application/json",
        )
    except StopAsyncIteration:
        await stream.aclose()
        raise web.HTTPInternalServerError(text="空响应")

    await response.prepare(request)
    try:
        await response.write(first)
        async for payload in stream:
            await response.write(payload)
    except (ConnectionResetError, BrokenPipeError, ClientConnectionError, ServerDisconnectedError) as exc:
        engine.notify_transport_failure(record.request_id, T_CLIENT_DISCONNECTED)
        logger.event("client_disconnected", request_id=record.request_id, error=type(exc).__name__)
    except Exception as exc:  # 其它网络发送失败也要触发同一条清理路径
        engine.notify_transport_failure(record.request_id, T_SEND_FAILED)
        logger.event("send_failed", request_id=record.request_id, error=repr(exc))
    finally:
        await stream.aclose()
        with contextlib.suppress(Exception):
            await response.write_eof()
    return response


@routes.get("/status")
async def get_status(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    return web.json_response(await engine.status())


@routes.get("/requests/{request_id}/status")
async def get_request_status(request: web.Request) -> web.Response:
    registry: Registry = request.app["registry"]
    rec = registry.get(request.match_info["request_id"])
    if rec is None:
        raise web.HTTPNotFound(text="未知 request_id")
    return web.json_response(rec.snapshot())


@routes.post("/requests/{request_id}/cancel")
async def cancel_request(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    result = await engine.cancel_request(request.match_info["request_id"])
    if not result.get("found"):
        raise web.HTTPNotFound(text="未知 request_id")
    return web.json_response(result)


def build_app(config: Config, logger: EventLogger) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app["config"] = config
    app["logger"] = logger
    app["registry"] = Registry()
    app["pool"] = BoundedPool(config, logger)
    app["monitor"] = Monitor(config, logger)
    app["engine"] = Engine(
        config=config, pool=app["pool"], monitor=app["monitor"], registry=app["registry"], logger=logger
    )
    app.add_routes(routes)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _on_startup(app: web.Application) -> None:
    await app["monitor"].start()
    await app["pool"].start()


async def _on_cleanup(app: web.Application) -> None:
    engine: Engine = app["engine"]
    await engine.shutdown()


async def run_server(config: Config) -> None:
    # 不同实例（演示会同时拉起多个网关）用可区分的 application_name，
    # 便于在 pg_stat_activity 中按实例核对 backend 清理情况
    if config.app_name == "qgateway":
        config.app_name = f"qgateway-p{config.port}"
    logger = EventLogger(config.log_path, enabled=config.log_events)
    app = build_app(config, logger)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port, backlog=128)
    await site.start()
    logger.event("http_listening", host=config.host, port=config.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(__import__("signal"), sig), stop_event.set)

    try:
        await stop_event.wait()
        logger.event("signal_received")
    finally:
        await runner.cleanup()
