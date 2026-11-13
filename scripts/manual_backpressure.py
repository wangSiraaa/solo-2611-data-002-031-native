"""确定性背压验证：消费者先暂停读取，让有界队列填满后再恢复。

证明三点：
1. 生产者在队列满时进入 WAITING_FOR_CLIENT 并停止 fetchmany（fetch_calls 暂停增长）；
2. 在途帧/字节峰值被 frame_queue_limit 限定，不会随结果集增长而全量物化；
3. 恢复读取后能正常读到 success 终态。
"""
import asyncio
import json
import sys

import aiohttp

BASE = "http://127.0.0.1:8080"


async def main() -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{BASE}/query",
            json={"query_id": "large_scan", "params": {"start_id": 1, "end_id": 120000}},
        ) as resp:
            rid = None
            rows = 0
            terminal = None
            stall_done = False
            async for raw in resp.content:
                frame = json.loads(raw)
                if frame["type"] == "meta":
                    rid = frame["request_id"]
                elif frame["type"] == "rows":
                    rows += len(frame["rows"])
                    if not stall_done and rows >= 800:
                        # 停止读取 2.5 秒：生产者应被有界队列阻塞
                        await asyncio.sleep(2.5)
                        async with s.get(f"{BASE}/requests/{rid}/status") as st:
                            stalled = await st.json()
                        print("暂停消费期间:")
                        print("  state              =", stalled["state"])
                        print("  inflight_frames    =", stalled["inflight_frames"])
                        print("  peak_inflight_frames =", stalled["peak_inflight_frames"])
                        print("  peak_inflight_bytes  =", stalled["peak_inflight_bytes"])
                        print("  fetch_calls        =", stalled["fetch_calls"], "(已暂停增长)")
                        # 再等 1 秒确认 fetch_calls 不再增长（生产者确实停拉）
                        await asyncio.sleep(1.0)
                        async with s.get(f"{BASE}/requests/{rid}/status") as st:
                            stalled2 = await st.json()
                        print("  1 秒后 fetch_calls =", stalled2["fetch_calls"],
                              "(应等于", stalled["fetch_calls"], ")")
                        stall_done = True
                        stalled_state_ok = (
                            stalled["state"] == "WAITING_FOR_CLIENT"
                            and stalled2["fetch_calls"] == stalled["fetch_calls"]
                        )
                elif frame["type"] == "terminal":
                    terminal = frame
            async with s.get(f"{BASE}/requests/{rid}/status") as st:
                final = await st.json()
            print("\n恢复读取后:")
            print("  rows               =", rows)
            print("  terminal           =", terminal["status"])
            print("  total fetch_calls  =", final["fetch_calls"])
            print("  peak_inflight_frames =", final["peak_inflight_frames"])
            print("  peak_inflight_bytes  =", final["peak_inflight_bytes"])
            ok = (
                stalled_state_ok
                and terminal["status"] == "success"
                and rows == 120000
                and final["peak_inflight_frames"] <= 8
            )
            print("\n背压验证:", "PASS" if ok else "FAIL")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
