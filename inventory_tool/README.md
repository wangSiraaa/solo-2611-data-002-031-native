# 本地库存预占工具 (inventory-tool)

在单机 PostgreSQL 16 上实现的库存**整单预占 / 发货 / 取消**工具。所有数量按
精确整数（`BIGINT`）处理，每个请求的订单状态、商品变化、逐商品流水与请求结果
在**一个数据库事务**内完成，并可由流水完整重算库存。

全部数据由项目自身生成，不依赖真实仓库或任何外部服务。

---

## 1. 业务规则与不变量

### 操作与状态机

```
                 reserve(整单全部可用)
   (不存在) ───────────────────────────▶ RESERVED
                                            │
                              ship ┌────────┴────────┐ cancel
                                   ▼                  ▼
                                SHIPPED            CANCELLED
                              (终态, 不可互转)     (终态, 不可互转)
```

- **reserve**：只有订单内**全部** SKU 的可用量（`on_hand - reserved`）都足够时才
  成功，订单进入 `RESERVED`；任一 SKU 不足则**整单失败**，不占用任何商品，失败
  结果明确落库（`INSUFFICIENT_STOCK`）。
- **ship**：仅 `RESERVED` 可执行，一次性变 `SHIPPED`，同时**减少库存与预占量**。
- **cancel**：仅 `RESERVED` 可执行，变 `CANCELLED` 并**释放预占**。
- 终态（`SHIPPED` / `CANCELLED`）之间不能互转，对终态再次操作返回
  `INVALID_ORDER_STATUS`。
- 同一订单的第二次 reserve 一律拒绝（`ORDER_ALREADY_RESERVED`），不会叠加数量。

### 幂等

- `request_id` 全局唯一，作为请求台账主键。
- **相同 request_id + 相同内容**重试：直接返回第一次已持久化的结果
  （`replayed=true`），**包括失败结果**，不会再次改动库存。
- **相同 request_id + 不同内容**：拒绝（`REQUEST_CONTENT_CONFLICT`），原始台账
  一行绝不被覆盖；拒绝事件以保留后缀 `::content-conflict` 另存一行 `REJECTED`。
- 请求内容指纹 = 规范化业务体（`order_id/action/items`，键排序）的 SHA-256。

### 始终成立的不变量

1. 库存 `on_hand >= 0`、预占 `reserved >= 0`、`reserved <= on_hand`
   （数据库 CHECK + 触发器双重保证）。
2. 可用量 `available = on_hand - reserved`。
3. 每个 SKU 的 `reserved` = 所有 `RESERVED` 订单对该 SKU 的数量之和。
4. 流水重算值 = 表值：
   - `on_hand  = SUM(ledger.delta_on_hand)`（含 `INIT` 初始化基线）
   - `reserved = SUM(ledger.delta_reserved)`（初始为 0）

### 并发正确性

- 事务隔离级别 `SERIALIZABLE`；所有 `stock` 行**统一按 sku 升序**
  `SELECT ... FOR UPDATE` 加行锁，与调用方商品顺序无关，因此相反顺序的多 SKU
  请求不会因加锁顺序相反而长期死锁。
- 可重试冲突（`SerializationFailure` / `DeadlockDetected` / `UniqueViolation`）
  做**有限次**（默认 10 次）指数退避重试，终态只持久化一次；超过上限返回明确
  结果码 `RETRIES_EXHAUSTED`。
- 竞争多个 SKU 不会超卖：整单可用性在持锁后判定，不足即整体回滚。

---

## 2. 目录结构

```
inventory_tool/
├── inventory_tool/
│   ├── config.py        # 连接 DSN 解析 (env > 默认本机 socket)
│   ├── validation.py    # 纯函数校验 + 请求内容指纹
│   ├── service.py       # 核心: 单事务状态机 / 行锁 / 幂等 / 有限重试 / 崩溃钩子
│   ├── repository.py    # 迁移运行 / 库存导入 / 查询 / 对账
│   ├── generator.py     # 合成 stock.csv 与 requests.jsonl
│   └── cli.py           # 命令行入口
├── migrations/
│   └── 001_init.sql     # 建表 / 约束 / 触发器 / 对账函数
├── tests/
│   ├── conftest.py
│   ├── test_functional.py    # 状态机/原子性/幂等/不变量/流水重算
│   ├── test_concurrency.py   # 进程屏障并发竞争
│   └── test_crash.py         # SIGKILL 强制终止后重试
├── examples/
│   ├── stock.csv
│   └── requests.jsonl
├── runtime/             # 运行产物 (gitignore): 结果、合成数据、压力脚本
├── requirements.txt
├── pytest.ini
└── README.md
```

