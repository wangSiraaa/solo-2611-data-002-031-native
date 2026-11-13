# 合成多币种账簿 CLI

一个本地独立的模拟账簿，用于验证批量入账（post）与月末关账（close）。
仅处理合成数据；不提供任何真实业务数据或外部服务连接。

实现：Python 3.11（`psycopg` 3 + 标准库），PostgreSQL 16，单个本地集群，
Unix socket、`trust` 本地认证，不使用网络监听。

---

## 1. 快速开始

依赖已安装在本仓库的 `.venv`：

```sh
# 1) 启动独立 PostgreSQL 16 集群（默认数据目录 /tmp/ledger-pgdata，
#    socket 在 /tmp/ledger-pg-sock，端口 55432）
bash scripts/pg_start.sh

# 2) 建表 / 迁移（幂等）
.venv/bin/python -m ledger init-db

# 3) 合成数据 + 完整演示（入账、冲销、关账、试算、审计）
bash scripts/demo.sh

# 4) 运行全部测试（含并发与进程终止）
bash scripts/run_tests.sh
```

> 数据目录默认放在 `/tmp` 而非仓库内：本工作区的 `/workspace` 是 FUSE
> 文件系统，会拒绝 PostgreSQL 需要的文件权限/socket 操作，导致偶发
> `Permission denied`。可用 `LEDGER_PGDATA`、`LEDGER_PGHOST`、`LEDGER_PGPORT`、
> `LEDGER_PGDATABASE` 覆盖。

停止集群：

```sh
bash scripts/pg_stop.sh
```

### 连接参数

CLI 默认读取环境变量（也可用 `--dsn` 显式指定）：

| 变量 | 默认值 |
| --- | --- |
| `LEDGER_DSN` | 空；若设置则优先使用 |
| `LEDGER_PGHOST` | `/tmp/ledger-pg-sock` |
| `LEDGER_PGPORT` | `55432` |
| `LEDGER_PGDATABASE` | `ledger` |
| `LEDGER_PGUSER` | 空（本地 trust，自动使用当前用户） |

等价 DSN 示例：

```
host=/tmp/ledger-pg-sock port=55432 dbname=ledger user=$(id -un)
```

---

## 2. 命令一览

```
ledger init-db                       应用迁移（幂等，记录 schema_migrations）
ledger reset-db --yes                清空 public schema 并重新迁移（破坏性）
ledger load-accounts <accounts.csv>  载入账户（account_id,currency）
ledger post --entry-file f.json      记账
ledger post --entry-json '<json>'    记账（内联 JSON）
ledger reverse --reversal-file f     冲销
ledger reverse --reversal-json '<j>' 冲销
ledger close --period 2026-03        关账并存快照（幂等）
ledger trial-balance [--period P]    试算平衡（累计或按期间）
ledger audit                         对原始表独立校验全部不变量
ledger show-entry <entry_id>         查看凭证与分录
ledger demo-data --period P --out-dir D  生成合成 CSV/JSON
ledger sql '<SELECT ...>'            只读 SQL 查询（连接设为 read-only）
```

所有命令输出单个 JSON 对象。成功时 `"ok": true`；确定性拒绝时
`"ok": false` 且带 `error` 代码，退出码 1；数据库/连接错误退出码 2。

镜像入口：`python -m ledger ...` 或 `python ledger_cli.py ...`。

---

## 3. 账户 CSV

```csv
account_id,currency
cash-usd,USD
revenue-usd,USD
cash-eur,EUR
```

- `account_id` 全局唯一，账户币种固定且不可变。
- 重复载入相同声明是幂等 no-op；同一 `account_id` 声明不同币种会整体拒绝
  （`accounts_conflict`），不会部分写入。
- 载入同时为该账户建立零余额行，后续入账只更新这一行。

## 4. 凭证 JSON（post）

```json
{
  "entry_id": "e-001",
  "entry_date": "2026-03-05",
  "lines": [
    {"account_id": "cash-usd",   "currency": "USD", "debit_cents": 120000, "credit_cents": 0},
    {"account_id": "revenue-usd","currency": "USD", "debit_cents": 0, "credit_cents": 120000},
    {"account_id": "cash-eur",   "currency": "EUR", "debit_cents": 9000,   "credit_cents": 0},
    {"account_id": "revenue-eur","currency": "EUR", "debit_cents": 0,      "credit_cents": 9000}
  ]
}
```

规则（任一违反 → 整张凭证失败，无部分分录）：

1. 金额为非负整数“分”，每行恰好一方（借方或贷方）大于零。
2. 一张凭证可含多个币种，但**每个币种各自**借贷相等；不允许币种间串抵。
3. 账户必须已在 CSV 中声明；分录声明的币种必须等于账户固定币种。
4. 记账日期所在的自然月（`YYYY-MM`）即会计期间。期间已关闭则拒绝
   （`period_closed`）。

## 5. 冲销 JSON（reverse）

```json
{"entry_id": "r-001", "reverses_entry_id": "e-001", "entry_date": "2026-04-02"}
```

