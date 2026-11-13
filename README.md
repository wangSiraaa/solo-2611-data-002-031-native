# qgateway：本地 HTTP 查询网关

Python 3.11 + PostgreSQL 16 + aiohttp + psycopg3 实现的本地查询网关。
客户端只提交目录里的 `query_id` 与参数（驱动绑定，不能上传 SQL），服务端以只读事务、
逐帧 NDJSON 流返回结果，并在取消、断连、超时、超预算时保证游标/事务被清理、连接可安全复用。

## 目录结构

```
qgateway/
  config.py      可调配置（池大小、队列、帧队列、fetch 块、宽限期）
  catalog.py     查询目录：固定 SQL、绑定参数类型、行数/字节/时间预算
  values.py      带 PostgreSQL 类型标签的值编解码（int8/numeric/timestamptz/date/NULL...）
  frames.py      NDJSON 帧（meta / rows / terminal）与字节统计
  pool.py        有界连接池 + 有上限等待队列 + 归还健康检查
  monitor.py     独立监控连接：pg_stat_activity、类型 OID
  registry.py    每请求状态机（QUEUED/EXECUTING/WAITING_FOR_CLIENT/终态）
  engine.py      生产者-有界队列-消费者流水线、取消、预算、清理
  webapp.py      aiohttp 路由与优雅停止
  dbinit.py      合成 schema 与确定性数据生成（默认 12 万行）
  demo_check.py  独立直连数据库核对导出结果的类型/行数/值
  demo.py        可重复的综合负载演示与自动验收
  cli.py         命令行入口
scripts/         初始化、启动、演示、手工取消/背压验证脚本
docs/            协议文档与架构说明
```

## 本地运行步骤

需要 PostgreSQL 16 的 `initdb/pg_ctl/psql/createdb` 在 PATH 中。
注意：PostgreSQL 数据目录与 Unix socket 不能放在 virtiofs/网络挂载上，
本项目默认使用本地磁盘 `/tmp/qgateway-pgdata`（可用环境变量 `QGATEWAY_PGDATA` 覆盖）；
日志与导出文件仍在仓库内的 `.runtime/`（已被 gitignore 忽略）。

```sh
# 1. 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 初始化专用本地 PG 实例（仅 Unix socket）并生成 12 万行合成数据
bash scripts/init_db.sh

# 3. 启动网关（默认 pool=3，等待队列=2，帧队列=4）
bash scripts/run_server.sh
# 或：.venv/bin/python -m qgateway.cli serve --port 8080
```

## 常用入口

```sh
# 查看查询目录
curl -s http://127.0.0.1:8080/catalog | python3 -m json.tool

# 提交查询（参数由驱动绑定；NULL 用 null）
.venv/bin/python -m qgateway.cli query point_lookup --params '{"event_id": 42424}'
.venv/bin/python -m qgateway.cli query kind_aggregate \
  --params '{"kind_id": null, "start_time": "2026-01-01T00:00:00+00:00",
             "end_time": "2026-03-01T00:00:00+00:00"}'

# 慢客户端（每帧停顿）与读到一半主动断连
.venv/bin/python -m qgateway.cli query large_scan \
  --params '{"start_id": 1, "end_id": 24000}' --pace-ms 6
.venv/bin/python -m qgateway.cli query large_scan \
  --params '{"start_id": 1, "end_id": 120000}' --abort-after-rows 400

# 导出结果（NDJSON + 清单），再独立直连数据库核对
.venv/bin/python -m qgateway.cli export large_scan \
  --params '{"start_id": 1, "end_id": 120000}' \
  --output .runtime/demo/large.ndjson
.venv/bin/python -m qgateway.cli check-db --export-file .runtime/demo/large.ndjson

# 观察服务与单请求状态（含 pg_stat_activity、backend pid、阶段耗时、背压指标）
curl -s http://127.0.0.1:8080/status | python3 -m json.tool
.venv/bin/python -m qgateway.cli status <request_id>

# 取消指定请求（不影响复用同一连接的其它请求）
.venv/bin/python -m qgateway.cli cancel <request_id>
```

## 一键可重复演示与验收

```sh
bash scripts/run_demo.sh
# 等价于：.venv/bin/python -m qgateway.cli demo --pool-size 3 --wait-queue-limit 2 --port 8080
```

演示自动覆盖并逐项断言（21 项，全部 PASS 才退出 0）：

1. 快速点查与多表聚合（含 NULL 参数、精确类型）；
2. 池=3、队列=2 下并发占满、排队、超额请求被快速 503；
3. 显式取消：`pg_stat_activity` 中查询停止、无 idle in transaction、连接随后可成功复用；
4. 读到一半断连：客户端标记未完成，服务状态保留终态，查询/事务被清理；
5. SQL 失败（22012）后再次借用同一池连接成功；
6. 慢客户端有界背压：队列满时生产者停在 `WAITING_FOR_CLIENT`、fetch 暂停、
   在途帧/字节峰值有界、服务 RSS 平稳，无全量物化；
7. 12 万行大结果导出 + 独立直连数据库逐行核对类型/行数/值；
8. 目录时间预算触发 `time_limit_exceeded`；
9. SIGTERM 停机：拒绝新请求、宽限清理、强制终止残余 backend、无闲置事务残留。

报告写入 `.runtime/demo/report.json`，网关事件日志写入 `.runtime/gateway-*.log`。

## 文档

- [NDJSON 帧协议](docs/protocol.md)
- [架构与设计说明](docs/architecture.md)

## 停止数据库

```sh
pg_ctl -D "${QGATEWAY_PGDATA:-/tmp/qgateway-pgdata}" stop
```
