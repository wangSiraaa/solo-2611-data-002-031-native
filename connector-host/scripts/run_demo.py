# -*- coding: utf-8 -*-
"""端到端合成演示（确定性）。

覆盖场景：
  1. 正常查询 + 过滤/投影/分批（fast）
  2. 交错数据批：同一连接器多个查询并发，批次交错到达（progress）
  3. 相反完成顺序（reorder，且该连接器不支持 cancel）
  4. stderr 洪水（chaos: stderr_flood，宿主持续排空不死锁）
  5. 消息拆成极小字节块（FRAME_CHUNK_BYTES=1，含半个中文字符）
  6. 未知响应 id（chaos: unknown_id -> 按偏移定位并终止该连接器）
  7. 声明长度过大（chaos: oversize -> 超限即终止，不缓冲主体）
  8. 子进程半途退出（chaos: half_exit -> 未完成请求全部明确失败）
  9. 取消碰撞（chaos: cancel_race -> 明确展示最终采用的终态）
 10. 不响应 cancel 的挂起连接器（hang -> 超时结束进程，只报告受影响请求）

每个故障场景之后都会对另外两个健康连接器再发查询，证明故障源被隔离。
结束后导出 manifest，用 ground_truth.py 独立比对真值，并输出在途峰值与 RSS。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from host.app import HostApp  # noqa: E402
from host.framing import FrameAssembler, encode_frame  # noqa: E402
from scripts.ground_truth import expected_records  # noqa: E402

BASE_CONFIG = os.path.join(ROOT, "config", "host.json")


# ------------------------------------------------------------ 分帧模糊测试
def framing_fuzz() -> dict:
    """不经过子进程，直接验证字节级分帧：1 字节块、半头、半个中文字符、粘连多帧。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "result": {"text": "中文边界测试-αβγ"}},
        {"jsonrpc": "2.0", "method": "progress", "params": {"percent": 50}},
        {"jsonrpc": "2.0", "id": 2, "result": {"nested": {"k": "数据"}}},
    ]
    stream = b"".join(encode_frame(m) for m in messages)
    for chunk_size in (1, 2, 3, 7, 64, 4096):
        asm = FrameAssembler(max_message_bytes=1 << 20)
        got = []
        for i in range(0, len(stream), chunk_size):
            got.extend(asm.feed(stream[i : i + chunk_size]))
        assert got == messages, f"fuzz failed at chunk_size={chunk_size}"
    # 声明长度超限必须在读取主体前报错。
    asm = FrameAssembler(max_message_bytes=128)
    bad = b"Content-Length: 999999\r\n\r\n" + b"x"
    try:
        asm.feed(bad)
        raise AssertionError("oversize declaration was not rejected")
    except Exception as exc:
        assert "exceeds limit" in str(exc)
    return {"scenario": "framing-fuzz", "chunk_sizes": [1, 2, 3, 7, 64, 4096],
            "stream_bytes": len(stream), "verdict": "pass",
            "note": "1 字节块必然切到半个中文字符（3 字节/字），全部正确重组"}


