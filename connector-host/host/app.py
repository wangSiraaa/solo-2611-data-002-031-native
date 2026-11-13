# -*- coding: utf-8 -*-
"""数据连接器宿主应用。

入口（见 host/__main__.py）：
    python -m host --config config/host.json list-sources
    python -m host --config config/host.json query --connector fast --source fast_orders ...
    python -m host --config config/host.json cancel  --query-id q-xxxx
    python -m host --config config/host.json status
    python -m host --config config/host.json restart --connector chaos
    python -m host --config config/host.json export --out runs/manifest.json
    python -m host --config config/host.json shell        # 交互式 REPL

宿主只编排连接器，不直接读连接器的数据；所有结果经协议逐批核对后落盘。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Dict, List, Optional

from .storage import Storage
from .supervisor import ConnectorSupervisor, Metrics


class HostError(Exception):
    pass


def _new_query_id() -> str:
    return "q-" + uuid.uuid4().hex[:12]


class HostApp:
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        # 项目根：显式环境变量优先（演示会把配置写到 runs/ 下的临时目录），
        # 否则取配置文件上两级（标准布局 config/host.json）。
        self.base_dir = os.environ.get("CONNECTOR_HOST_ROOT") or (
            os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
        )
        base = self.base_dir
        self.logs_dir = os.path.join(base, self.config.get("logs_dir", "logs"))
        self.results_dir = os.path.join(base, self.config.get("results_dir", "runs/results"))
        db_path = os.path.join(base, self.config.get("db_path", "runs/host.db"))
        self.storage = Storage(db_path, self.results_dir)
        self.metrics = Metrics()
        self.supervisors: Dict[str, ConnectorSupervisor] = {}
        self._query_tasks: Dict[str, asyncio.Task] = {}
        self._query_owner: Dict[str, str] = {}
        self._shutdown = asyncio.Event()

    def _resolve_command(self, entry: dict) -> list[str]:
        cmd = list(entry["command"])
        if "/" not in cmd[0]:
            cmd[0] = os.path.join(self.base_dir, cmd[0])
        return cmd

    def _resolve_dir(self, path: str) -> str:
        """配置中的相对路径一律相对宿主项目根目录，而不是运行目录。"""
        return path if os.path.isabs(path) else os.path.join(self.base_dir, path)

    async def start_all(self) -> None:
        for name, entry in self.config["connectors"].items():
            cfg = {
                "command": self._resolve_command(entry),
                "data_dir": self._resolve_dir(entry.get("data_dir", "data")),
                "env": entry.get("env", {}),
                "max_inflight": int(entry["limits"]["max_inflight"]),
                "max_message_bytes": int(entry["limits"]["max_message_bytes"]),
                "wait_timeout_s": float(entry["limits"]["wait_timeout_s"]),
                "data_cap_bytes": int(entry["limits"]["data_cap_bytes"]),
                "stderr_keep_lines": int(entry["limits"].get("stderr_keep_lines", 200)),
                "stderr_log_max_bytes": int(entry["limits"].get("stderr_log_max_bytes", 262144)),
                "cancel_grace_s": float(entry["limits"].get("cancel_grace_s", 5.0)),
            }
            sup = ConnectorSupervisor(
                name=name,
                config=cfg,
                storage=self.storage,
                metrics=self.metrics,
                logs_dir=self.logs_dir,
                results_dir=self.results_dir,
                emit=self._emit,
            )
            self.supervisors[name] = sup
            try:
                await sup.start()
            except Exception as exc:
                # 一个连接器起不来，不阻止其他连接器服务。
                await self._emit("error", f"[{name}] start failed: {exc!r}")

    async def _emit(self, level: str, message: str) -> None:
        if self.config.get("quiet"):
            return
        # 诊断事件走 stderr，保证 stdout 只输出可被 json.load 解析的结果。
        import sys
        print(f"[{level}] {message}", file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- 命令
    async def list_sources(self, connector: Optional[str] = None) -> dict:
        out: Dict[str, dict] = {}
        for name, sup in self.supervisors.items():
            if connector and name != connector:
                continue
            if sup.capability is None:
                out[name] = {"state": sup.state, "terminate_reason": sup.terminate_reason}
                continue
            out[name] = {
                "state": sup.state,
                "capabilities_version": sup.capability.get("capabilitiesVersion"),
                "supports_cancel": sup.capability.get("supportsCancel"),
                "sources": sup.capability.get("sources"),
                "filter_ops": sup.capability.get("filterOps"),
            }
        return out

    async def submit_query(
        self,
        connector: str,
        source: str,
        fields: Optional[List[str]] = None,
        filters: Optional[dict] = None,
        wait: bool = True,
    ) -> dict:
        sup = self.supervisors.get(connector)
        if sup is None:
            raise HostError(f"unknown connector {connector!r}")
        if sup.state != "alive":
            raise HostError(
                f"connector {connector} state={sup.state} "
                f"(reason={sup.terminate_reason}); use restart before retrying"
            )
        query_id = _new_query_id()
        task = asyncio.create_task(
            sup.submit_query(query_id, source, fields or [], filters or {})
        )
        self._query_tasks[query_id] = task
        self._query_owner[query_id] = connector
        if not wait:
            return {"query_id": query_id, "connector": connector, "running": True}

        try:
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "query_id": query_id,
                "connector": connector,
                "status": "failed",
                "fail_reason": str(exc),
                "terminal_state": (self.storage.get_query(query_id) or {}).get("terminal_state"),
            }
        return {"connector": connector, **result}

    async def cancel_query(self, query_id: str) -> dict:
        connector = self._query_owner.get(query_id)
        if connector is None:
            # 可能是历史查询（进程仍在但任务表已回收的情况不会出现，这里查库给出诊断）。
            row = self.storage.get_query(query_id)
            raise HostError(
                f"no live query {query_id}" + (f"; stored status={row['status']}" if row else "")
            )
        sup = self.supervisors[connector]
        note = await sup.cancel_query(query_id)
        task = self._query_tasks.get(query_id)
        outcome: Optional[dict] = None
        if task is not None:
            try:
                outcome = await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
            except asyncio.TimeoutError:
                outcome = {"note": "terminal response still pending; timeout policy will apply"}
            except Exception as exc:
                row = self.storage.get_query(query_id) or {}
                outcome = {
                    "status": row.get("status", "failed"),
                    "terminal_state": row.get("terminal_state"),
                    "fail_reason": str(exc),
                }
        return {"query_id": query_id, "connector": connector, "note": note, "outcome": outcome}

    async def status(self) -> dict:
        return {
            "connectors": {name: sup.status() for name, sup in self.supervisors.items()},
            "metrics": self.metrics.snapshot(),
            "queries": self.storage.list_queries(),
        }

    async def restart(self, connector: str) -> dict:
        sup = self.supervisors.get(connector)
        if sup is None:
            raise HostError(f"unknown connector {connector!r}")
        await sup.restart()
        return {"connector": connector, "state": sup.state, "capability": sup.capability}

    async def export(self, out_path: str) -> dict:
        if not os.path.isabs(out_path):
            out_path = os.path.join(self.base_dir, out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        manifest = self.storage.export_manifest(out_path)
        return {"manifest": os.path.abspath(out_path), **{
            k: manifest[k] for k in ("query_count", "exported_at")
        }}

    async def wait_queries(self, timeout: Optional[float] = None) -> None:
        """等待当前已提交的全部查询任务结束（成功或失败）。"""
        pending = [t for t in self._query_tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def shutdown(self) -> None:
        # 取消宿主侧查询等待任务（不重发任何请求），然后终止并回收全部子进程。
        for task in self._query_tasks.values():
            if not task.done():
                task.cancel()
        for sup in self.supervisors.values():
            await sup.terminate("host shutdown")
        # 确认所有子进程已回收。
        for name, sup in self.supervisors.items():
            proc = sup.proc
            if proc is not None and proc.returncode is None:
                await proc.wait()
        self.storage.close()
        await self._emit("info", "all child pipes closed and processes reaped")