- 生成新凭证，逐行借贷互换、账户与币种不变、金额完全对称。
- 原凭证只能成功冲销一次（`already_reversed`）；并发也只有一个成功。
- 冲销凭证自身不能再次被冲销（`not_reversible`）。
- 冲销日期不得早于原凭证日期。
- 原凭证即使所在期间已关闭，也可在后续开放期间冲销；已关闭期间的数据与
  快照保持不变。

## 6. 关账（close）

- 按“账户 × 币种”汇总期间内借方、贷方、净额，并可 `close` 任意月份
  （包括空期间，空快照）。
- 关闭后拒绝该期间新入账（`period_closed`）。
- 重复关账返回同一快照（相同 `snapshot_id`、`snapshot_hash`）。
- 快照同时写入 `period_snapshots` 行、`periods` 头部（含 JSON 与哈希）以及
  `requests` 请求记录，全部同一事务提交。

---

## 7. 一致性模型

### 7.1 单一写者锁，明确关账/入账边界

所有写命令（post/reverse/close）在**同一事务**中先取
`pg_advisory_xact_lock(0x1ED6E5)`。该锁在提交/回滚时自动释放，因此：

- 关账与入账有唯一的总顺序：凭证要么在关账聚合之前提交并进入快照，要么
  在关账之后看到已关闭期间而被拒绝。不存在“已记账却不在快照中”。
- 同一 `entry_id` 的并发重试自然串行化：第一个创建，其余命中内容哈希返回
  原结果。
- 并发冲销同一凭证：只有一个能插入 `reverses_entry_id`，其余报
  `already_reversed`。

数据库层还有兜底：`close` 在读取聚合前 `LOCK TABLE entries, postings IN
SHARE MODE`，阻止绕过 CLI 的并发 INSERT 破坏快照边界；触发器禁止向已关闭
期间插入凭证。

### 7.2 原子性

一次成功的 post/reverse 在同一事务中写入：

- `entries` 头部（含规范内容与内容哈希、请求 SHA-256）
- `postings` 全部明细
- `account_balances` 累计余额增量（每行 1 次 UPDATE）
- `requests` 请求/响应记录

任一步失败 → 整个事务回滚。测试用 SIGKILL 验证“插入后提交前终止”无残留，
“提交后响应前终止”保留已提交效果且重试幂等。

### 7.3 不可变与防篡改

`accounts`、`account_balances`(DELETE)、`entries`、`postings`、`periods`、
`period_snapshots`、`requests` 上的触发器拒绝 UPDATE/DELETE（唯一例外是
CLI 在提交前对自身事务内余额行的 UPDATE，属于同一事务写入）。原始凭证与快照
一经提交不可修改或删除；`reset-db` 是唯一明确的整体重建入口。

### 7.4 幂等键

请求幂等键 = 规范化 JSON 的 SHA-256：

- JSON 键排序、无多余空白、UTF-8，因此键顺序/空白不同的等价输入视为同一请求。
- post 重试命中 `entries.content_hash`（规范内容）返回原结果；同 `entry_id`
  不同内容 → `entry_id_conflict`。
- reverse 重试命中冲销凭证规范内容；同一原凭证的第二次不同冲销 →
  `already_reversed`。
- close 以期间为幂等键，重复返回原快照。

`requests.request_sha256` 为主键，每条成功写请求恰好一行，与效果同事务。

---

## 8. 审计（audit）

`ledger audit` 只读原始表，独立重算并核对：

1. `account_balances` 与 `postings` 聚合完全一致（含零余额账户）；
2. 每个币种全账借贷差为零；
3. 每张凭证在它所触及的每个币种内借贷相等；
4. 不存在无分录凭证；
5. `canonical_content`/`request_json` 哈希与存储值一致；
6. 每个 post/reverse/close 请求都有对应效果，响应与存储一致；
7. 每个期间的 `snapshot_hash` 可重算，`entry_count`/`posting_count` 与原始行
   一致，`period_snapshots` 行等于原始聚合；
8. 每条冲销逐行镜像原凭证，且“原凭证 + 冲销”对每个账户/币种净影响恰为 0；
9. 分录声明币种等于账户固定币种。

测试 `tests/helpers.py::assert_raw_ledger_invariants` 用另一套纯 SQL 重算，
与 CLI `audit` 相互独立。

---

## 9. 目录结构

```
ledger/ledger.py         CLI 与全部业务逻辑
ledger/__main__.py       python -m ledger 入口
ledger_cli.py            独立脚本入口
migrations/001_schema.sql   表、约束、触发器
migrations/002_views.sql    只读审计视图
tests/                  端到端、并发、崩溃测试
scripts/                启动/停止/演示/测试脚本
docs/MANUAL_EXAMPLE.md  小规模手算样例
docs/VERIFICATION.md    验证记录与原始 SQL 核对结果
```

环境变量（仅测试使用，生产勿设）：`LEDGER_TEST_START_FILE`、
`LEDGER_TEST_PRE_LOCK_MS`、`LEDGER_TEST_LOCK_HOLD_MS`、
`LEDGER_TEST_LOCK_MARKER`、`LEDGER_CRASH_AT`、`LEDGER_CRASH_MARKER`。
