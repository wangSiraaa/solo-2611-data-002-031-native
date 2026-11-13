"""手工取消验证脚本（也可作为 API 用法示例）。"""
import asyncio
import json
import sys

import aiohttp

from qgateway.cli import _stream_query

BASE = "http://127.0.0.1:8080"


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(
            _stream_query(
                BASE,
                "slow_read",
                {"sleep_seconds": 30, "start_id": 1, "end_id": 100000},
                print_rows=False,
            )
        )
        request_id = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with session.get(f"{BASE}/status") as resp:
                status = await resp.json()
            active = [r for r in status["requests"] if r["query_id"] == "slow_read" and r["active"]]
            if active:
                request_id = active[0]["request_id"]
                backend_pid = active[0]["backend_pid"]
                break
        print(f"捕获在途请求 request_id={request_id} backend_pid={backend_pid}")
        async with session.post(f"{BASE}/requests/{request_id}/cancel") as resp:
            print("取消接口响应:", json.dumps(await resp.json(), ensure_ascii=False))
        summary = await task
        print("客户端终态:", summary["terminal"]["status"] if summary["terminal"] else None)
        print("客户端完成标记:", summary["completed"])
        # 等待服务状态落终态
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with session.get(f"{BASE}/requests/{request_id}/status") as resp:
                req = await resp.json()
            if req["terminal_reason"]:
                break
        print("服务终态:", req["terminal_reason"], "| 终态已送达:", req["terminal_delivered"])
        return 0 if req["terminal_reason"] == "cancelled" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
