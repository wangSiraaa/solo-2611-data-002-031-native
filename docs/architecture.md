# 架构与设计说明

## 组件总览

```
HTTP 客户端 (aiohttp)
   │  NDJSON 逐帧流（meta / rows* / terminal）
   ▼
webapp.py  ── aiohttp 路由、StreamResponse、传输错误捕获、优雅停止
   │
engine.py  ── 每请求流水线：生产者协程 + 有界帧队列 + 流消费者
   │            预算校验、取消、终态、游标/事务清理
   ├── pool.py     有界连接池（固定大小 + 有上限等待队列 + 归还健康检查）
   ├── monitor.py  独立监控连接（pg_stat_activity、类型 OID 名）
   ├── registry.py 每请求状态机与可观测指标
   ├── catalog.py  查询目录（query_id → 固定 SQL/参数类型/预算）
   ├── frames.py   帧构造与字节统计
   ├── values.py   带 PostgreSQL 类型标签的值编解码
   └── events.py   诊断事件日志（只记录，不参与控制决策）
```

## 每请求流水线与有界背压

```
命名游标 fetchmany(有界块)  ──►  asyncio.Queue(maxsize=frame_queue_limit)  ──► HTTP write
        生产者协程                              在途帧缓存                              流消费者
```

- 生产者在 `queue.put` 处遇到满队列会挂起，状态切到 `WAITING_FOR_CLIENT`，
  同时停止继续 `fetchmany`，数据库游标也不再向前推进。
- 因此网络发送、游标拉取、可用连接三者形成有界背压：慢客户端最多让服务端缓存
  `frame_queue_limit` 个帧，而不是整份结果。
- 实际读取块大小（`fetch_block_rows`）、在途帧数/字节数与峰值、fetch 次数都在
  `/status` 与每请求状态里可观测。

## 只读事务与固定视图

每个请求在自己的连接上执行
`BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ`，并设置
`SET LOCAL statement_timeout`。同一 HTTP 请求的所有结果行来自该事务的固定快照；
不同请求不要求共享快照。结束时统一 `ROLLBACK` 终结事务（只读，无提交副作用）。

## 连接池纪律

- 固定 `pool_size`，超出后进入容量为 `wait_queue_limit` 的等待队列；队列再满则
  在创建后台协程之前同步抛 `PoolQueueFull`，HTTP 层快速回 503，不产生孤儿请求。
- 归还连接前检查 `conn.info.transaction_status`：只有 `IDLE` 才回池。
  失败事务（`INERROR`）、仍在事务中（`INTRANS`）、活动（`ACTIVE`）或已关闭的连接
  一律先 `ROLLBACK` 再关闭，绝不交给新请求。
- SQL 报错后通过同一连接 `ROLLBACK` 即可恢复 `IDLE` 并安全复用；无法恢复时关闭补建。

## 取消、断连、超时的清理路径

这三类情况都收敛到同一条清理逻辑：

1. 通过独立连接对目标 backend 发 `CancelRequest`（取消）或由传输层异常触发（断连/发送失败）；
2. 命名游标 `close()`，事务 `ROLLBACK`；
3. 连接经健康检查后回池或关闭；
4. 写入终态帧；若消费者已消失，终态只保留在服务状态（`terminal_delivered=false`）。

显式取消按 `request_id` 路由到对应 `Context`，因此不会误伤复用同一 TCP 连接的下一次请求。

## 服务停止

收到 SIGINT/SIGTERM 后：标记关闭并拒绝新请求 → 通知全部在途请求取消 →
在 `shutdown_grace_seconds` 宽限内等待生产者收尾 → 仍占用的连接强制关闭
（PostgreSQL backend 随之终止），保证不留下永久闲置事务。

## 控制面与诊断面分离

`pg_stat_activity`（通过独立 monitor 连接查询）是数据库真实状态的唯一权威来源；
`events.py` 的 JSONL 日志与 registry 指标仅用于诊断和可观测性，不参与任何控制判断。