---

## 3. 数据库迁移说明

迁移文件 `migrations/001_init.sql` 是**可重复执行**的初始化迁移（开头
`DROP ... CASCADE` 后重建），创建：

| 对象 | 作用 |
| --- | --- |
| `stock` | SKU 库存：`on_hand`、`reserved`，含非负/不超卖 CHECK |
| `order_records` | 订单一行，状态机 CHECK |
| `order_items` | 订单逐商品明细（同订单 SKU 唯一，数量为正） |
| `request_records` | 请求台账：主键 `request_id`、内容指纹、终态结果 |
| `stock_ledger` | 逐商品流水：`INIT/RESERVE/SHIP/CANCEL` 及增量 CHECK |
| `trg_stock_touch` | 行更新时强制库存不变量 |
| `inventory_reconciliation()` | 表值 vs 流水重算 vs RESERVED 订单之和 对账函数 |

CLI 的 `init` 会执行迁移并导入库存；`--reset` 先 `TRUNCATE` 全部业务表。
也可手工执行：

```sh
psql -h "$PGHOST" -d inventory_test -v ON_ERROR_STOP=1 -f migrations/001_init.sql
```

---

## 4. 本机运行说明（Linux + Python 3.11 + PostgreSQL 16）

### 4.1 准备独立数据库（普通用户，本地 socket）

`/workspace` 若为 virtiofs 等不支持可执行位的挂载，请把 venv 与 PG 数据目录
放在本地文件系统（如下例的 `~/inventory-tool-runtime`），代码仍可放在
`/workspace`。

```sh
# 1) Python 虚拟环境与依赖
python3 -m venv ~/inventory-tool-runtime/venv
~/inventory-tool-runtime/venv/bin/pip install -r inventory_tool/requirements.txt

# 2) 初始化并启动一个独立 PostgreSQL 实例 (仅 Unix socket, 不监听网络)
RT=~/inventory-tool-runtime
mkdir -p "$RT/pgsocket"
initdb -D "$RT/pgdata" --auth-local=trust --auth-host=scram-sha-256 -U "$USER"
pg_ctl -D "$RT/pgdata" -l "$RT/postgres.log" \
  -o "-k $RT/pgsocket -c listen_addresses='' -c unix_socket_directories=$RT/pgsocket" start

# 3) 创建独立测试数据库
createdb -h "$RT/pgsocket" inventory_test

# 停止实例: pg_ctl -D "$RT/pgdata" stop
```

### 4.2 连接参数

解析顺序：`INV_DB_DSN`（完整 DSN）> `PGHOST/PGPORT/PGUSER/PGDATABASE`
> 默认值（`~/inventory-tool-runtime/pgsocket` + 库名 `inventory_test`）。

```sh
export PGHOME=~/inventory-tool-runtime
export PGHOST=$PGHOME/pgsocket
export PGDATABASE=inventory_test
PY=$PGHOME/venv/bin/python
```

### 4.3 CLI 命令

```sh
# 建表 + 导入初始库存 (--reset 清空后重建)
$PY -m inventory_tool.cli init examples/stock.csv --reset

# 执行请求 JSONL, 每条请求输出一行终态结果 (-o 可写文件)
$PY -m inventory_tool.cli run examples/requests.jsonl

# 查订单 (状态/明细/关联请求)
$PY -m inventory_tool.cli order ORD-1

# 库存与可用量 (按 SKU 排序)
$PY -m inventory_tool.cli stock

# 对账 (按 SKU 排序; 不一致时退出码 3)
$PY -m inventory_tool.cli reconcile

# 请求台账 / 逐商品流水
$PY -m inventory_tool.cli requests
$PY -m inventory_tool.cli ledger

# 生成合成输入 (固定 seed 可复现)
$PY -m inventory_tool.cli generate runtime/synth --seed 20261113 --sku-count 8 --order-count 12
```

### 4.4 运行测试

```sh
cd inventory_tool
~/inventory-tool-runtime/venv/bin/python -m pytest tests/ -v
```

并发压力脚本（8 轮进程屏障竞争，验证无死锁/超卖）：

```sh
PYTHONPATH=$PWD ~/inventory-tool-runtime/venv/bin/python runtime/stress.py
```

---

## 5. 可手算小样例

初始库存（`examples/stock.csv`）：