# ------------------------------------------------------------ 辅助
class Demo:
    def __init__(self, run_root: str):
        self.run_root = run_root
        self.results: list[dict] = []
        os.makedirs(run_root, exist_ok=True)
        with open(BASE_CONFIG, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.config["logs_dir"] = os.path.relpath(os.path.join(run_root, "logs"), ROOT)
        self.config["results_dir"] = os.path.relpath(os.path.join(run_root, "results"), ROOT)
        self.config["db_path"] = os.path.relpath(os.path.join(run_root, "host.db"), ROOT)
        # 数据目录锚定项目根，避免被相对到 run 目录。
        for entry in self.config["connectors"].values():
            entry["data_dir"] = "data"
        self.config["quiet"] = True
        self.config_path = os.path.join(run_root, "host.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False, indent=2)
        os.environ["CONNECTOR_HOST_ROOT"] = ROOT
        self.app = HostApp(self.config_path)

    def record(self, scenario: str, verdict: str, **detail) -> None:
        row = {"scenario": scenario, "verdict": verdict, **detail}
        self.results.append(row)
        print(f"[{verdict.upper()}] {scenario}: "
              f"{json.dumps({k: v for k, v in detail.items() if k != 'rows'}, ensure_ascii=False)[:220]}")

    async def query(self, connector: str, source: str, fields=None, filters=None, wait=True):
        return await self.app.submit_query(connector, source, fields or [], filters or {}, wait=wait)

    async def expect_ok(self, scenario: str, connector: str, source: str, fields, filters) -> dict:
        res = await self.query(connector, source, fields, filters)
        if res.get("status") != "succeeded":
            self.record(scenario, "fail", reason=res.get("fail_reason"), connector=connector)
            return res
        truth = expected_records(os.path.join(ROOT, "data"), source, fields, filters)
        row = self.app.storage.get_query(res["query_id"])
        checks = {
            "records_match_truth": res["records"] == len(truth),
            "batches_total": row["batches_total"],
            "records_total": row["records_total"],
            "terminal_state": row["terminal_state"],
        }
        verdict = "pass" if checks["records_match_truth"] else "fail"
        self.record(scenario, verdict, connector=connector, query_id=res["query_id"], **checks)
        return res

    async def expect_failure(self, scenario: str, connector: str, source: str,
                             reason_contains: str) -> None:
        try:
            res = await self.query(connector, source)
            # 宿主任务可能以失败 dict 返回。
            if res.get("status") == "failed":
                row = self.app.storage.get_query(res["query_id"])
                ok = reason_contains in (row["fail_reason"] or "")
                self.record(scenario, "pass" if ok else "fail",
                            connector=connector, query_id=res["query_id"],
                            fail_reason=row["fail_reason"], terminal_state=row["terminal_state"],
                            status=row["status"])
                return
            self.record(scenario, "fail", connector=connector, unexpected=res)
        except Exception as exc:
            # 连接器被终止时任务以异常结束；查库确认失败原因且未被标记为成功。
            await asyncio.sleep(0.05)
            rows = [q for q in self.app.storage.list_queries(connector)
                    if q["status"] in ("failed", "cancelled")]
            last = rows[-1] if rows else None
            ok = last is not None and reason_contains in (last["fail_reason"] or "")
            self.record(scenario, "pass" if ok else "fail", connector=connector,
                        exception=repr(exc)[:160],
                        stored_fail_reason=last["fail_reason"] if last else None,
                        stored_status=last["status"] if last else None,
                        note="未完成查询未保存为成功数据" if ok else None)

    async def set_chaos(self, modes: str, extra_env: dict | None = None) -> None:
        env = {"CHAOS_MODE": modes}
        if extra_env:
            env.update(extra_env)
        self.app.supervisors["chaos"].cfg["env"] = env
        await self.app.restart("chaos")

    async def isolation_probe(self, label: str) -> None:
        """故障之后：其他连接器必须照常完成。"""
        await self.expect_ok(
            f"isolation-after-{label}", "fast", "fast_orders",
            ["id", "name", "region"], {"region": {"eq": "华北"}},
        )
        await self.expect_ok(
            f"isolation-after-{label}", "progress", "progress_inventory",
            ["id", "category", "score"], {"score": {"ge": 80}},
        )


async def main() -> int:
    run_root = os.path.join(ROOT, "runs", time.strftime("demo-%Y%m%d-%H%M%S"))
    demo = Demo(run_root)
    await demo.app.start_all()
    try:
        # 0. 分帧字节级模糊测试
        fuzz = framing_fuzz()
        demo.record("framing-fuzz", fuzz["verdict"],
                    chunk_sizes=fuzz["chunk_sizes"], stream_bytes=fuzz["stream_bytes"],
                    note=fuzz["note"])

        # 1. 正常查询：过滤 + 投影 + 多批
        await demo.expect_ok(
            "01-normal-filtered", "fast", "fast_orders",
            ["id", "name", "region", "amount"],
            {"region": {"eq": "华北"}, "amount": {"gt": 100}},
        )

        # 2. 交错数据批：同一 progress 连接器同时跑 3 个查询，批次在流上交错
        interleaved_fields = ["id", "name"]
        interleaved_filters = [
            {"score": {"lt": 30}},
            {"score": {"ge": 70}},
            {"region": {"in": ["华北", "华东"]}},
        ]
        qs = await asyncio.gather(
            demo.query("progress", "progress_inventory", interleaved_fields, interleaved_filters[0]),
            demo.query("progress", "progress_inventory", interleaved_fields, interleaved_filters[1]),
            demo.query("progress", "progress_inventory", interleaved_fields, interleaved_filters[2]),
        )
        truths = [
            expected_records(os.path.join(ROOT, "data"), "progress_inventory",
                             interleaved_fields, f)
            for f in interleaved_filters
        ]
        ok = all(q.get("status") == "succeeded" and q["records"] == len(t)
                 for q, t in zip(qs, truths))
        demo.record("02-interleaved-batches", "pass" if ok else "fail",
                    query_ids=[q.get("query_id") for q in qs],
                    records=[q.get("records") for q in qs],
                    truth_records=[len(t) for t in truths],
                    note="批次交错但按 query_id+batch_no 归属，无串批")

        # 3. 相反完成顺序（reorder 不支持 cancel）
        t0 = time.monotonic()
        rq = await asyncio.gather(*[
            demo.query("reorder", "reorder_shipments", ["id"], {"score": {"gt": p * 10}})
            for p in range(6)
        ])
        elapsed = time.monotonic() - t0
        truth_n = [
            len(expected_records(os.path.join(ROOT, "data"), "reorder_shipments", ["id"],
                                 {"score": {"gt": p * 10}}))
            for p in range(6)
        ]
        # 结果必须与各自请求的过滤条件匹配，证明没有按发送顺序错配。
        ok = all(r.get("status") == "succeeded" and r["records"] == n
                 for r, n in zip(rq, truth_n))
        peak = demo.app.metrics.snapshot()["peak_inflight_by_connector"].get("reorder", 0)
        demo.record("03-reverse-completion", "pass" if ok else "fail",
                    inflight_peak=peak, elapsed_s=round(elapsed, 2),
                    records=[r.get("records") for r in rq], truth=truth_n,
                    supports_cancel=demo.app.supervisors["reorder"].capability["supportsCancel"])

        # 4. stderr 洪水
        await demo.set_chaos("stderr_flood")
        await demo.expect_ok("04-stderr-flood", "chaos", "chaos_lab",
                             ["id", "name"], {"category": {"eq": "订单"}})
        await asyncio.sleep(0.4)  # 让洪水持续一会儿
        sup = demo.app.supervisors["chaos"]
        tail_ok = len(sup._stderr_tail) <= sup.cfg["stderr_keep_lines"]
        log_size = os.path.getsize(sup._stderr_log_path)
        demo.record("04b-stderr-bounded", "pass" if tail_ok and log_size <= sup.cfg["stderr_log_max_bytes"] + 4096 else "fail",
                    tail_lines=len(sup._stderr_tail), log_bytes=log_size,
                    cap=sup.cfg["stderr_log_max_bytes"])
        await demo.isolation_probe("stderr-flood")

        # 5. 极小字节块（每帧按 1 字节写出，必然切开多字节中文）
        await demo.set_chaos("", {"FRAME_CHUNK_BYTES": "1"})
        await demo.expect_ok("05-tiny-chunks", "chaos", "chaos_lab",
                             ["id", "name", "region"], {"region": {"contains": "北"}})

        # 6. 未知响应 id
        await demo.set_chaos("unknown_id")
        await demo.expect_failure("06-unknown-id", "chaos", "chaos_lab", "unknown rpc id")
        await demo.isolation_probe("unknown-id")

        # 7. 声明长度过大
        await demo.set_chaos("oversize", {"CHAOS_OVERSIZE_BYTES": str(20 * 1024 * 1024)})
        await demo.expect_failure("07-oversize-length", "chaos", "chaos_lab", "exceeds limit")
        await demo.isolation_probe("oversize")

        # 8. 子进程半途退出
        await demo.set_chaos("half_exit")
        await demo.expect_failure("08-half-exit", "chaos", "chaos_lab", "exited early")
        await demo.isolation_probe("half-exit")

        # 9a. 取消碰撞（完成获胜）：等全部数据批与终态响应都已发出后再取消，
        # 宿主已按 id 接收终态 -> 采用 completed，并注明 cancel 输掉竞争。
        await demo.set_chaos("cancel_race", {"CHAOS_RACE_WINDOW_S": "0.3",
                                             "CHAOS_BATCH_DELAY": "0.05"})
        submitted = await demo.query("chaos", "chaos_lab", ["id"],
                                     {"score": {"le": 40}}, wait=False)
        # 等待该查询在宿主侧落到终态（4 批 * 0.05s + 窗口 0.3s 足够）。
        await asyncio.sleep(1.5)
        row_before = demo.app.storage.get_query(submitted["query_id"])
        cancel_res = await demo.app.cancel_query(submitted["query_id"])
        await asyncio.sleep(0.3)
        row = demo.app.storage.get_query(submitted["query_id"])
        # 终态必须保持唯一：先完成即完成，迟到的 cancel 不能改写结果。
        # 若 cancel 在终态核对前已入队 -> 标注竞争中完成获胜；
        # 若 cancel 在作业回收后才到 -> 普通 completed，cancel 返回 no running query。
        ok = (
            row["status"] == "succeeded"
            and row["terminal_state"] in (
                "completed",
                "completed (cancel request lost the race)",
            )
            and row_before["status"] == "succeeded"
        )
        demo.record("09a-cancel-race-completion-wins", "pass" if ok else "fail",
                    query_id=submitted["query_id"], status=row["status"],
                    terminal_state=row["terminal_state"], cancel_note=cancel_res["note"],
                    note="终态响应先到，迟到 cancel 不能改写已核对完成的结果")

        # 9c. 真正的同时到达竞争（确定复现“完成获胜”）。
        # complete_on_race 模式：连接器批次很快发完，进入 0.5s 窗口，
        # 随后在 2s 窄缝中等待 cancel；宿主在窄缝内发 cancel（cancel_sent 登记为 True），
        # 连接器确认看到 cancel 却仍发成功终态 ->
        # 宿主命中 “completed (cancel request lost the race)”。
        await demo.set_chaos("cancel_race,complete_on_race",
                             {"CHAOS_RACE_WINDOW_S": "0.5",
                              "CHAOS_TIGHT_WINDOW_S": "2.0",
                              "CHAOS_BATCH_DELAY": "0.02"})
        submitted = await demo.query("chaos", "chaos_lab", ["id"],
                                     {"score": {"le": 20}}, wait=False)
        await asyncio.sleep(0.8)  # 批次(约0.08s)+窗口(0.5s)已过，进入 2s 窄缝
        cancel_res = await demo.app.cancel_query(submitted["query_id"])
        await asyncio.sleep(0.6)  # 窄缝内收到 cancel，连接器随即发成功终态
        row = demo.app.storage.get_query(submitted["query_id"])
        valid_terminal = (
            row["status"] == "succeeded"
            and row["terminal_state"] == "completed (cancel request lost the race)"
        )
        demo.record("09c-cancel-race-simultaneous", "pass" if valid_terminal else "fail",
                    query_id=submitted["query_id"], status=row["status"],
                    terminal_state=row["terminal_state"], cancel_note=cancel_res["note"],
                    note="cancel 已登记但成功终态先被核对 -> 完成获胜（输掉竞争）")

        # 9b. 明确取消成功：大批次延迟下，查询早期就取消 -> cancel 获胜
        await demo.set_chaos("cancel_race", {"CHAOS_RACE_WINDOW_S": "1.0",
                                             "CHAOS_BATCH_DELAY": "1.0"})
        submitted = await demo.query("chaos", "chaos_lab", ["id"], wait=False)
        await asyncio.sleep(0.15)
        await demo.app.cancel_query(submitted["query_id"])
        await asyncio.sleep(2.5)
        row = demo.app.storage.get_query(submitted["query_id"])
        ok = row["status"] == "cancelled" and "cancel won" in (row["terminal_state"] or "")
        demo.record("09b-cancel-wins", "pass" if ok else "fail",
                    query_id=submitted["query_id"], status=row["status"],
                    terminal_state=row["terminal_state"], fail_reason=row["fail_reason"])
        await demo.isolation_probe("cancel-race")

        # 10. 挂起 + 不支持 cancel -> 超时结束进程，只影响本连接器
        await demo.set_chaos("hang", {"CHAOS_SUPPORTS_CANCEL": "0"})
        t0 = time.monotonic()
        await demo.expect_failure("10-hang-timeout-kill", "chaos", "chaos_lab", "timed out")
        demo.results[-1]["timeout_s"] = round(time.monotonic() - t0, 2)
        # 不相关连接器必须存活
        alive = {n: s.state for n, s in demo.app.supervisors.items() if n != "chaos"}
        demo.record("10b-others-survive",
                    "pass" if all(v == "alive" for v in alive.values()) else "fail",
                    states=alive)
        await demo.isolation_probe("hang")

        # 11. 重启后可服务，但不会自动重发旧请求
        await demo.set_chaos("")
        before = len(demo.app.storage.list_queries("chaos"))
        await demo.expect_ok("11-restart-serves-new-query", "chaos", "chaos_lab",
                             ["id", "name"], {"score": {"le": 5}})
        after = len(demo.app.storage.list_queries("chaos"))
        demo.record("11b-no-silent-resend", "pass" if after == before + 1 else "fail",
                    queries_before=before, queries_after=after,
                    note="重启只建新进程，历史失败查询未被悄悄重发拼接")

        await demo.app.wait_queries(timeout=5)
    finally:
        # 指标快照（在关闭前抓取）
        metrics = demo.app.metrics.snapshot()
        manifest_path = os.path.join(run_root, "manifest.json")
        await demo.app.export(os.path.relpath(manifest_path, ROOT))
        await demo.app.shutdown()

    # ------------------------------------------------------------ 真值比对
    sys.path.insert(0, HERE)
    from scripts.ground_truth import verify_manifest

    ok_items, bad_items = verify_manifest(manifest_path, os.path.join(ROOT, "data"))
    report = {
        "run_root": run_root,
        "scenarios": demo.results,
        "ground_truth": {
            "verified_ok": len(ok_items),
            "problems": bad_items,
            "ok_verdicts": [{"query_id": i["query_id"], "verdict": i["verdict"],
                             "records": i.get("records"), "batches": i.get("batches")}
                            for i in ok_items if i["verdict"] == "match"],
            "expected_failures": [i for i in ok_items if i["verdict"] == "expected-failure"],
        },
        "metrics": metrics,
    }
    report_path = os.path.join(run_root, "demo-report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    n_fail = sum(1 for r in demo.results if r["verdict"] != "pass")
    print("\n================ 演示汇总 ================")
    for r in demo.results:
        print(f"  {r['verdict'].upper():4s}  {r['scenario']}")
    print(f"\n真值比对: 成功匹配 {sum(1 for i in ok_items if i['verdict']=='match')} 条查询, "
          f"预期失败 {sum(1 for i in ok_items if i['verdict']=='expected-failure')} 条, "
          f"异常 {len(bad_items)} 条")
    print(f"在途峰值: {metrics['peak_inflight_by_connector']}")
    print(f"连接器 RSS 峰值(字节): {metrics['peak_rss_bytes_by_connector']}")
    print(f"宿主 RSS 峰值(字节): {metrics['host_peak_rss_bytes']}")
    print(f"\n报告: {report_path}\n清单: {manifest_path}")
    return 1 if (n_fail or bad_items) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
