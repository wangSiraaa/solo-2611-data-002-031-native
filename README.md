# GET-only 隔离转发代理

本目录只实现题面要求的范围：仅转发 `GET /item`，两个固定上游来源之间做**可解释的资源隔离**，
每个逻辑请求一个总时间预算，返回明确终态并记录每来源计数。不是通用网关。

- `proxy.py` — 代理（Python 3.11 标准库，`asyncio`，无第三方依赖）
- `demo.py` — 并发演示 / 校验脚本（真实启动 `fixtures/upstream.py` + 代理并解析双方日志）
- `fixtures/config.json` — 题目给定配置
- `fixtures/upstream.py` — 题目给定确定性上游

## 启动命令

终端 A（阻塞源，先确保 gate 路径不存在）：

```sh
python3 fixtures/upstream.py --port 8111 --mode blocked \
  --gate .runtime/release.gate --audit .runtime/slow.jsonl
```

终端 B（健康源）：

```sh
python3 fixtures/upstream.py --port 8112 --mode fast --audit .runtime/fast.jsonl
```

终端 C（代理，默认读取 `fixtures/config.json`，监听 127.0.0.1:8123）：

```sh
mkdir -p .runtime
python3 proxy.py --config fixtures/config.json --port 8123 \
  --stats-file .runtime/stats.json --event-log .runtime/events.jsonl
```

客户端只能请求配置中的来源与固定路径（不得传任意 URL）：

```sh
curl -sD- 'http://127.0.0.1:8123/item?source=fast'
curl -sD- -H 'X-Source: slow' 'http://127.0.0.1:8123/item'
```

响应头 `X-Proxy-Outcome` 给出终态：`success` / `queue_full` / `deadline` /
`client_cancelled`（只有代理内部记录，客户端此时通常已断开）/ `upstream_error` / `shutdown`。
`--stats-file` 原子写入的 JSON 快照与 `--event-log` 的 JSONL 事件流用于诊断与演示校验。

一键并发演示（含三个场景与断言）：

```sh
python3 demo.py --scenario all     # 或 isolation / cancel / shutdown
```

会写 `.runtime/demo_summary.json`、各场景 `*_stats.json` / `*_events.jsonl` 以及上游审计
`iso_slow.jsonl` 等。退出码 0 表示全部断言通过。

## 隔离与调度策略（明确预留）

配置：`global_max_inflight=2`，每来源 `max_inflight=1`、`queue_limit=3`，总预算 600ms。

- **硬预留，而非单一全局信号量**：准入条件是 *该来源* `inflight < max_inflight` **且**
  全局 `inflight < global_max_inflight`。每来源的上限是硬上限，所以 `slow` 最多占 1 个全局位，
  `fast` 的 1 个全局位始终空着。启动时校验 `Σ per-source max_inflight ≤ global_max_inflight`，
  否则拒绝启动，而不是静默退化成“先到的坏源占满全局资源”。
- **轮转分派（round-robin）**：槽位释放时 `pump()` 按来源轮转，每轮每个来源至多派发 1 个，
  所以一个来源的积压不会让另一个来源排不上队。
- **队满即拒**：来源内 FIFO 队列达到 `queue_limit` 时，新请求立即返回 `503 queue_full`
  （`X-Proxy-Outcome: queue_full`），不占用任何槽位。
- **不插队**：`submit()` 只在来源队列为空时允许直接运行，否则一律入队，保证 FIFO。

每个来源维护：`received`（进入代理的逻辑请求）、`dispatched`（**真正写出上游 socket 后**才计数）、
`inflight`、`queued`、`ended`（按终态拆分）、`slot_assignments`、`max_inflight_observed`。
全局同样计数，并统计 `bad_requests` 与 `delivery_failed`。

## 单一总预算

`deadline = 接入时刻 + total_budget_ms`，只此一个。排队、建连、写请求、读完整响应都在
`asyncio.timeout(deadline - now)` 之下；出队后**不重新分配预算**，也不只给单次 socket 操作设超时。
排队项到期由同一超时立即结束并同步移出队列（不等待、不补发）；`pump()` 派发前还会二次检查
`remaining() <= 0`，作为绝不向上游补发的兜底。在途请求到期/客户端断开时取消读取任务，
`finally` 关闭上游 socket 并释放槽位。

## 槽位的一次性释放

`Request.slot` 标志 + 无 `await` 的临界区保证 `slot`/全局计数恰好释放一次；`finalize()` 幂等，
以“谁先设置 `finished` 谁定终态”处理完成与取消的竞态。因此不会重复释放，也不会留下永久等待
（排队等待者由 `finalize()` 用终态结果唤醒，而非永远挂起）。

## 诊断说明（阅读日志时注意）

- 上游审计里的 `begin`/`end` 是**上游实际处理的请求**，不是代理存活 socket 的计数。
  本 mock 在客户端关闭后可能仍继续持有 handler：`cancel` 场景中代理在客户端取消时立即关闭上游
  连接并回收槽位，但上游审计的 `end` 要等它的阻塞循环退出（最长 30s 或 gate 释放）才记录，
  单个 `begin→end` 跨度约 600ms+ 就是这个原因——这是**上游日志的语义差异**，不代表代理仍在读。
- 每次逻辑请求可在代理事件流中按 `rid` 串联：`received → queued/slot_assigned →
  upstream_write → finished(reason)`，`slot_assigned`/`slot_released` 可重新计算每来源与全局
  在途峰值，`queue_expired` 标记到期出队项；断言脚本用这些事件校验无泄漏、无重复终态、
  到期项未被 `upstream_write`。

## 演示已验证的结论（`python3 demo.py --scenario all`）

1. **isolation**：向 `slow` 并发 5 个请求（1 在途 + 3 排队 + 1 队满拒绝），同时持续请求 `fast`。
   预算内：`slow` 为 `queue_full=1, deadline=4`，且在途请求也因总预算到期返回 `deadline`；
   `fast` 在阻塞窗口内持续完成（约 47 次，全部 `success`，延迟远低于预算）。随后释放屏障：
   上游审计 `slow begins=1, ends=1`，**没有**在释放后补发的排队项（`begin_after_release=0`）；
   每来源在途峰值 = 1，全局峰值 = 2。
2. **cancel**：在代理读取阻塞上游时客户端断开 → 立即记为 `client_cancelled` 并释放槽位
   （`inflight` 回到 0，全局回到 0）；随后新请求可正常使用该槽位并到期结束；`fast` 正常 `success`。
3. **shutdown**：在途 1 + 排队 3 时发送 SIGTERM → 进程约 16ms 内 `exit 0`，排队项从未被派发
   （上游仅 1 次 `begin`），每个请求都有终态（排队者收到 `503 shutdown`），全局在途归零。

所有场景均校验：`received == Σ ended`、结束 `inflight=queued=0`、无重复终态、无槽位泄漏、
到期队列项未出现在上游 `begin` 中。