| SKU | on_hand |
| --- | --- |
| SKU-A | 10 |
| SKU-B | 6 |
| SKU-C | 3 |

请求序列（`examples/requests.jsonl`）与手算结果：

| # | request | 动作 | 手算过程 | 结果 |
| -| --- | --- | --- | --- |
| 1 | REQ-001 / ORD-1 | reserve A×4,B×2 | A 可用10≥4，B 可用6≥2，全够 | `RESERVED`；A reserved=4，B reserved=2 |
| 2 | REQ-002 / ORD-2 | reserve A×7,C×1 | ORD-1 后 A 可用=6 < 7 | `INSUFFICIENT_STOCK`，整单不占库存，无 ORD-2 |
| 3 | REQ-003 / ORD-1 | ship A×4,B×2 | 库存与预占同减 | `SHIPPED`；A: 10-4=6，B: 6-2=4，reserved 均归 0 |
| 4 | REQ-004 / ORD-3 | reserve B×3 | B 可用=4≥3 | `RESERVED`；B reserved=3 |
| 5 | REQ-005 / ORD-3 | cancel B×3 | 释放预占 | `CANCELLED`；B reserved 回到 0，on_hand 仍=4 |
| 6 | REQ-001 重放 | 同 #1 | 幂等 | 返回 #1 的结果，`replayed=true`，库存不变 |
| 7 | REQ-001 换内容 | A×9 | 同编号不同内容 | `REQUEST_CONTENT_CONFLICT`，拒绝且不改库存 |

最终库存：**A=6（reserved 0）、B=4（reserved 0）、C=3（reserved 0）**。

逐商品流水（`cli ledger`，可据此重算）：

```
INIT    A 10  (+10, 0)      INIT    B 6 (+6, 0)      INIT C 3 (+3, 0)
RESERVE A 4   (0, +4)       RESERVE B 2 (0, +2)
SHIP    A 4   (-4, -4)      SHIP    B 2 (-2, -2)
RESERVE B 3   (0, +3)       CANCEL  B 3 (0, -3)
```

- A：`on_hand = 10-4 = 6`，`reserved = 4-4 = 0`
- B：`on_hand = 6-2 = 4`，`reserved = 2-2+3-3 = 0`
- C：`on_hand = 3`，`reserved = 0`

与 `cli reconcile` 输出完全一致。

---

## 6. 强制终止（崩溃）测试与重试

服务层内置崩溃注入钩子（仅测试用），环境变量
`INV_CRASH_HOOK="<phase>:<request_id>"`：

- `pre_commit`：商品行已更新、**事务提交前** `SIGKILL` 本进程。连接断开后
  PostgreSQL 自动回滚，订单/台账/流水均不存在，库存未动；重试作为首次请求成功。
- `post_commit`：**事务提交后、返回前** `SIGKILL`。数据已落库；重试走幂等路径
  返回首次终态（`replayed=true`），不重复扣减。

CLI 手工验证：

```sh
$PY -m inventory_tool.cli init examples/stock.csv --reset
printf '%s\n' '{"request_id":"CRASH-1","order_id":"ORD-C","action":"reserve","items":[{"sku":"SKU-A","qty":2}]}' > runtime/one.jsonl

INV_CRASH_HOOK="pre_commit:CRASH-1" $PY -m inventory_tool.cli run runtime/one.jsonl   # 退出码 137
$PY -m inventory_tool.cli order ORD-C      # found=false (已回滚)
$PY -m inventory_tool.cli run runtime/one.jsonl    # 重试成功, replayed=false
$PY -m inventory_tool.cli reconcile        # 全部一致
```

自动化覆盖见 `tests/test_crash.py`（reserve/ship/cancel × 提交前后），重试后
核对订单状态、流水与库存完整一致。

---

## 7. 测试覆盖一览

| 测试文件 | 覆盖点 |
| --- | --- |
| `test_functional.py` | 整单成功/缺货原子失败、ship/cancel、终态互转、重复预占拒绝、幂等（含失败）、同号换内容拒绝、输入校验、流水重算 |
| `test_concurrency.py` | 两单抢最后库存、并发同单发货、发货/取消竞争、相反 SKU 顺序无死锁、缺货无部分预占、终态操作串行化 |
| `test_crash.py` | 提交前/提交后 SIGKILL，重试后订单/流水/库存完整一致 |

并发测试用 `multiprocessing` + `Barrier` 让多个进程在同一瞬间发起竞争；
断言前回滚父连接旧快照，确保读到子进程已提交的数据。
