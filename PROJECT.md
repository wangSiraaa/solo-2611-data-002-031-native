# 增量连接物化 CLI（PostgreSQL 16 + Python 3.11）

两张不断修改的小表 `L` / `R`，按 `join_key` 完整相等做连接。本工具不每批重跑全部
报表，而是**只维护本批受影响键对应的连接明细与汇总**，并保证：

- 源表、连接明细、汇总、批次回执在**同一个 SERIALIZABLE 事务**内完成；
- 任一非法操作整批回滚，不留半成品；
- 同 `batch_id` + 同规范内容重放幂等，内容不同直接拒绝；
- 并发批次等价于某个完整串行顺序，不发布半批混合值。

## 1. 数据模型

| 表 | 内容 |
| --- | --- |
| `l_source` | 左表：`row_id`（主键）、非空 `join_key`、正整数 `qty` |
| `r_source` | 右表：`row_id`（主键）、非空 `join_key`、非负整数 `unit_cents` |
| `join_detail` | 连接明细，主键 `(l_row_id, r_row_id)`；同键多行产生全部左右配对，`amount_cents = qty * unit_cents` |
| `key_summary` | 每个 `join_key` 的 `pair_count`、`qty_total`（按每个实际配对累加，来源不去重）、`amount_total_cents` |
| `batch_receipt` | 批次回执：`batch_id` 唯一、规范内容 SHA-256、`affected_keys`、操作数、状态 |

建表 SQL 在 `incjoin/schema.sql`，幂等可重复执行。

## 2. 局部维护算法（增量实现，非全量重算）

一个批次在单个事务内按 `seq` 升序应用所有操作，同时收集受影响键集合
（put 的新键、被替换行的旧键、delete 行的当前键）。两侧操作全部落表后，
**只对受影响键**做三步集合维护，未受影响键的物化行（含 `last_changed_batch`）
完全不被触碰：

1. **删失效配对**：受影响键上，旧明细中已无法由当前 `L JOIN R ON join_key`
   重新 JOIN 出来的行删除（删源行由外键 `ON DELETE CASCADE` 兜底，键迁移、
   重复键删一条等情况同样覆盖，不会误删同键其他配对）。
2. **upsert 现存配对**：对受影响键执行一次完整的现存源表 JOIN 并 upsert。
   两侧同时增行的交叉配对由这一条 JOIN 天然产生——**不把左右增量分别相加**，
   因此不会遗漏或重复；行内容没变时 `WHERE` 条件为假，连
   `last_changed_batch` 都不更新。
3. **汇总重算与清理**：受影响键的汇总从明细 `GROUP BY` 重算 upsert；
   最后一个配对消失的键直接删除汇总行。

并发正确性：事务隔离级别为 `SERIALIZABLE`（PostgreSQL SSI），命中
序列化失败/死锁时按指数退避整批重试；同 `batch_id` 并发插入由回执主键
裁决，内容一致者走重放、不一致者拒绝。

## 3. 目录结构

```
incjoin/
  schema.sql          建表迁移（幂等）
  db.py               连接、建库、迁移
  batches.py          批次 JSON 严格校验、规范序列化、内容哈希
  service.py          事务应用、局部物化维护、幂等/重试、崩溃钩子、load
  verify.py           独立完整 SQL JOIN + GROUP BY 重算并逐行比对（仅验证用）
  queries.py          只读查询
  generator.py        合成快照、命名场景、确定性随机批次
  cli.py              命令行入口
tests/                unittest 套件（23 个用例）
scripts/run_tests.sh  一键环境检查 + 测试
```

## 4. 本机运行（不依赖外部服务）

需要 PostgreSQL 16 的 `initdb/pg_ctl/createdb/psql` 在 PATH 中。

```sh
# 1) Python 虚拟环境与驱动
python3.11 -m venv .venv
.venv/bin/pip install "psycopg[binary]"

# 2) 本机独立数据库实例（数据目录在被忽略的 .runtime/ 下）
mkdir -p .runtime
initdb -D "$PWD/.runtime/pgdata" --auth-local=trust --auth-host=trust
# 注意：若工作目录文件系统不支持 Unix socket 权限（如某些挂载盘），
# 把 socket 放到 /tmp 并监听本地回环端口：
pg_ctl -D "$PWD/.runtime/pgdata" -l "$PWD/.runtime/postgres.log" \
    -o "-k /tmp -c listen_addresses=127.0.0.1 -p 55432" start
createdb -h 127.0.0.1 -p 55432 taskdb

# 或者直接跑测试脚本，它会自动完成上述初始化（含测试库 taskdb_test）
scripts/run_tests.sh
```

