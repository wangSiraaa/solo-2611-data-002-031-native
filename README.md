# snapx — 一致快照并行导出工具（PostgreSQL 16 + Python 3.11）

在数据库持续写入的情况下，由**一个协调进程**建立 PostgreSQL 可导出事务快照
（`pg_export_snapshot()`），**多个独立工作进程**在各自的只读事务中导入同一快照，
按稳定的 `order_id % N` 规则分片并行导出 `orders` / `payments` / `dataset_epoch`。
全程纯 MVCC：**不复制整库、不锁表**，写入器在导出期间可正常提交。

## 布局

```
migrations/0001_init.sql   # orders / payments(FK) / dataset_epoch(单行)
snapx/
  cli.py                   # 命令行入口：generate/writer/export/inspect/verify (+dbstart/dbstop/_worker)
  db.py                    # 连接参数（SNAPX_PG* 环境变量）
  generate.py              # 应用迁移 + 合成数据（含边界用例）
  writer.py                # 合成写入器：单事务改订单+付款+epoch，保持逐笔不变量
  exporter.py              # 协调进程：导出快照、派生 worker、屏障编排、校验、原子发布
  worker.py                # _worker：导入快照的独立进程，读一个分片
  inspect_cmd.py           # 查看已发布物与各 attempt 状态/原因
  verify.py                # 对已发布目录做全量独立复核
demo.sh                    # 一键演示全部场景
exports/<export_id>/       # 已发布目录（manifest.json, baseline/, shards/）
exports/_attempts/<id>/attempt-N/   # 每次尝试：ATTEMPT.json(身份+状态+原因), control/, payload/
```

## 一致性机制

- 协调进程：`BEGIN ISOLATION LEVEL REPEATABLE READ` → `SELECT pg_export_snapshot()`，
  事务在整个导出期间保持打开（快照由此存活）。同一事务内还执行独立的完整查询，
  保存**小样例基准**（每分片前 2 笔订单及其全部付款，canonical JSONL）。
- 工作进程：独立进程，`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` →
  `SET TRANSACTION SNAPSHOT '<sid>'`，然后只读本分片（`mod(order_id, N) = K`）。
  付款按所属订单的 `order_id` 分片，因此**每笔订单及其付款恰好属于一个分片**；
  无付款、零金额订单照常出现在 orders JSONL 中。
- 输出：每分片 `orders-K.jsonl` / `payments-K.jsonl`（按主键排序、canonical JSON、
  整数分）+ `shard-K.meta.json`（快照号、epoch、行数、整数金额、sha256、读取时间戳）。
- 校验（全部通过才发布）：文件摘要、行数、金额、分片规则、主键有序、跨分片唯一、
  FK 闭合、**逐笔及全局付款合计 == 订单金额**、与协调事务内独立计数交叉核对、
  基准文件与合并结果**逐行 diff**。
- 发布：先写 `manifest.json`（epoch、快照号、各分片行数/整数金额/摘要、写入器证明、
  时间戳），再 `os.rename(payload, exports/<id>)` —— 目标已存在则失败，
  **半成品永远覆盖不了已发布目录**。

## 失败语义

- 协调事务存活时 worker 崩溃（`SNAPX_CRASH_SHARDS=K` 注入）：该分片在**同一快照**上重试
  （默认最多 3 次）。
- 协调进程被杀：快照随之失效（迟到 worker 导入会得到
  `invalid snapshot identifier`）。该 attempt 永远无法发布；重跑同一 export_id 时
  旧 attempt 被标记 `abandoned`（保留身份与原因），以**新 attempt、新快照**重新导出，
  旧分片绝不混入（每个分片 meta 都记录快照号，校验时强制一致）。
- 同一 export_id 已成功：相同分片参数重试 → 直接返回原发布物（rc=0）；
  分片参数不同 → 拒绝（rc=2）。
- 失败 attempt：目录保留 `ATTEMPT.json`（status=failed + reason），payload 中
  **没有 manifest.json**，无法伪装成完成。

## 屏障演示（证明同一快照）

`export --barrier-demo`：协调进程派生一个**门控写入器**（事务构建好后等待放行），
偶数分片（A 组）在写入器提交**前**读取，奇数分片（B 组）在文件屏障后、写入器提交
**后**才读取。manifest 记录写入器 `commit_ts < finished_ts`、B 组 `read_started_ts >
commit_ts`，而所有分片的快照号与 epoch 完全一致 —— 导出内容仍是提交前的同一数据库视图。

## 本机演示命令

```bash
python3 -m venv .venv && .venv/bin/pip install "psycopg[binary]>=3.1"
./demo.sh          # 一键跑完全部 9 个场景（含 dbstart/generate/...）
```

或逐步执行：

```bash
export SNAPX_PGHOST=127.0.0.1 SNAPX_PGPORT=54329 SNAPX_PGDB=snapx SNAPX_PGUSER=postgres
.venv/bin/python -m snapx dbstart                       # initdb + 启动独立本地实例
.venv/bin/python -m snapx generate --orders 240 --seed 42
.venv/bin/python -m snapx export --export-id demo1 --shards 4 --barrier-demo
.venv/bin/python -m snapx verify --export-id demo1
.venv/bin/python -m snapx inspect --export-id demo1
.venv/bin/python -m snapx writer --loop --interval 0.2 --count 50   # 并发写入
SNAPX_CRASH_SHARDS=2 .venv/bin/python -m snapx export --export-id demo2 --shards 4
.venv/bin/python -m snapx dbstop
```

所有数据均为合成（`generate`/`writer` 生成，随机种子可控）。