默认 DSN 为 `postgresql://127.0.0.1:55432/taskdb`，可用 `--dsn`
（全局参数，子命令前后均可）或环境变量 `INCJOIN_DSN` 覆盖。

## 5. 命令

```sh
# 建库（如需）并应用迁移；其他命令启动时也会自动 migrate
.venv/bin/python -m incjoin bootstrap

# 生成合成数据：初始快照、8 个命名场景、20 个随机批次
.venv/bin/python -m incjoin generate .runtime/demo

# 整快照全量替换（单事务 TRUNCATE + 重建），随后自动完整重算验证
.venv/bin/python -m incjoin load \
    --left .runtime/demo/snapshot_left.json \
    --right .runtime/demo/snapshot_right.json

# 应用批次（事务、幂等、自动验证；非法批次退出码 1，验证失败退出码 2）
.venv/bin/python -m incjoin apply .runtime/demo/scenarios/both_sides_add.json

# 只读查询
.venv/bin/python -m incjoin query detail [--key K3]
.venv/bin/python -m incjoin query summary [--key K2]
.venv/bin/python -m incjoin query left | right | receipts

# 独立完整重算验证（不参与增量实现）
.venv/bin/python -m incjoin verify
```

批次 JSON 形态：

```json
{
  "batch_id": "B1",
  "operations": [
    {"seq": 1, "action": "put", "side": "L", "row_id": "L6",
     "join_key": "K3", "qty": 4},
    {"seq": 2, "action": "delete", "side": "R", "row_id": "R1"}
  ]
}
```

`seq` 必须为批次内唯一正整数（应用前自动按升序排列）；put 携带
`join_key` 与侧对应数值字段（L 侧 `qty` 正整数，R 侧 `unit_cents`
非负整数），delete 不得携带其他字段且要求该行当时存在。布尔值不被
接受为整数，64 位溢出（含 `qty*unit_cents`）整批回滚。

### 幂等与冲突

规范形式 = `sort_keys`、固定分隔符、操作按 `seq` 升序的 JSON，取
SHA-256。同 `batch_id` 同哈希：返回 `replayed` 且数据零改动；同
`batch_id` 不同哈希：报错拒绝，退出码 1。

## 6. 验证策略

- `incjoin/verify.py` 对**每张物化表**用独立 SQL 完整重算（只依赖两张
  源表的 `JOIN ... GROUP BY`），逐行比对明细与汇总，并核对配对数、
  数量、金额三条守恒关系，以及批次局部性（每个批次改动的键必须在其
  回执 `affected_keys` 内）。
- 测试套件另有一套**纯 Python 字典参照模型**独立计算连接与汇总，与
  数据库结果交叉比对。
- 每次 `load` / `apply` 成功后 CLI 自动执行一次完整重算。

## 7. 测试覆盖（23 个用例）

```sh
.venv/bin/python -m unittest discover -s tests -t . -v
```

- 两侧同时对同一键增行（交叉新增 4 配对）
- 同批先删后加同一行、同批多次修改同一行（只发布批次后状态）
- 键迁移（旧键配对清理、新键配对建立）
- 右侧单价归零（配对保留、金额为 0）
- 最后一个配对删除（明细与汇总行都消失）
- 多个重复键增删（删一条重复行不误伤其他配对；数量按配对累加）
- 未受影响键物化行与 `last_changed_batch` 保持不变
- 幂等重放（空白/键序不同但规范内容相同）与同 ID 内容冲突拒绝
- 七类静态非法输入 + delete 不存在行的整批回滚
- 两个进程并发更新同一键、同 ID 同内容/异内容并发、5 轮并发压测、
  SSI 只读-写冲突重试
- 提交前 / 提交后 `os._exit(137)` 崩溃后的重试（回滚后重提 / 幂等重放）
- CLI 端到端 + 20 个确定性随机批次，每批完整重算守恒

崩溃钩子只在测试环境生效：

```sh
INCJOIN_CRASH_BEFORE_COMMIT=<batch_id> .venv/bin/python -m incjoin apply b.json
INCJOIN_CRASH_AFTER_COMMIT=<batch_id>  .venv/bin/python -m incjoin apply b.json
```

## 8. 停止数据库

```sh
pg_ctl -D "$PWD/.runtime/pgdata" stop
```
